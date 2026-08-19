"""
The nine user-management operations: invite (= admin-initiated
"create"), resend invite (refresh an existing Pending user's invite link
in place, without creating a new user record), register (an invitee
completing their own setup), sign in, sign out, forgot password, reset
password, delete (a real row removal -- see delete_user()'s own
docstring for the earlier soft-delete design this replaced, and why the
change isn't reversible), and change role (toggles Admin <-> 'User'
within the same organization -- see change_role()'s own docstring).

Role model, three roles: 'Streetleaf Admin' (broad access, not scoped to
one customer), 'Customer Admin' (restricted to their own CustomerId's
data), and 'User' (also restricted to their own CustomerId's data, with
narrower permissions than Customer Admin within it -- the specific
differences are enforced wherever else in this project reads ctx.role,
not re-litigated here). Invite permissions specifically (see
invite_user()'s own docstring for the full reasoning): Streetleaf Admin
can invite any of the three; Customer Admin can invite Customer
Admin/User (but not Streetleaf Admin), and only for their own
CustomerId; User cannot invite at all. Delete and change-role
permissions (see delete_user()'s and change_role()'s own docstrings for
the full reasoning) are structurally IDENTICAL to each other: Streetleaf
Admin can act on any Streetleaf Admin/Customer Admin/User except itself;
Customer Admin can act on a Customer Admin/User within their own
CustomerId only, never a Streetleaf Admin; User can do neither to
anyone. No caller, of any role, can ever delete or change the role of
their own account. resend_invite() remains Streetleaf-Admin-only for now
-- see its own docstring for why that's narrower than invite_user()'s
(and now delete_user()'s/change_role()'s) own model, and how to widen it
later if that turns out to be wanted too.

Anti-enumeration note, worth being explicit about: sign_in() and
forgot_password() are both deliberately designed so their behavior
doesn't reveal whether a given email exists in the system -- a wrong
password and a nonexistent email produce the exact same sign_in() error,
and forgot_password() never raises for a nonexistent email at all (the
HTTP layer always returns the same generic "if that email exists..."
message regardless of what actually happened internally). This is a
deliberate, standard security practice for these two flows specifically
-- it does NOT apply to invite_user()/resend_invite(), where a
nonexistent/wrong-status target SHOULD produce a clear, specific error,
since those endpoints are only reachable by an already-authenticated
caller with real invite permissions, not a public/anonymous caller.
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

_VALID_ROLES = ("Streetleaf Admin", "Customer Admin", "User")


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


def _send_invite_email(user_id, name: str, email: str, token: str, operation_name: str) -> bool:
    """
    Shared by invite_user() and resend_invite() -- both need to send the
    exact same invite email shape (a registration link, valid for
    TOKEN_LIFETIME), just with a different token bound to it each time.
    A failed send here is deliberately non-fatal to the caller: the
    Users row (created by invite_user(), or refreshed in place by
    resend_invite()) is already committed by the time this runs, so an
    email delivery hiccup shouldn't undo that -- just be visible via the
    returned emailSent=False so it can be investigated/retried, not
    silently swallowed. operation_name is purely for the log line, so a
    failure here is traceable to which of the two callers produced it.
    """
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
        return True
    except EmailSendError as ex:
        logging.error("%s: user %s's invite email failed to send: %s", operation_name, user_id, ex)
        return False


def _normalize_customer_id(value):
    """
    Treats None, an empty/whitespace-only string, and the literal string
    "null" (case-insensitive) as all meaning the exact same thing: "no
    customerId was actually given" -- not three different
    representations of it. Defends against a caller (or a buggy
    frontend) sending customerId as "" or the string "null" instead of
    genuinely omitting the key or sending JSON null -- either of those
    would otherwise slip straight past a plain `if customer_id` check,
    since both are non-empty, truthy strings in Python. Returns the
    ORIGINAL, unmodified value for anything that isn't one of these --
    doesn't strip/alter a genuine customerId's own content, only decides
    whether it counts as "missing" at all.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped.lower() == "null":
        return None
    return value


