"""Tests for shared/users_management_api.py"""

import uuid
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from shared import auth_utils, users_management_api
from shared.email_client import EmailSendError


def _uuid() -> uuid.UUID:
    """A fresh, real uuid.UUID -- matching what Users.Id/ResetToken
    (both uniqueidentifier columns in the real schema) actually round-
    trip as, unlike an arbitrary placeholder string like "user1"."""
    return uuid.uuid4()


class _Ctx:
    def __init__(
        self, role, customer_id=None, user_id="11111111-1111-1111-1111-111111111111", session_id="sess1"
    ):
        self.role = role
        self.customer_id = customer_id
        self.user_id = user_id
        self.session_id = session_id


STREETLEAF_ADMIN = _Ctx("Streetleaf Admin")
CUSTOMER_ADMIN = _Ctx("Customer Admin", customer_id="cust1")
USER_ROLE = _Ctx("User", customer_id="cust1")


class TestNormalizeCustomerId:
    def test_none_stays_none(self):
        assert users_management_api._normalize_customer_id(None) is None

    def test_empty_string_becomes_none(self):
        assert users_management_api._normalize_customer_id("") is None

    def test_whitespace_only_becomes_none(self):
        assert users_management_api._normalize_customer_id("   ") is None

    def test_literal_null_string_becomes_none_case_insensitive(self):
        for value in ("null", "NULL", "Null", "  null  "):
            assert users_management_api._normalize_customer_id(value) is None

    def test_genuine_customer_id_passes_through_unmodified(self):
        """Confirms this doesn't strip/alter valid input -- only decides
        whether it counts as missing at all."""
        assert users_management_api._normalize_customer_id("cust1") == "cust1"
        assert users_management_api._normalize_customer_id(" cust1 ") == " cust1 "


