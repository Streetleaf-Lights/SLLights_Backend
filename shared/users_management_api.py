"""
The ten user-management operations: invite, resend invite, register,
sign in, sign out, forgot password, reset password, delete, change role,
and transfer ownership (Customer Owner → another user).

Role model, four roles in descending order of privilege:
  'Streetleaf Admin'  -- broad access, not scoped to one customer
  'Customer Owner'    -- highest customer-scoped role; exactly one per
                         customer at any time; can act on Customer Admin
                         and User within their customer
  'Customer Admin'    -- restricted to their own CustomerId's data; can
                         act on User within their customer only
  'User'              -- restricted to their own CustomerId's data, with
                         narrower permissions than Customer Admin

Invite permissions:
  Streetleaf Admin    can invite any role
  Customer Owner      can invite Customer Admin / User for their own customer
  Customer Admin      can invite Customer Admin / User for their own customer
  User                cannot invite at all

Delete / change-role permissions:
  Streetleaf Admin    can act on anyone except themselves
  Customer Owner      can act on Customer Admin / User in their own customer
  Customer Admin      can act on User in their own customer only
                      (cannot act on Customer Owner or Streetleaf Admin)
  User                cannot act on anyone

Transfer ownership (Customer Owner only):
  Initiates a pending transfer to another user in their own customer.
  The nominee receives an email; on acceptance their role becomes
  'Customer Owner' and the previous owner is deleted.

Anti-enumeration note: sign_in() and forgot_password() deliberately
don't reveal whether a given email exists -- wrong password and
nonexistent email produce the same error. This does NOT apply to
invite/resend/delete/change-role, which are admin-only endpoints where
specific errors are appropriate.
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

_VALID_ROLES = ("Streetleaf Admin", "Customer Owner", "Customer Admin", "User")

# Roles that are scoped to a specific customer (vs. Streetleaf-wide)
_CUSTOMER_SCOPED_ROLES = ("Customer Owner", "Customer Admin", "User")

# Role precedence for permission checks -- higher index = higher privilege
_ROLE_RANK = {
    "User": 0,
    "Customer Admin": 1,
    "Customer Owner": 2,
    "Streetleaf Admin": 3,
}


def _rank(role: str) -> int:
    return _ROLE_RANK.get(role, -1)


def _parse_uuid(value, error_message: str) -> uuid.UUID:
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


def _ownership_transfer_link(token: str) -> str:
    base_url = os.environ.get("APP_BASE_URL", "").rstrip("/")
    return f"{base_url}/accept-ownership?token={token}"


def _normalize_customer_id(customer_id) -> str | None:
    if customer_id is None:
        return None
    s = str(customer_id)
    if s.strip().lower() in ("", "null", "none"):
        return None
    return s


def _send_invite_email(user_id, name: str, email: str, token: str, operation_name: str) -> bool:
    link = _registration_link(str(token))
    try:
        send_email(
            email,
            "You've been invited to LightsApp",
            (
                f"<p>You've been invited to LightsApp. "
                f"<a href='{link}'>Click here to set up your account.</a></p>"
                f"<p>This link expires in {TOKEN_LIFETIME.days} day(s).</p>"
            ),
        )
        return True
    except (EmailSendError, Exception) as ex:
        logging.error("%s: user %s's invite email failed to send: %s", operation_name, user_id, ex)
        return False


def _send_ownership_transfer_email(
    nominee_id, nominee_name: str, nominee_email: str, token: str
) -> bool:
    link = _ownership_transfer_link(str(token))
    try:
        send_email(
            nominee_email,
            "LightsApp ownership transfer",
            (
                f"<p>Hi {nominee_name},</p>"
                f"<p>You have been invited to become the Customer Owner "
                f"for your organisation. "
                f"<a href='{link}'>Click here to accept.</a></p>"
                f"<p>This link expires in {TOKEN_LIFETIME.days} day(s).</p>"
            ),
        )
        return True
    except (EmailSendError, Exception) as ex:
        logging.error(
            "initiate_ownership_transfer: nominee %s's email failed to send: %s",
            nominee_id, ex,
        )
        return False


def invite_user(
    inviter: AuthContext, name: str, email: str, role: str, customer_id: str = None
) -> dict:
    """
    Creates a Pending user and emails an invite link.

    Streetleaf Admin can invite any role.
    Customer Owner / Customer Admin can invite Customer Admin / User for
    their own customer only.
    User cannot invite at all.

    Only one Customer Owner is permitted per customer -- inviting a second
    one raises 409.
    """
    require_role(inviter, ["Streetleaf Admin", "Customer Owner", "Customer Admin"])
    customer_id = _normalize_customer_id(customer_id)

    if role not in _VALID_ROLES:
        raise AuthError(f"role must be one of: {', '.join(_VALID_ROLES)}", status_code=400)

    # Customer Owner and Customer Admin: can only invite within their own
    # customer and cannot invite roles with equal or higher privilege.
    if inviter.role in ("Customer Owner", "Customer Admin"):
        # Customer Owner can invite Customer Admin, User, or Customer Owner
        # (inviting a new Customer Owner auto-initiates a transfer).
        # Customer Admin can only invite Customer Admin or User.
        allowed = ("Customer Admin", "User") if inviter.role == "Customer Admin" else ("Customer Owner", "Customer Admin", "User")
        if role not in allowed:
            raise AuthError(
                f"a {inviter.role} can only invite a {' or '.join(allowed)}",
                status_code=403,
            )
        if customer_id and customer_id != inviter.customer_id:
            raise AuthError(
                f"a {inviter.role} can only invite users for their own customer",
                status_code=403,
            )
        customer_id = inviter.customer_id

    if role == "Customer Owner" and not customer_id:
        raise AuthError("customerId is required when role is 'Customer Owner'", status_code=400)
    if role == "Customer Admin" and not customer_id:
        raise AuthError("customerId is required when role is 'Customer Admin'", status_code=400)
    if role == "Streetleaf Admin" and customer_id:
        raise AuthError("customerId must not be given when role is 'Streetleaf Admin'", status_code=400)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM Users WHERE Email = ?", email)
        if cursor.fetchone() is not None:
            raise AuthError("a user with this email already exists", status_code=409)

        # If inviting a Customer Owner and one already exists, treat this as
        # initiating an ownership transfer rather than an error. The new user
        # is created as Pending with the existing owner's Id stored on their
        # row; when they register (set their password), the old owner is
        # deleted automatically as part of that same transaction.
        existing_owner_id = None
        if role == "Customer Owner":
            cursor.execute(
                "SELECT Id FROM Users WHERE Role = 'Customer Owner' AND CustomerId = ?",
                customer_id,
            )
            row = cursor.fetchone()
            if row is not None:
                existing_owner_id = row[0]

        user_id = uuid.uuid4()
        token = generate_token()
        expires_at = datetime.now(timezone.utc) + TOKEN_LIFETIME

        cursor.execute(
            """
            INSERT INTO Users (
                Id, Name, Email, Role, Status, CustomerId,
                PasswordHash, ResetToken, ResetTokenExpiresAt,
                OwnershipTransferFromUserId
            )
            VALUES (?, ?, ?, ?, 'Pending', ?, NULL, ?, ?, ?)
            """,
            user_id,
            name,
            email,
            role,
            customer_id,
            token,
            _to_dto_string(expires_at),
            str(existing_owner_id) if existing_owner_id is not None else None,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    is_transfer = existing_owner_id is not None
    if is_transfer:
        # Send the ownership transfer email so the nominee understands
        # what they're accepting, not a generic "you've been invited" email.
        email_sent = _send_ownership_transfer_email(
            user_id, name, email, token
        )
    else:
        email_sent = _send_invite_email(user_id, name, email, token, "invite_user")

    result = {"userId": str(user_id), "email": email, "emailSent": email_sent}
    if is_transfer:
        result["ownershipTransfer"] = True
        result["message"] = (
            "An ownership transfer has been initiated. "
            "The nominee will receive an email to set up their account and accept ownership. "
            "The current owner will be removed when the nominee accepts."
        )
    return result


def resend_invite(caller: AuthContext, target_user_id: str) -> dict:
    """
    Re-sends an invite email to an existing Pending user, refreshing their
    token in place. Streetleaf Admin only.
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
            "UPDATE Users SET ResetToken = ?, ResetTokenExpiresAt = ? WHERE Id = ?",
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
    Completes an invited user's setup: verifies the token, sets password,
    activates the account, and returns a session immediately.

    If the registering user is a Pending Customer Owner with an
    OwnershipTransferFromUserId set (invited via invite_user when an owner
    already existed), the old owner is deleted in the same transaction so
    there is never more than one Customer Owner per customer.
    """
    token_uuid = _parse_uuid(token, "invalid or expired invite link")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT Id, Role, CustomerId, Name, Email, OwnershipTransferFromUserId
            FROM Users
            WHERE ResetToken = ? AND Status = 'Pending' AND ResetTokenExpiresAt > SYSDATETIMEOFFSET()
            """,
            token_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("invalid or expired invite link", status_code=400)

        user_id, role, customer_id, name, email, previous_owner_id_str = row
        password_hash = hash_password(password)

        cursor.execute(
            """
            UPDATE Users
            SET PasswordHash = ?, Status = 'Active',
                ResetToken = NULL, ResetTokenExpiresAt = NULL,
                OwnershipTransferFromUserId = NULL
            WHERE Id = ?
            """,
            password_hash,
            user_id,
        )

        # If this registration completes an ownership transfer, delete the
        # previous owner and revoke their sessions atomically.
        if previous_owner_id_str:
            cursor.execute(
                "DELETE FROM Users WHERE Id = ?",
                previous_owner_id_str,
            )
            cursor.execute(
                "UPDATE UserSessions SET RevokedAt = ? WHERE UserId = ? AND RevokedAt IS NULL",
                _to_dto_string(datetime.now(timezone.utc)),
                previous_owner_id_str,
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


def sign_in(email: str, password: str, impersonate_customer_id: str = None) -> dict:
    """
    Verifies email/password and, if valid, creates a new session.
    Deliberately generic error for every failure mode.

    Impersonation (debugging only): if impersonate_customer_id is supplied
    and the signing-in user is a Streetleaf Admin, the session is created
    with role 'Customer Owner' scoped to that customer instead of their
    actual role. The JWT still carries their real user Id so audit trails
    are intact. Non-Streetleaf-Admin users cannot use this parameter.
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
            raise AuthError("invalid email or password", status_code=401)

        user_id, name, role, status, customer_id, password_hash = row
        if status != "Active":
            raise AuthError("invalid email or password", status_code=401)
        if not verify_password(password, password_hash):
            raise AuthError("invalid email or password", status_code=401)

        session_role = role
        session_customer_id = customer_id

        if impersonate_customer_id:
            if role != "Streetleaf Admin":
                raise AuthError(
                    "only a Streetleaf Admin can sign in with a customerId",
                    status_code=403,
                )
            cursor.execute(
                "SELECT 1 FROM Customers WHERE Id = ?",
                impersonate_customer_id,
            )
            if cursor.fetchone() is None:
                raise AuthError(
                    "customerId does not match any customer",
                    status_code=404,
                )
            session_role = "Customer Owner"
            session_customer_id = impersonate_customer_id

        user_id_str = str(user_id)
        session_token = create_session(cursor, user_id_str, session_role, session_customer_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return {
        "token": session_token,
        "user": {
            "id": user_id_str,
            "name": name,
            "email": email,
            "role": session_role,
            "customerId": session_customer_id,
        },
    }


def sign_out(caller: AuthContext) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE Id = ? AND RevokedAt IS NULL",
            _to_dto_string(datetime.now(timezone.utc)),
            caller.session_id,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def forgot_password(email: str) -> None:
    """
    Sends a password-reset link if the email exists and is Active.
    Deliberately no error for a nonexistent email -- see module docstring.
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

    link = _reset_link(str(token))
    try:
        send_email(
            email,
            "Reset your LightsApp password",
            f"<p>Hi {name}, <a href='{link}'>click here to reset your password</a>. "
                 f"This link expires in {TOKEN_LIFETIME.days} day(s).</p>",
        )
    except (EmailSendError, Exception) as ex:
        logging.error("forgot_password: reset email for %s failed to send: %s", email, ex)


def reset_password(token: str, new_password: str) -> None:
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

        user_id = row[0]
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
    Permanently removes a user and revokes their active sessions.

    Streetleaf Admin  can delete anyone except themselves.
    Customer Owner    can delete Customer Admin / User in their own customer.
    Customer Admin    can delete User in their own customer only.
    User              cannot delete anyone.

    No caller can delete a user with equal or higher privilege
    (Customer Admin cannot delete Customer Owner; Customer Owner cannot
    delete Streetleaf Admin). No caller can self-delete.
    """
    require_role(caller, ["Streetleaf Admin", "Customer Owner", "Customer Admin"])
    target_user_id_uuid = _parse_uuid(target_user_id, "user not found")

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

        if caller.role != "Streetleaf Admin":
            if target_role == "Streetleaf Admin":
                raise AuthError(
                    f"a {caller.role} cannot delete a Streetleaf Admin",
                    status_code=403,
                )
            if caller.role == "Customer Admin" and target_role == "Customer Owner":
                raise AuthError(
                    "a Customer Admin cannot delete a Customer Owner",
                    status_code=403,
                )
            if target_customer_id != caller.customer_id:
                raise AuthError(
                    f"a {caller.role} can only delete users for their own customer",
                    status_code=403,
                )

        cursor.execute("DELETE FROM Users WHERE Id = ?", target_user_id_uuid)
        conn.commit()

        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE UserId = ? AND RevokedAt IS NULL",
            _to_dto_string(datetime.now(timezone.utc)),
            target_user_id,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _new_role_after_toggle(current_role: str, customer_id) -> str:
    """
    Toggles between an Admin role and 'User' within the same organisation.
    Customer Owner is not toggle-able -- use transfer_ownership() instead.
    """
    if current_role == "Customer Owner":
        raise AuthError(
            "Customer Owner role cannot be toggled; use transfer_ownership() instead",
            status_code=400,
        )
    if current_role == "User":
        return "Streetleaf Admin" if customer_id is None else "Customer Admin"
    return "User"


def change_role(caller: AuthContext, target_user_id: str) -> dict:
    """
    Toggles a user's role between an Admin role and 'User', keeping them
    in the same organisation. Customer Owner role cannot be toggled --
    use transfer_ownership() instead.

    Streetleaf Admin  can change anyone except themselves.
    Customer Owner    can change Customer Admin / User in their own customer.
    Customer Admin    can change User in their own customer only.
    User              cannot change anyone.

    No caller can change a user with equal or higher privilege.
    Revokes the target's active sessions so the new role takes effect
    immediately.
    """
    require_role(caller, ["Streetleaf Admin", "Customer Owner", "Customer Admin"])
    target_user_id_uuid = _parse_uuid(target_user_id, "user not found")

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

        if caller.role != "Streetleaf Admin":
            if target_role == "Streetleaf Admin":
                raise AuthError(
                    f"a {caller.role} cannot change the role of a Streetleaf Admin",
                    status_code=403,
                )
            if caller.role == "Customer Admin" and target_role == "Customer Owner":
                raise AuthError(
                    "a Customer Admin cannot change the role of a Customer Owner",
                    status_code=403,
                )
            if target_customer_id != caller.customer_id:
                raise AuthError(
                    f"a {caller.role} can only change the role of users for their own customer",
                    status_code=403,
                )

        new_role = _new_role_after_toggle(target_role, target_customer_id)

        cursor.execute(
            "UPDATE Users SET Role = ? WHERE Id = ?",
            new_role,
            target_user_id_uuid,
        )
        conn.commit()

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


def initiate_ownership_transfer(caller: AuthContext, nominee_user_id: str) -> dict:
    """
    Starts an ownership transfer from the current Customer Owner to a
    nominee within the same customer. The nominee receives an email with
    an acceptance link; the caller's account is NOT deleted until the
    nominee accepts.

    Only a Customer Owner can initiate. The nominee must be an Active
    Customer Admin or User in the same customer -- pending users and users
    from other customers are rejected. The caller cannot nominate
    themselves.

    Generates a transfer token stored on the NOMINEE's ResetToken/
    ResetTokenExpiresAt columns (reusing the existing token machinery).
    An existing pending transfer for this nominee is overwritten (fresh
    link, same nominee).
    """
    require_role(caller, ["Customer Owner"])
    nominee_uuid = _parse_uuid(nominee_user_id, "nominee not found")

    if nominee_uuid == uuid.UUID(caller.user_id):
        raise AuthError("cannot transfer ownership to yourself", status_code=400)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Name, Email, Role, Status, CustomerId FROM Users WHERE Id = ?",
            nominee_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError("nominee not found", status_code=404)

        nominee_name, nominee_email, nominee_role, nominee_status, nominee_customer_id = row

        if nominee_status != "Active":
            raise AuthError("nominee must be an Active user", status_code=400)
        if nominee_customer_id != caller.customer_id:
            raise AuthError(
                "nominee must belong to the same customer", status_code=403
            )
        if nominee_role not in ("Customer Admin", "User"):
            raise AuthError(
                "nominee must be a Customer Admin or User", status_code=400
            )

        token = generate_token()
        expires_at = datetime.now(timezone.utc) + TOKEN_LIFETIME

        # Store the transfer token on the nominee (not the caller) --
        # the nominee is the one who needs to act on it.
        # OwnershipTransferFromUserId is a separate column so it doesn't
        # collide with password-reset tokens. If that column isn't in the
        # schema yet, fall back to a ResetToken-based approach here and
        # update the schema via migration.
        cursor.execute(
            """
            UPDATE Users
            SET OwnershipTransferToken = ?,
                OwnershipTransferTokenExpiresAt = ?,
                OwnershipTransferFromUserId = ?
            WHERE Id = ?
            """,
            token,
            _to_dto_string(expires_at),
            caller.user_id,
            nominee_uuid,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    email_sent = _send_ownership_transfer_email(
        nominee_uuid, nominee_name, nominee_email, token
    )
    return {"nomineeUserId": str(nominee_uuid), "email": nominee_email, "emailSent": email_sent}


def accept_ownership_transfer(token: str) -> dict:
    """
    Completes an ownership transfer: validates the token on the nominee's
    record, promotes the nominee to Customer Owner, and deletes the
    previous owner. The nominee is signed in immediately.

    The previous owner's sessions are revoked as part of the delete.
    The nominee's existing sessions are also revoked so they get a fresh
    session reflecting the new role.
    """
    token_uuid = _parse_uuid(token, "invalid or expired ownership transfer link")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Find the nominee by their transfer token
        cursor.execute(
            """
            SELECT Id, Name, Email, Role, CustomerId, OwnershipTransferFromUserId
            FROM Users
            WHERE OwnershipTransferToken = ?
              AND Status = 'Active'
              AND OwnershipTransferTokenExpiresAt > SYSDATETIMEOFFSET()
            """,
            token_uuid,
        )
        row = cursor.fetchone()
        if row is None:
            raise AuthError(
                "invalid or expired ownership transfer link", status_code=400
            )

        nominee_id, nominee_name, nominee_email, _, customer_id, previous_owner_id_str = row
        previous_owner_uuid = uuid.UUID(previous_owner_id_str)

        # Promote nominee to Customer Owner and clear the transfer token
        cursor.execute(
            """
            UPDATE Users
            SET Role = 'Customer Owner',
                OwnershipTransferToken = NULL,
                OwnershipTransferTokenExpiresAt = NULL,
                OwnershipTransferFromUserId = NULL
            WHERE Id = ?
            """,
            nominee_id,
        )

        # Delete the previous owner
        cursor.execute(
            "DELETE FROM Users WHERE Id = ?",
            previous_owner_uuid,
        )

        # Revoke all sessions for both (nominee gets a fresh one below)
        now_str = _to_dto_string(datetime.now(timezone.utc))
        cursor.execute(
            "UPDATE UserSessions SET RevokedAt = ? WHERE UserId IN (?, ?) AND RevokedAt IS NULL",
            now_str,
            str(nominee_id),
            str(previous_owner_uuid),
        )

        user_id_str = str(nominee_id)
        session_token = create_session(cursor, user_id_str, "Customer Owner", customer_id)
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    return {
        "token": session_token,
        "user": {
            "id": user_id_str,
            "name": nominee_name,
            "email": nominee_email,
            "role": "Customer Owner",
            "customerId": customer_id,
        },
    }