def invite_user(
    inviter: AuthContext, name: str, email: str, role: str, customer_id: str = None
) -> dict:
    """
    Creates a Pending user record and emails an invite link (the
    admin-initiated "create").

    Permission model -- an explicit requirement, not an incidental
    default: 'Streetleaf Admin' can invite any of the three roles.
    'Customer Admin' can invite 'Customer Admin'/'User' but NOT
    'Streetleaf Admin' (can't create a role with more privilege than
    their own), and -- critically, since 'Customer Admin' wasn't
    permitted to invite AT ALL before this -- only ever for their OWN
    CustomerId, never an arbitrary one: customer_id is silently forced
    to inviter.customer_id for a Customer Admin caller (an explicitly
    passed, MISMATCHED customer_id is rejected outright, rather than
    silently overridden, so a caller relying on it being honored finds
    out immediately rather than getting a quietly different result than
    what they asked for). Without this, any Customer Admin could invite
    a Customer Admin/User for a DIFFERENT customer entirely -- a genuine
    cross-tenant privilege-escalation hole, not a hypothetical one.
    'User' cannot invite at all -- require_role() below rejects that
    caller before anything else runs.

    'User' is legitimately valid with EITHER a customerId (a
    customer-side user) or WITHOUT one (a "Streetleaf User" -- e.g.
    Streetleaf's own staff who need ordinary, non-admin access, the same
    way Streetleaf Admin itself has no customerId). Only a Streetleaf
    Admin caller can actually produce the unscoped case -- a Customer
    Admin caller's own customer_id is always forced to their own value
    per the paragraph above, so they can never create an unscoped user
    even if they wanted to. This is genuinely different from 'Customer
    Admin', which DOES always require a customerId (there's no
    equivalent "unscoped Customer Admin" concept) -- see the check
    itself, further down, for exactly where these two diverge.

    If a Pending invite already exists for this email and just needs a
    fresh link (e.g. the original expired, or the email got lost), use
    resend_invite() instead -- this function's own existence check below
    doesn't distinguish Pending from Active, so calling this again for
    an email that's already Pending fails with 409, by design (see
    resend_invite()'s own docstring for the full reasoning).

    customer_id is normalized via _normalize_customer_id() before any of
    the checks below run -- None, "", whitespace-only, and the literal
    string "null" are all treated identically as "not given", not three
    separately-handled cases.
    """
    require_role(inviter, ["Streetleaf Admin", "Customer Admin"])
    customer_id = _normalize_customer_id(customer_id)

    if role not in _VALID_ROLES:
        raise AuthError(f"role must be one of: {', '.join(_VALID_ROLES)}", status_code=400)

    if inviter.role == "Customer Admin":
        if role == "Streetleaf Admin":
            raise AuthError("a Customer Admin cannot invite a Streetleaf Admin", status_code=403)
        if customer_id and customer_id != inviter.customer_id:
            raise AuthError(
                "a Customer Admin can only invite users for their own customer", status_code=403
            )
        # role is now known to be 'Customer Admin' or 'User' (the only
        # two not already rejected above). A Customer Admin caller can
        # never create an unscoped user (there's no such thing as an
        # "unscoped Customer Admin" invite, and this caller specifically
        # isn't permitted to create an unscoped "Streetleaf User"
        # either -- only a Streetleaf Admin can do that, see below), so
        # the only customer_id that can ever be correct here is the
        # inviter's own. Set unconditionally (not just when customer_id
        # was omitted) so an explicitly-passed, MATCHING value and an
        # omitted one behave identically -- this is the single point
        # that actually enforces the scoping, not just the rejection
        # check above.
        customer_id = inviter.customer_id

    # 'User' deliberately has NO equivalent check -- unlike 'Customer
    # Admin', which always needs a customerId (there's no such thing as
    # a Customer Admin unscoped from any customer), 'User' is
    # legitimately valid EITHER way: WITH a customerId (a customer-side
    # user) or WITHOUT one (a "Streetleaf User" -- e.g. Streetleaf's own
    # staff who need normal-user-level access without admin privileges,
    # analogous to how Streetleaf Admin itself has no customerId). Only
    # a Streetleaf Admin caller can actually produce the unscoped case in
    # practice -- a Customer Admin caller's own customer_id is already
    # forced to their own (non-None) value above, before this check ever
    # runs.
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

    email_sent = _send_invite_email(user_id, name, email, token, "invite_user")

    # str(user_id): a raw uuid.UUID isn't JSON-serializable -- json.dumps()
    # would raise on it at the HTTP layer.
    return {"userId": str(user_id), "email": email, "emailSent": email_sent}


