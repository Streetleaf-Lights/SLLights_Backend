"""
The seven user-management operations: invite (= admin-initiated
"create"), register (an invitee completing their own setup), sign in,
sign out, forgot password, reset password, and delete (a real row
removal -- see delete_user()'s own docstring for the earlier
soft-delete design this replaced, and why the change isn't reversible).

Role model: 'Streetleaf Admin' can invite and delete users; 'Customer
Admin' cannot do either (an explicit requirement) -- both are enforced
via shared/auth_utils.py's require_role(), not re-implemented here.

Anti-enumeration note, worth being explicit about: sign_in() and
forgot_password() are both deliberately designed so their behavior
doesn't reveal whether a given email exists in the system -- a wrong
password and a nonexistent email produce the exact same sign_in() error,
and forgot_password() never raises for a nonexistent email at all (the
HTTP layer always returns the same generic "if that email exists..."
message regardless of what actually happened internally). This is a
deliberate, standard security practice for these two flows specifically
-- it does NOT apply to invite_user(), where an existing email SHOULD
produce a clear, specific error, since that endpoint is only reachable
by an already-authenticated Streetleaf Admin, not a public/anonymous
caller.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

from shared.auth_utils import (
    AuthContext,
    TOKEN_LIFETIME,
    create_session,
    generate_token,
    hash_password,
    require_role,
    verify_password,
)
from shared.auth_utils import AuthError
from shared.datetime_utils import to_dto_string as _to_dto_string
from shared.email_client import EmailSendError, send_email
from shared.sql_client import get_connection

_VALID_ROLES = ("Streetleaf Admin", "Customer Admin")


def _parse_uuid(value, error_message: str) -> uuid.UUID:
    """
    Converts an incoming string (a token or user id arriving from an
    HTTP request body/query param) into a uuid.UUID, suitable for
    binding into a uniqueidentifier column -- Users.Id and
    Users.ResetToken are both that type in the real schema, and pyodbc/
    SQL Server can fail outright to implicitly convert a plain string
    parameter into that column type (this is what originally surfaced as
    a raw "Conversion failed when converting from a character string to
    uniqueidentifier" SQL error, rather than a clean 400).

    Raises AuthError(error_message, 400) if value isn't a well-formed
    UUID at all -- treated the same as "won't match any real row",
    since a malformed token/id can't possibly match one either way.
    """
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise AuthError(error_message, status_code=400)


def _registration_link(token: str) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    return f"{base_url}/register?token={token}"


def _reset_link(token: str) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    return f"{base_url}/reset-password?token={token}"


def invite_user(
    inviter: AuthContext, name: str, email: str, role: str, customer_id: str = None
) -> dict:
    """
    Creates a Pending user record and emails an invite link (the
    admin-initiated "create"). Only a Streetleaf Admin may call this --
    an explicit requirement, not an incidental default.
    """
    require_role(inviter, ["Streetleaf Admin"])

    if role not in _VALID_ROLES:
        raise AuthError(f"role must be one of: {', '.join(_VALID_ROLES)}", status_code=400)
    if role == "Customer Admin" and not customer_id:
        raise AuthError("customerId is required when role is 'Customer Admin'", status_code=400)
    if role == "Streetleaf Admin" and customer_id:
        # Not a hard security issue, but silently ignoring a customerId
        # that doesn't mean anything for this role would be more
        # confusing than rejecting it outright.
        raise AuthError("customerId must not be given when role is 'Streetleaf Admin'", status_code=400)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM Users WHERE Email = ?", email)
        if cursor.fetchone() is not None:
            raise AuthError("a user with this email already exists", status_code=409)

        # Bound directly as a uuid.UUID, not str(uuid.uuid4()) -- Users.Id
        # is uniqueidentifier in the real schema; pyodbc converts an
        # actual UUID object to/from SQL Server's native GUID type
        # correctly, but can fail to implicitly convert a plain string
        # parameter into that column type.
        user_id = uuid.uuid4()
        token = generate_token()  # also a uuid.UUID -- see its own docstring
        expires_at = datetime.now(timezone.utc) + TOKEN_LIFETIME

        cursor.execute(
            """
            INSERT INTO Users (Id, Name, Email, Role, Status, CustomerId, PasswordHash, ResetToken, ResetTokenExpiresAt)
            VALUES (?, ?, ?, ?, 'Pending', ?, NULL, ?, ?)
            """,
            user_id,
            name,
            email,
            role,
            customer_id,
            token,
            _to_dto_string(expires_at),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    email_sent = True
    try:
        send_email(
            to_address=email,
            subject="You've been invited to LightsApp",
            body_html=(
                f"<p>Hi {name},</p>"
                f"<p>You've been invited to LightsApp. "
                f'<a href="{_registration_link(token)}">Click here to set up your account</a>.</p>'
                f"<p>This link expires in 48 hours.</p>"
            ),
        )
    except EmailSendError as ex:
        # The Pending user row is already committed above -- an email
        # delivery hiccup shouldn't undo a successful invite, just be
        # visible to the caller so it can be resent/investigated rather
        # than silently swallowed.
        email_sent = False
        logging.error("invite_user: user %s created but invite email failed: %s", user_id, ex)

    # str(user_id): a raw uuid.UUID isn't JSON-serializable -- json.dumps()
    # would raise on it at the HTTP layer.
    return {"userId": str(user_id), "email": email, "emailSent": email_sent}


def register_user(token: str, password: str) -> dict:
    """
    Completes an invited user's account setup: verifies the invite token,
    sets their password, activates the account, and signs them in
    immediately (returns a session token) so they don't need a separate
    sign-in step right after registering.
    """
    token_uuid = _parse_uuid(token, "invalid or expired invite link")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT Id, Role, CustomerId, Name, Email
            FROM Users
            WHERE ResetToken = ? AND Status = 'Pending' AND ResetTokenExpiresAt > SYSDATETIMEOFFSET()
            """,
            token_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("invalid or expired invite link", status_code=400)

        # user_id comes back from this uniqueidentifier column as an
        # actual uuid.UUID (pyodbc's normal read behavior) -- kept as
        # that type for the UPDATE below (needs it), then converted to a
        # string for everything downstream that isn't a Users-table
        # query (create_session()'s UserSessions.UserId is VARCHAR, and
        # a raw UUID isn't JSON-serializable for the response anyway).
        user_id, role, customer_id, name, email = row
        password_hash = hash_password(password)

        cursor.execute(
            """
            UPDATE Users
            SET PasswordHash = ?, Status = 'Active', ResetToken = NULL, ResetTokenExpiresAt = NULL
            WHERE Id = ?
            """,
            password_hash,
            user_id,
        )

        user_id_str = str(user_id)
        session_token = create_session(cursor, user_id_str, role, customer_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return {
        "token": session_token,
        "user": {"id": user_id_str, "name": name, "email": email, "role": role, "customerId": customer_id},
    }


def sign_in(email: str, password: str) -> dict:
    """
    Verifies email/password and, if valid, creates a new session.
    Deliberately generic error for every failure mode (no such email,
    account not Active yet, wrong password) -- see this module's own
    docstring on why.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Id, Name, Role, Status, CustomerId, PasswordHash FROM Users WHERE Email = ?",
            email,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("invalid email or password")

        user_id, name, role, status, customer_id, password_hash = row
        if status != "Active" or not verify_password(password, password_hash):
            raise AuthError("invalid email or password")

        # user_id comes back from Users.Id (uniqueidentifier) as a
        # uuid.UUID -- stringified before create_session(), whose
        # UserSessions.UserId column is VARCHAR, and before the response
        # dict, since a raw UUID isn't JSON-serializable.
        user_id_str = str(user_id)
        session_token = create_session(cursor, user_id_str, role, customer_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return {
        "token": session_token,
        "user": {"id": user_id_str, "name": name, "email": email, "role": role, "customerId": customer_id},
    }


def sign_out(ctx: AuthContext) -> None:
    """Revokes the caller's current session -- the specific session tied
    to the token they authenticated this request with, not every session
    they might have open elsewhere."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE Id = ? AND RevokedAt IS NULL",
            _to_dto_string(datetime.now(timezone.utc)),
            ctx.session_id,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def forgot_password(email: str) -> None:
    """
    Issues a password-reset token and emails it, IF a matching Active
    user exists -- but never raises, and never reports either way,
    regardless of whether one does. The HTTP layer always returns the
    same generic "if that email exists, a reset link has been sent"
    message no matter what this function actually did internally --
    that's the whole anti-enumeration point, so don't change this
    function to signal success/failure differently.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Id, Name FROM Users WHERE Email = ? AND Status = 'Active'",
            email,
        )
        row = cursor.fetchone()
        if row is None:
            return

        user_id, name = row
        token = generate_token()
        expires_at = datetime.now(timezone.utc) + TOKEN_LIFETIME

        cursor.execute(
            "UPDATE Users SET ResetToken = ?, ResetTokenExpiresAt = ? WHERE Id = ?",
            token,
            _to_dto_string(expires_at),
            user_id,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    try:
        send_email(
            to_address=email,
            subject="Reset your LightsApp password",
            body_html=(
                f"<p>Hi {name},</p>"
                f'<p><a href="{_reset_link(token)}">Click here to reset your password</a>.</p>'
                f"<p>This link expires in 48 hours. If you didn't request this, you can ignore this email.</p>"
            ),
        )
    except EmailSendError as ex:
        # Same reasoning as invite_user(): the token is already committed
        # above, so this is a delivery problem to log and investigate,
        # not something that should change forgot_password()'s outward
        # behavior (which must stay identical whether or not the email
        # existed in the first place).
        logging.error("forgot_password: reset token issued for %s but email failed: %s", user_id, ex)


def reset_password(token: str, new_password: str) -> None:
    """
    Completes a password reset: verifies the token, sets the new
    password, and revokes every one of that user's currently-active
    sessions -- if the password needed resetting because it (or an
    existing session) was compromised, any sessions started under the
    old password shouldn't be trusted to keep working afterward.
    """
    token_uuid = _parse_uuid(token, "invalid or expired reset link")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT Id FROM Users
            WHERE ResetToken = ? AND Status = 'Active' AND ResetTokenExpiresAt > SYSDATETIMEOFFSET()
            """,
            token_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("invalid or expired reset link", status_code=400)

        user_id = row[0]  # a uuid.UUID, straight from Users.Id
        password_hash = hash_password(new_password)

        cursor.execute(
            """
            UPDATE Users
            SET PasswordHash = ?, ResetToken = NULL, ResetTokenExpiresAt = NULL
            WHERE Id = ?
            """,
            password_hash,
            user_id,
        )
        # UserSessions.UserId is VARCHAR, unlike Users.Id -- needs the
        # string form here specifically, not the UUID object used just
        # above for the Users-table UPDATE.
        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE UserId = ? AND RevokedAt IS NULL",
            _to_dto_string(datetime.now(timezone.utc)),
            str(user_id),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def delete_user(caller: AuthContext, target_user_id: str) -> None:
    """
    Permanently removes a user (a hard DELETE, not a status change) and
    immediately revokes any of their active sessions. Previously a soft
    deactivation (Status = 'Deactivated'); changed to a real row removal
    per explicit request. Worth knowing: this is not reversible -- once
    a user is deleted there's no built-in way to restore their record
    (name/email/role/customer association), unlike the soft-delete
    version this replaced. The 'Deactivated' Status value added for that
    earlier design (see sql/Users/Add Deactivated status and ResetToken
    index.sql) is now unused by this function, but left in place in the
    database -- an unused, allowed Status value is harmless, and wasn't
    worth a separate migration to revert.

    Restricted to Streetleaf Admin, same as invite_user() -- this wasn't
    spelled out as explicitly as invite's restriction was, but is treated
    as the safer default given user management as a whole was described
    as something Customer Admin is restricted from. Loosen this (e.g. to
    let a Customer Admin delete users within their own CustomerId) if
    that turns out to be wanted instead -- it's a small, contained change
    here.
    """
    require_role(caller, ["Streetleaf Admin"])
    target_user_id_uuid = _parse_uuid(target_user_id, "user not found")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM Users WHERE Id = ?",
            target_user_id_uuid,
        )
        if cursor.rowcount == 0:
            raise AuthError("user not found", status_code=404)

        # UserSessions.UserId is VARCHAR -- the original string form of
        # target_user_id (not the UUID object used just above for the
        # Users-table DELETE) is what belongs here. No FK between the two
        # tables, so these rows are simply left behind, already revoked,
        # as a historical record -- same as any other revoked session.
        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE UserId = ? AND RevokedAt IS NULL",
            _to_dto_string(datetime.now(timezone.utc)),
            target_user_id,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
