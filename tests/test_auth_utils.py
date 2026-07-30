"""Tests for shared/auth_utils.py"""

from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from shared import auth_utils


class TestHashAndVerifyPassword:
    def test_hash_is_not_the_plaintext_password(self):
        h = auth_utils.hash_password("correcthorsebatterystaple")
        assert h != "correcthorsebatterystaple"

    def test_hash_uses_bcrypt(self):
        """A real, deliberately-slow, salted algorithm -- not a fast
        general-purpose hash like SHA-256, which is unsuitable for
        password storage precisely because it's fast."""
        h = auth_utils.hash_password("correcthorsebatterystaple")
        assert h.startswith("$2b$")

    def test_same_password_hashed_twice_produces_different_hashes(self):
        """Confirms a real per-hash salt is in play, not a fixed/no salt
        -- otherwise two users with the same password would have
        identical PasswordHash values, which a breach would reveal."""
        h1 = auth_utils.hash_password("correcthorsebatterystaple")
        h2 = auth_utils.hash_password("correcthorsebatterystaple")
        assert h1 != h2

    def test_verify_correct_password_returns_true(self):
        h = auth_utils.hash_password("correcthorsebatterystaple")
        assert auth_utils.verify_password("correcthorsebatterystaple", h) is True

    def test_verify_wrong_password_returns_false(self):
        h = auth_utils.hash_password("correcthorsebatterystaple")
        assert auth_utils.verify_password("wrongpassword", h) is False

    def test_verify_against_none_hash_returns_false_not_an_exception(self):
        """A Pending user (invited but never registered) has no password
        set yet -- PasswordHash is NULL/None. Must fail closed, not
        raise."""
        assert auth_utils.verify_password("anything", None) is False

    def test_verify_against_malformed_hash_returns_false_not_an_exception(self):
        assert auth_utils.verify_password("anything", "not-a-real-bcrypt-hash") is False


class TestGenerateToken:
    def test_tokens_are_unique(self):
        assert auth_utils.generate_token() != auth_utils.generate_token()

    def test_returns_a_real_uuid_matching_users_reset_token_column_type(self):
        """Users.ResetToken is uniqueidentifier in the real schema, not a
        free-form string column -- this must return an actual uuid.UUID
        (122 bits of real entropy from os.urandom() under the hood),
        not e.g. secrets.token_urlsafe(), which SQL Server can't convert
        into that column type at all."""
        import uuid as uuid_module

        token = auth_utils.generate_token()
        assert isinstance(token, uuid_module.UUID)
        assert len(str(token)) == 36  # standard hyphenated GUID form


class TestCreateSession:
    def test_inserts_a_user_sessions_row(self, mock_cursor):
        auth_utils.create_session(mock_cursor, "user1", "Customer Admin", "cust1")
        sql = mock_cursor.execute.call_args.args[0]
        assert "INSERT INTO UserSessions" in sql

    def test_does_not_commit_itself(self, mock_cursor, mock_conn):
        """Commit is the caller's (sign_in()/register_user()'s)
        responsibility, as part of its own transaction -- matching every
        other writer in this project."""
        auth_utils.create_session(mock_cursor, "user1", "Customer Admin", "cust1")
        mock_conn.commit.assert_not_called()

    def test_returned_token_decodes_to_the_given_claims(self):
        token = auth_utils.create_session(MagicMock(), "user1", "Streetleaf Admin", None)
        payload = pyjwt.decode(token, "test-jwt-secret", algorithms=["HS256"])
        assert payload["sub"] == "user1"
        assert payload["role"] == "Streetleaf Admin"
        assert payload["customerId"] is None
        assert "jti" in payload  # the session id UserSessions is keyed by

    def test_each_session_gets_a_distinct_session_id(self):
        token1 = auth_utils.create_session(MagicMock(), "user1", "Customer Admin", "cust1")
        token2 = auth_utils.create_session(MagicMock(), "user1", "Customer Admin", "cust1")
        jti1 = pyjwt.decode(token1, "test-jwt-secret", algorithms=["HS256"])["jti"]
        jti2 = pyjwt.decode(token2, "test-jwt-secret", algorithms=["HS256"])["jti"]
        assert jti1 != jti2


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class TestRequireAuth:
    def _make_token(self, cursor):
        return auth_utils.create_session(cursor, "user1", "Customer Admin", "cust1")

    def test_missing_authorization_header_raises_401(self):
        with pytest.raises(auth_utils.AuthError) as exc_info:
            auth_utils.require_auth(_FakeRequest({}))
        assert exc_info.value.status_code == 401

    def test_non_bearer_authorization_header_raises_401(self):
        with pytest.raises(auth_utils.AuthError):
            auth_utils.require_auth(_FakeRequest({"Authorization": "Basic abc123"}))

    def test_garbage_token_raises_401(self):
        with pytest.raises(auth_utils.AuthError, match="invalid session token"):
            auth_utils.require_auth(_FakeRequest({"Authorization": "Bearer not.a.jwt"}))

    def test_valid_non_revoked_session_returns_matching_auth_context(
        self, patch_get_connection_auth_utils, mock_cursor
    ):
        token = self._make_token(mock_cursor)
        mock_cursor.fetchone.return_value = (None,)  # RevokedAt IS NULL

        ctx = auth_utils.require_auth(_FakeRequest({"Authorization": f"Bearer {token}"}))

        assert ctx.user_id == "user1"
        assert ctx.role == "Customer Admin"
        assert ctx.customer_id == "cust1"
        assert ctx.is_streetleaf_admin is False

    def test_streetleaf_admin_role_sets_is_streetleaf_admin_true(
        self, patch_get_connection_auth_utils, mock_cursor
    ):
        token = auth_utils.create_session(mock_cursor, "user2", "Streetleaf Admin", None)
        mock_cursor.fetchone.return_value = (None,)

        ctx = auth_utils.require_auth(_FakeRequest({"Authorization": f"Bearer {token}"}))

        assert ctx.is_streetleaf_admin is True

    def test_revoked_session_raises_401(self, patch_get_connection_auth_utils, mock_cursor):
        token = self._make_token(mock_cursor)
        mock_cursor.fetchone.return_value = ("2026-01-01 00:00:00.000 +00:00",)  # RevokedAt set

        with pytest.raises(auth_utils.AuthError, match="signed out"):
            auth_utils.require_auth(_FakeRequest({"Authorization": f"Bearer {token}"}))

    def test_missing_session_row_raises_401(self, patch_get_connection_auth_utils, mock_cursor):
        """A session row that's been purged (or never existed) must be
        treated as invalid, not as "no opinion, let it through"."""
        token = self._make_token(mock_cursor)
        mock_cursor.fetchone.return_value = None

        with pytest.raises(auth_utils.AuthError, match="signed out"):
            auth_utils.require_auth(_FakeRequest({"Authorization": f"Bearer {token}"}))


class TestRequireRole:
    class _Ctx:
        def __init__(self, role):
            self.role = role

    def test_matching_role_does_not_raise(self):
        auth_utils.require_role(self._Ctx("Streetleaf Admin"), ["Streetleaf Admin"])

    def test_non_matching_role_raises_403(self):
        with pytest.raises(auth_utils.AuthError) as exc_info:
            auth_utils.require_role(self._Ctx("Customer Admin"), ["Streetleaf Admin"])
        assert exc_info.value.status_code == 403

    def test_role_in_a_list_of_multiple_allowed_roles_does_not_raise(self):
        auth_utils.require_role(self._Ctx("Customer Admin"), ["Streetleaf Admin", "Customer Admin"])