class TestInviteUser:
    def test_streetleaf_admin_inviting_user_without_customer_id_creates_a_streetleaf_user(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """The corrected understanding of the originally-reported
        scenario: a Streetleaf Admin inviting role='User' with the
        customerId key omitted entirely (function_app.py's own
        body.get("customerId") then passes None here, same as calling
        this directly without that argument at all) is a VALID,
        INTENTIONAL case -- a "Streetleaf User", with no customer
        association, the same way Streetleaf Admin itself has none. Not
        an error."""
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        users_management_api.invite_user(
            STREETLEAF_ADMIN, "Minh Tran", "minh+7@streetleaf.com", "User"
        )  # must not raise

        insert_call = mock_cursor.execute.call_args_list[-1]
        bound_customer_id = insert_call.args[5]
        assert bound_customer_id is None

    def test_empty_or_null_string_customer_id_also_creates_a_streetleaf_user(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """The additional defensive hardening: "" and the literal string
        "null" are normalized to None the same as a genuinely omitted
        customerId, so they ALSO produce a Streetleaf User -- not
        silently stored as a literal "null"/"" CustomerId value, which
        would be worse than NULL since it looks like real data but
        isn't."""
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        for customer_id_value in ("", "null", "NULL", "   "):
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Minh Tran", "minh+7@streetleaf.com", "User", customer_id_value
            )  # must not raise
            insert_call = mock_cursor.execute.call_args_list[-1]
            bound_customer_id = insert_call.args[5]
            assert bound_customer_id is None

    def test_user_role_cannot_invite_at_all(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """Blocked at the very first require_role() check -- never even
        reaches the role-being-invited logic below."""
        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.invite_user(
                USER_ROLE, "Jane Doe", "jane@example.com", "User", "cust1"
            )
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_customer_admin_cannot_invite_a_streetleaf_admin(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """The privilege-escalation guard: a Customer Admin can't create
        a role with MORE privilege than their own."""
        with pytest.raises(
            auth_utils.AuthError, match="a Customer Admin cannot invite a Streetleaf Admin"
        ) as exc_info:
            users_management_api.invite_user(
                CUSTOMER_ADMIN, "Jane Doe", "jane@example.com", "Streetleaf Admin"
            )
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_customer_admin_can_invite_customer_admin_for_their_own_customer(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        result = users_management_api.invite_user(
            CUSTOMER_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin", "cust1"
        )

        insert_call = mock_cursor.execute.call_args_list[-1]
        assert "INSERT INTO Users" in insert_call.args[0]
        assert result["email"] == "jane@example.com"

    def test_customer_admin_can_invite_user_role_for_their_own_customer(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        result = users_management_api.invite_user(
            CUSTOMER_ADMIN, "Jane Doe", "jane@example.com", "User", "cust1"
        )

        insert_call = mock_cursor.execute.call_args_list[-1]
        assert "INSERT INTO Users" in insert_call.args[0]
        assert result["email"] == "jane@example.com"

    def test_customer_admin_omitting_customer_id_defaults_to_their_own(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """A Customer Admin doesn't need to pass customerId at all --
        it's implicitly their own either way. Distinct from a Streetleaf
        Admin inviter, for whom omitting customerId on a 'User' role
        produces an intentionally UNSCOPED "Streetleaf User" instead --
        a Customer Admin caller can never produce that unscoped case,
        precisely because this forcing happens unconditionally."""
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        users_management_api.invite_user(CUSTOMER_ADMIN, "Jane Doe", "jane@example.com", "User")

        insert_call = mock_cursor.execute.call_args_list[-1]
        # INSERT INTO Users (Id, Name, Email, Role, Status, CustomerId, ...)
        # VALUES (?, ?, ?, ?, 'Pending', ?, ...) -- CustomerId is the 5th bound param.
        bound_customer_id = insert_call.args[5]
        assert bound_customer_id == "cust1"  # CUSTOMER_ADMIN's own customer_id

    def test_customer_admin_cannot_invite_for_a_different_customer(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """The actual security fix this permission model exists to
        enforce: a Customer Admin explicitly passing a DIFFERENT
        customerId than their own is rejected outright, not silently
        overridden -- without this, any Customer Admin could invite a
        Customer Admin/User for an arbitrary OTHER customer, a genuine
        cross-tenant privilege-escalation hole."""
        with pytest.raises(
            auth_utils.AuthError, match="only invite users for their own customer"
        ) as exc_info:
            users_management_api.invite_user(
                CUSTOMER_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin", "some-other-cust"
            )
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_streetleaf_admin_can_invite_all_three_roles(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        for role, customer_id in (
            ("Streetleaf Admin", None),
            ("Customer Admin", "cust1"),
            ("User", "cust1"),  # a customer-side User
            ("User", None),  # a "Streetleaf User" -- also valid
        ):
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", role, customer_id
            )  # must not raise for any of these

    def test_user_role_does_not_require_customer_id_unlike_customer_admin(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """The actual distinction this permission model draws: 'Customer
        Admin' always needs a customerId (there's no "unscoped Customer
        Admin" concept), but 'User' doesn't -- it's valid either way."""
        mock_cursor.fetchone.return_value = None
        mocker.patch("shared.users_management_api.send_email")

        users_management_api.invite_user(
            STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "User"
        )  # must not raise

        with pytest.raises(auth_utils.AuthError, match="customerId is required"):
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin"
            )

    def test_invalid_role_is_rejected(self, patch_get_connection_users_management, mock_cursor):
        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Superuser"
            )
        assert exc_info.value.status_code == 400

    def test_customer_admin_role_without_customer_id_is_rejected(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError, match="customerId is required"):
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin"
            )

    def test_streetleaf_admin_role_with_customer_id_is_rejected(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError, match="must not be given"):
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Streetleaf Admin", "cust1"
            )

    def test_existing_email_is_rejected_with_409(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)  # a matching row exists

        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.invite_user(
                STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin", "cust1"
            )
        assert exc_info.value.status_code == 409

    def test_successful_invite_inserts_pending_user_and_sends_email(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = None  # no existing user
        mock_send = mocker.patch("shared.users_management_api.send_email")

        result = users_management_api.invite_user(
            STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin", "cust1"
        )

        insert_call = mock_cursor.execute.call_args_list[-1]
        assert "INSERT INTO Users" in insert_call.args[0]
        assert "'Pending'" in insert_call.args[0]
        mock_send.assert_called_once()
        assert result["email"] == "jane@example.com"
        assert result["emailSent"] is True

    def test_email_failure_does_not_undo_the_created_user(
        self, patch_get_connection_users_management, mock_conn, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = None
        mocker.patch(
            "shared.users_management_api.send_email",
            side_effect=EmailSendError("smtp is down"),
        )

        result = users_management_api.invite_user(
            STREETLEAF_ADMIN, "Jane Doe", "jane@example.com", "Customer Admin", "cust1"
        )

        mock_conn.commit.assert_called_once()  # the INSERT was still committed
        assert result["emailSent"] is False


class TestResendInvite:
    def test_customer_admin_cannot_resend(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """Role check happens before UUID parsing -- this must be
        rejected on that basis alone, regardless of whether "user1"
        would also fail as a malformed id."""
        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.resend_invite(CUSTOMER_ADMIN, "user1")
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_malformed_user_id_is_rejected_without_querying_the_database(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.resend_invite(STREETLEAF_ADMIN, "not-a-real-uuid")
        assert exc_info.value.status_code == 400
        mock_cursor.execute.assert_not_called()

    def test_nonexistent_user_raises_404(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.resend_invite(STREETLEAF_ADMIN, str(_uuid()))
        assert exc_info.value.status_code == 404

    def test_already_active_user_is_rejected_with_409(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """The defining restriction this function has that invite_user()
        doesn't: only a CURRENTLY Pending user's invite can be resent --
        an Active user has no invite link left to refresh."""
        mock_cursor.fetchone.return_value = ("Jane Doe", "jane@example.com", "Active")

        with pytest.raises(auth_utils.AuthError, match="only a Pending user's invite can be resent"):
            users_management_api.resend_invite(STREETLEAF_ADMIN, str(_uuid()))

    def test_successful_resend_refreshes_token_and_sends_email(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = ("Jane Doe", "jane@example.com", "Pending")
        mock_send = mocker.patch("shared.users_management_api.send_email")
        target_user_id = str(_uuid())

        result = users_management_api.resend_invite(STREETLEAF_ADMIN, target_user_id)

        update_call = mock_cursor.execute.call_args_list[-1]
        assert "UPDATE Users" in update_call.args[0]
        assert "ResetToken = ?" in update_call.args[0]
        assert "ResetTokenExpiresAt = ?" in update_call.args[0]
        mock_send.assert_called_once()
        assert result["userId"] == target_user_id  # SAME id -- not a new user record
        assert result["email"] == "jane@example.com"
        assert result["emailSent"] is True

    def test_resend_does_not_touch_name_email_role_or_customer_id(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """Deliberately out of scope for this function -- only
        ResetToken/ResetTokenExpiresAt are refreshed."""
        mock_cursor.fetchone.return_value = ("Jane Doe", "jane@example.com", "Pending")
        mocker.patch("shared.users_management_api.send_email")

        users_management_api.resend_invite(STREETLEAF_ADMIN, str(_uuid()))

        update_call = mock_cursor.execute.call_args_list[-1]
        update_sql = update_call.args[0]
        assert "Name" not in update_sql
        assert "Email" not in update_sql
        assert "Role" not in update_sql
        assert "CustomerId" not in update_sql

    def test_does_not_create_a_new_users_row(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """The whole point of this function existing, versus the
        delete_user()-then-invite_user() workaround it replaces: no
        INSERT anywhere in the call sequence."""
        mock_cursor.fetchone.return_value = ("Jane Doe", "jane@example.com", "Pending")
        mocker.patch("shared.users_management_api.send_email")

        users_management_api.resend_invite(STREETLEAF_ADMIN, str(_uuid()))

        calls = mock_cursor.execute.call_args_list
        assert not any("INSERT" in c.args[0].upper() for c in calls)

    def test_email_failure_does_not_undo_the_token_refresh(
        self, patch_get_connection_users_management, mock_conn, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = ("Jane Doe", "jane@example.com", "Pending")
        mocker.patch(
            "shared.users_management_api.send_email",
            side_effect=EmailSendError("smtp is down"),
        )

        result = users_management_api.resend_invite(STREETLEAF_ADMIN, str(_uuid()))

        mock_conn.commit.assert_called_once()  # the token UPDATE was still committed
        assert result["emailSent"] is False


class TestRegisterUser:
    def test_malformed_token_is_rejected_without_querying_the_database(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """_parse_uuid() must catch this before it ever reaches a query
        -- this is the exact bug this whole fix was for: a plain,
        non-UUID string bound into a uniqueidentifier column raises a
        raw SQL conversion error, not a clean 400."""
        with pytest.raises(auth_utils.AuthError, match="invalid or expired invite link"):
            users_management_api.register_user("not-a-real-uuid", "newpassword123")
        mock_cursor.execute.assert_not_called()

    def test_wellformed_but_nonexistent_token_is_rejected(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(auth_utils.AuthError, match="invalid or expired invite link"):
            users_management_api.register_user(str(_uuid()), "newpassword123")

    def test_valid_token_activates_user_and_returns_a_session(
        self, patch_get_connection_users_management, mock_cursor
    ):
        user_id = _uuid()
        mock_cursor.fetchone.return_value = (user_id, "Customer Admin", "cust1", "Jane Doe", "jane@example.com")

        result = users_management_api.register_user(str(_uuid()), "newpassword123")

        update_call = mock_cursor.execute.call_args_list[-2]  # session insert is last
        assert "UPDATE Users" in update_call.args[0]
        assert "'Active'" in update_call.args[0]
        assert result["user"]["id"] == str(user_id)
        assert result["user"]["role"] == "Customer Admin"
        payload = pyjwt.decode(result["token"], "test-jwt-secret", algorithms=["HS256"])
        assert payload["sub"] == str(user_id)

    def test_password_is_hashed_not_stored_plain(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (_uuid(), "Customer Admin", "cust1", "Jane Doe", "jane@example.com")

        users_management_api.register_user(str(_uuid()), "newpassword123")

        update_call = mock_cursor.execute.call_args_list[-2]
        bound_password_hash = update_call.args[1]
        assert bound_password_hash != "newpassword123"
        assert bound_password_hash.startswith("$2b$")


class TestSignIn:
    def test_nonexistent_email_raises_generic_error(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(auth_utils.AuthError, match="invalid email or password"):
            users_management_api.sign_in("nobody@example.com", "whatever")

    def test_wrong_password_raises_the_same_generic_error(
        self, patch_get_connection_users_management, mock_cursor
    ):
        correct_hash = auth_utils.hash_password("correctpassword")
        mock_cursor.fetchone.return_value = ("user1", "Jane Doe", "Customer Admin", "Active", "cust1", correct_hash)

        with pytest.raises(auth_utils.AuthError, match="invalid email or password"):
            users_management_api.sign_in("jane@example.com", "wrongpassword")

    def test_pending_status_raises_the_same_generic_error(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """A user who was invited but never completed registration has
        no real password to check against -- must fail the same way a
        wrong password would, not with a different, more revealing
        message."""
        correct_hash = auth_utils.hash_password("correctpassword")
        mock_cursor.fetchone.return_value = ("user1", "Jane Doe", "Customer Admin", "Pending", "cust1", correct_hash)

        with pytest.raises(auth_utils.AuthError, match="invalid email or password"):
            users_management_api.sign_in("jane@example.com", "correctpassword")

    def test_deactivated_status_raises_the_same_generic_error(
        self, patch_get_connection_users_management, mock_cursor
    ):
        correct_hash = auth_utils.hash_password("correctpassword")
        mock_cursor.fetchone.return_value = ("user1", "Jane Doe", "Customer Admin", "Deactivated", "cust1", correct_hash)

        with pytest.raises(auth_utils.AuthError, match="invalid email or password"):
            users_management_api.sign_in("jane@example.com", "correctpassword")

    def test_correct_credentials_return_a_valid_session_token(
        self, patch_get_connection_users_management, mock_cursor
    ):
        correct_hash = auth_utils.hash_password("correctpassword")
        mock_cursor.fetchone.return_value = ("user1", "Jane Doe", "Customer Admin", "Active", "cust1", correct_hash)

        result = users_management_api.sign_in("jane@example.com", "correctpassword")

        payload = pyjwt.decode(result["token"], "test-jwt-secret", algorithms=["HS256"])
        assert payload["sub"] == "user1"
        assert payload["role"] == "Customer Admin"
        assert result["user"]["email"] == "jane@example.com"


class TestSignOut:
    def test_revokes_the_callers_own_session(
        self, patch_get_connection_users_management, mock_conn, mock_cursor
    ):
        ctx = _Ctx("Customer Admin", session_id="the-session-id")

        users_management_api.sign_out(ctx)

        sql, revoked_at, session_id = mock_cursor.execute.call_args.args
        assert "UPDATE UserSessions" in sql
        assert "RevokedAt IS NULL" in sql
        assert session_id == "the-session-id"
        mock_conn.commit.assert_called_once()


class TestForgotPassword:
    def test_nonexistent_email_does_not_raise(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = None
        mock_send = mocker.patch("shared.users_management_api.send_email")

        users_management_api.forgot_password("nobody@example.com")  # must not raise

        mock_send.assert_not_called()

    def test_existing_active_user_gets_a_reset_token_and_an_email(
        self, patch_get_connection_users_management, mock_conn, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = ("user1", "Jane Doe")
        mock_send = mocker.patch("shared.users_management_api.send_email")

        users_management_api.forgot_password("jane@example.com")

        update_call = mock_cursor.execute.call_args_list[-1]
        assert "UPDATE Users" in update_call.args[0]
        assert "ResetToken" in update_call.args[0]
        mock_conn.commit.assert_called_once()
        mock_send.assert_called_once()

    def test_email_failure_does_not_raise_either(
        self, patch_get_connection_users_management, mock_cursor, mocker
    ):
        """Must behave identically (no exception) whether the email
        succeeds or fails -- an outward difference here would leak
        whether the email existed, defeating the anti-enumeration point."""
        mock_cursor.fetchone.return_value = ("user1", "Jane Doe")
        mocker.patch(
            "shared.users_management_api.send_email",
            side_effect=EmailSendError("smtp is down"),
        )

        users_management_api.forgot_password("jane@example.com")  # must not raise


class TestResetPassword:
    def test_malformed_token_is_rejected_without_querying_the_database(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError, match="invalid or expired reset link"):
            users_management_api.reset_password("not-a-real-uuid", "newpassword123")
        mock_cursor.execute.assert_not_called()

    def test_wellformed_but_nonexistent_token_is_rejected(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(auth_utils.AuthError, match="invalid or expired reset link"):
            users_management_api.reset_password(str(_uuid()), "newpassword123")

    def test_valid_token_updates_password_and_revokes_existing_sessions(
        self, patch_get_connection_users_management, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (_uuid(),)

        users_management_api.reset_password(str(_uuid()), "newpassword123")

        calls = mock_cursor.execute.call_args_list
        assert any("UPDATE Users" in c.args[0] and "PasswordHash" in c.args[0] for c in calls)
        assert any("UPDATE UserSessions" in c.args[0] for c in calls)
        mock_conn.commit.assert_called_once()

    def test_user_sessions_update_binds_a_string_not_a_uuid_object(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """UserSessions.UserId is VARCHAR, unlike Users.Id -- binding the
        raw uuid.UUID object here (rather than its string form) would
        risk the same kind of type-mismatch this whole fix was for, just
        in the opposite direction."""
        user_id = _uuid()
        mock_cursor.fetchone.return_value = (user_id,)

        users_management_api.reset_password(str(_uuid()), "newpassword123")

        sessions_call = next(
            c for c in mock_cursor.execute.call_args_list if "UPDATE UserSessions" in c.args[0]
        )
        bound_user_id = sessions_call.args[2]
        assert isinstance(bound_user_id, str)
        assert bound_user_id == str(user_id)

    def test_new_password_is_hashed_not_stored_plain(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (_uuid(),)

        users_management_api.reset_password(str(_uuid()), "newpassword123")

        users_update_call = next(
            c for c in mock_cursor.execute.call_args_list if "UPDATE Users" in c.args[0]
        )
        bound_password_hash = users_update_call.args[1]
        assert bound_password_hash != "newpassword123"
        assert bound_password_hash.startswith("$2b$")


class TestDeleteUser:
    def test_user_role_cannot_delete_at_all(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.delete_user(USER_ROLE, str(_uuid()))
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_malformed_user_id_is_rejected_without_querying_the_database(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.delete_user(STREETLEAF_ADMIN, "not-a-real-uuid")
        assert exc_info.value.status_code == 400
        mock_cursor.execute.assert_not_called()

    def test_streetleaf_admin_cannot_delete_itself(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """Checked before any database round trip -- a pure comparison
        of already-known values, so no query should even run."""
        with pytest.raises(auth_utils.AuthError, match="cannot delete your own account") as exc_info:
            users_management_api.delete_user(STREETLEAF_ADMIN, STREETLEAF_ADMIN.user_id)
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_customer_admin_cannot_delete_itself(
        self, patch_get_connection_users_management, mock_cursor
    ):
        with pytest.raises(auth_utils.AuthError, match="cannot delete your own account") as exc_info:
            users_management_api.delete_user(CUSTOMER_ADMIN, CUSTOMER_ADMIN.user_id)
        assert exc_info.value.status_code == 403
        mock_cursor.execute.assert_not_called()

    def test_streetleaf_admin_can_delete_another_streetleaf_admin(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("Streetleaf Admin", None)
        mock_cursor.rowcount = 1

        users_management_api.delete_user(STREETLEAF_ADMIN, str(_uuid()))  # must not raise

    def test_streetleaf_admin_can_delete_any_customer_admin(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("Customer Admin", "some-other-customer")
        mock_cursor.rowcount = 1

        users_management_api.delete_user(STREETLEAF_ADMIN, str(_uuid()))  # must not raise

    def test_streetleaf_admin_can_delete_any_user(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("User", "some-other-customer")
        mock_cursor.rowcount = 1

        users_management_api.delete_user(STREETLEAF_ADMIN, str(_uuid()))  # must not raise

    def test_customer_admin_cannot_delete_a_streetleaf_admin(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("Streetleaf Admin", None)

        with pytest.raises(
            auth_utils.AuthError, match="a Customer Admin cannot delete a Streetleaf Admin"
        ) as exc_info:
            users_management_api.delete_user(CUSTOMER_ADMIN, str(_uuid()))
        assert exc_info.value.status_code == 403

    def test_customer_admin_can_delete_customer_admin_of_own_customer(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("Customer Admin", CUSTOMER_ADMIN.customer_id)
        mock_cursor.rowcount = 1

        users_management_api.delete_user(CUSTOMER_ADMIN, str(_uuid()))  # must not raise

    def test_customer_admin_cannot_delete_customer_admin_of_other_customer(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("Customer Admin", "some-other-customer")

        with pytest.raises(
            auth_utils.AuthError, match="only delete users for their own customer"
        ) as exc_info:
            users_management_api.delete_user(CUSTOMER_ADMIN, str(_uuid()))
        assert exc_info.value.status_code == 403

    def test_customer_admin_can_delete_user_of_own_customer(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("User", CUSTOMER_ADMIN.customer_id)
        mock_cursor.rowcount = 1

        users_management_api.delete_user(CUSTOMER_ADMIN, str(_uuid()))  # must not raise

    def test_customer_admin_cannot_delete_user_of_other_customer(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("User", "some-other-customer")

        with pytest.raises(
            auth_utils.AuthError, match="only delete users for their own customer"
        ) as exc_info:
            users_management_api.delete_user(CUSTOMER_ADMIN, str(_uuid()))
        assert exc_info.value.status_code == 403

    def test_wellformed_but_nonexistent_user_raises_404(
        self, patch_get_connection_users_management, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(auth_utils.AuthError) as exc_info:
            users_management_api.delete_user(STREETLEAF_ADMIN, str(_uuid()))
        assert exc_info.value.status_code == 404

    def test_successful_delete_removes_user_and_revokes_sessions(
        self, patch_get_connection_users_management, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = ("User", "cust1")
        mock_cursor.rowcount = 1

        users_management_api.delete_user(STREETLEAF_ADMIN, str(_uuid()))

        calls = mock_cursor.execute.call_args_list
        assert any(c.args[0].strip().upper().startswith("DELETE FROM USERS") for c in calls)
        assert any("UPDATE UserSessions" in c.args[0] for c in calls)
        assert mock_conn.commit.call_count == 2  # once for the DELETE, once for the session revoke

    def test_user_sessions_update_binds_the_original_string_not_a_uuid_object(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """UserSessions.UserId is VARCHAR -- the incoming string form of
        target_user_id belongs there, not the uuid.UUID object parsed
        out for the Users-table DELETE just above it."""
        mock_cursor.fetchone.return_value = ("User", "cust1")
        mock_cursor.rowcount = 1
        target_user_id = str(_uuid())

        users_management_api.delete_user(STREETLEAF_ADMIN, target_user_id)

        sessions_call = next(
            c for c in mock_cursor.execute.call_args_list if "UPDATE UserSessions" in c.args[0]
        )
        bound_user_id = sessions_call.args[2]
        assert bound_user_id == target_user_id
        assert isinstance(bound_user_id, str)

    def test_delete_issues_a_real_delete_not_a_status_update(
        self, patch_get_connection_users_management, mock_cursor
    ):
        """Changed from a soft deactivation to a hard delete per
        explicit request -- confirms the Users-table statement is a
        genuine DELETE, not an UPDATE ... SET Status = 'Deactivated'
        (the previous design this replaced)."""
        mock_cursor.fetchone.return_value = ("User", "cust1")
        mock_cursor.rowcount = 1

        users_management_api.delete_user(STREETLEAF_ADMIN, str(_uuid()))

        calls = mock_cursor.execute.call_args_list
        assert not any("Deactivated" in c.args[0] for c in calls)
        assert not any(
            "UPDATE Users" in c.args[0] and "Status" in c.args[0] for c in calls
        )