def resend_invite(caller: AuthContext, target_user_id: str) -> dict:
    """
    Re-sends an invite email to an existing Pending user, refreshing
    their invite token/expiry IN PLACE -- the same Users row, same Id,
    same CreatedAt -- rather than a delete_user()-then-invite_user()
    round trip, which would generate a brand new Id and discard any
    history tied to the original one. Restricted to Streetleaf Admin --
    narrower than invite_user()'s own model (which permits Customer
    Admin) and delete_user()'s/change_role()'s own model (which now also
    permit Customer Admin, within their own CustomerId) -- see any of
    those functions' own docstrings; widen this the same way if
    resending is ever wanted for a Customer Admin caller too.

    Only valid for a user CURRENTLY Status = 'Pending' -- raises 409 for
    an Active user (there's no invite link left to resend for them; use
    forgot_password() instead if they need a new password-reset link)
    and 404 for a user that doesn't exist at all. Deliberately does NOT
    touch Name/Email/Role/CustomerId -- if any of those genuinely need
    to change, that's outside this function's own scope (delete and
    re-invite, or a separate "edit pending user" operation, not this
    one).
    """
    require_role(caller, ["Streetleaf Admin"])
    target_user_id_uuid = _parse_uuid(target_user_id, "user not found")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Name, Email, Status FROM Users WHERE Id = ?",
            target_user_id_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("user not found", status_code=404)

        name, email, status = row
        if status != "Pending":
            raise AuthError("only a Pending user's invite can be resent", status_code=409)

        token = generate_token()
        expires_at = datetime.now(timezone.utc) + TOKEN_LIFETIME

        cursor.execute(
            """
            UPDATE Users
            SET ResetToken = ?, ResetTokenExpiresAt = ?
            WHERE Id = ?
            """,
            token,
            _to_dto_string(expires_at),
            target_user_id_uuid,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    email_sent = _send_invite_email(target_user_id_uuid, name, email, token, "resend_invite")

    return {"userId": str(target_user_id_uuid), "email": email, "emailSent": email_sent}


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

    Permission model -- an explicit requirement, not an incidental
    default, and structurally similar to invite_user()'s own (see that
    function's docstring for the parallel): 'Streetleaf Admin' can
    delete any other Streetleaf Admin, any Customer Admin, and any User
    -- but never itself (self-deletion is rejected regardless of role,
    not just for Streetleaf Admin -- see below). 'Customer Admin' can
    delete a Customer Admin or User belonging to their OWN CustomerId
    only -- never a Streetleaf Admin (any CustomerId), and never a
    Customer Admin/User belonging to a DIFFERENT CustomerId, even though
    'Customer Admin' itself has no comparable "own record" concept
    protecting THOSE targets beyond the ordinary CustomerId-matching
    check. 'User' cannot delete anyone at all -- require_role() below
    rejects that caller before anything else runs.

    Self-deletion is rejected for EVERY role uniformly, not just
    Streetleaf Admin -- deleting your own account through this same
    endpoint you're calling it from has no sensible recovery path (your
    own session would be revoked mid-request), and there's no
    legitimate reason any caller would need to self-delete through an
    admin-facing operation rather than some other, deliberate account-
    closure flow.
    """
    require_role(caller, ["Streetleaf Admin", "Customer Admin"])
    target_user_id_uuid = _parse_uuid(target_user_id, "user not found")

    # Checked BEFORE any database round trip -- a pure comparison of two
    # already-known values, so there's nothing to gain from querying
    # first. caller.user_id comes from an already-verified JWT (not
    # untrusted input the way target_user_id is), so it's parsed
    # directly rather than through _parse_uuid()'s own defensive error
    # handling -- a malformed value here would mean something is wrong
    # with session issuance itself, not with what this caller passed in.
    if target_user_id_uuid == uuid.UUID(caller.user_id):
        raise AuthError("cannot delete your own account", status_code=403)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Role, CustomerId FROM Users WHERE Id = ?",
            target_user_id_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("user not found", status_code=404)
        target_role, target_customer_id = row

        if caller.role == "Customer Admin":
            if target_role == "Streetleaf Admin":
                raise AuthError("a Customer Admin cannot delete a Streetleaf Admin", status_code=403)
            if target_customer_id != caller.customer_id:
                raise AuthError(
                    "a Customer Admin can only delete users for their own customer", status_code=403
                )

        cursor.execute(
            "DELETE FROM Users WHERE Id = ?",
            target_user_id_uuid,
        )
        conn.commit()

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


def _toggle_role(current_role: str, customer_id) -> str:
    """
    Toggles between an Admin role and 'User', WITHIN the same
    organization the target already belongs to -- CustomerId itself is
    never touched by this, only Role. A target with customer_id=None
    (Streetleaf Admin or a "Streetleaf User" -- see invite_user()'s own
    docstring for that concept) toggles between 'Streetleaf Admin' and
    'User', staying unscoped either way. A target with a real
    customer_id (Customer Admin or a customer-side User) toggles between
    'Customer Admin' and 'User', staying within that SAME customer
    either way -- this is what "keeping the same organization" means in
    practice: which organization a user belongs to is entirely a
    function of their own customer_id, not their Role, so leaving
    customer_id untouched IS what keeps them in place.
    """
    if current_role == "User":
        return "Streetleaf Admin" if customer_id is None else "Customer Admin"
    return "User"


def change_role(caller: AuthContext, target_user_id: str) -> dict:
    """
    Toggles a user's role between an Admin role and 'User', keeping them
    within the SAME organization (Streetleaf if they currently have no
    CustomerId, or that same Customer if they do) -- see
    _toggle_role()'s own docstring for exactly how the new role is
    determined; CustomerId itself is never changed by this operation,
    only Role.

    Permission model -- identical in STRUCTURE to delete_user()'s own
    (see that function's docstring for the parallel, including the exact
    reasoning behind each restriction, not re-litigated here):
    'Streetleaf Admin' can change the role of any other Streetleaf
    Admin, any Customer Admin, and any User -- but never itself.
    'Customer Admin' can change the role of a Customer Admin or User
    belonging to their OWN CustomerId only -- never a Streetleaf Admin
    (any CustomerId), and never a Customer Admin/User belonging to a
    DIFFERENT CustomerId. 'User' cannot change anyone's role at all --
    require_role() below rejects that caller before anything else runs.

    Self-role-change is rejected for EVERY role uniformly, not just
    Streetleaf Admin -- same reasoning as delete_user()'s own equivalent
    restriction: changing your own role mid-session has no sensible,
    safe in-place outcome (your own privileges would shift under you
    while your existing session/JWT still reflects the old ones), and
    there's no legitimate reason any caller would need to self-promote
    or self-demote through this same admin-facing operation.

    Also revokes the target's active sessions, same as delete_user() --
    a role change is a privilege change, and an existing JWT/session
    issued under the OLD role would otherwise keep granting (or keep
    denying) access based on stale information until it naturally
    expires, regardless of which direction the change went. Forcing a
    fresh sign-in makes the new role take effect immediately rather than
    "eventually".

    Returns {"userId": ..., "role": <the NEW role>, "customerId": ...}
    -- customerId echoed back unchanged, confirming this operation
    didn't touch it.
    """
    require_role(caller, ["Streetleaf Admin", "Customer Admin"])
    target_user_id_uuid = _parse_uuid(target_user_id, "user not found")

    # Checked BEFORE any database round trip -- same reasoning, same
    # placement, as delete_user()'s own equivalent check.
    if target_user_id_uuid == uuid.UUID(caller.user_id):
        raise AuthError("cannot change your own role", status_code=403)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Role, CustomerId FROM Users WHERE Id = ?",
            target_user_id_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("user not found", status_code=404)
        target_role, target_customer_id = row

        if caller.role == "Customer Admin":
            if target_role == "Streetleaf Admin":
                raise AuthError(
                    "a Customer Admin cannot change the role of a Streetleaf Admin", status_code=403
                )
            if target_customer_id != caller.customer_id:
                raise AuthError(
                    "a Customer Admin can only change the role of users for their own customer",
                    status_code=403,
                )

        new_role = _toggle_role(target_role, target_customer_id)

        cursor.execute(
            "UPDATE Users SET Role = ? WHERE Id = ?",
            new_role,
            target_user_id_uuid,
        )
        conn.commit()

        # Same UserSessions revocation as delete_user()'s own -- see
        # that function's own comment on binding target_user_id (the
        # original string) rather than target_user_id_uuid here.
        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE UserId = ? AND RevokedAt IS NULL",
            _to_dto_string(datetime.now(timezone.utc)),
            target_user_id,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return {"userId": str(target_user_id_uuid), "role": new_role, "customerId": target_customer_id}
