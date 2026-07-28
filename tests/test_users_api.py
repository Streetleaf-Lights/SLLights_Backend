"""Tests for shared/users_api.py"""

import pytest

from shared import api_utils, users_api


class TestGetUsers:
    def test_no_ids_queries_top_n_ordered_by_name(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users()

        sql, limit = mock_cursor.execute.call_args.args
        assert "SELECT TOP (?)" in sql
        assert "FROM Users u" in sql
        assert "ORDER BY u.Name" in sql
        assert limit == api_utils.DEFAULT_LIMIT

    def test_left_joins_customers_not_inner(
        self, patch_get_connection_users_api, mock_cursor
    ):
        """A user with no CustomerId (e.g. a Streetleaf Admin, not
        scoped to one customer) must still appear, with customerName
        NULL, rather than being dropped by an INNER JOIN."""
        mock_cursor.fetchall.return_value = []

        users_api.get_users()

        sql = mock_cursor.execute.call_args.args[0]
        assert "LEFT JOIN Customers c ON u.CustomerId = c.Id" in sql

    def test_custom_limit_is_passed_through_clamped(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users(limit=5)

        _, limit = mock_cursor.execute.call_args.args
        assert limit == 5

    def test_limit_above_max_is_capped(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users(limit=99999)

        _, limit = mock_cursor.execute.call_args.args
        assert limit == api_utils.MAX_LIMIT

    def test_does_not_select_password_hash_or_reset_fields(
        self, patch_get_connection_users_api, mock_cursor
    ):
        """Hard security requirement, not an incidental omission --
        these must never appear in this query's SELECT list."""
        mock_cursor.fetchall.return_value = []

        users_api.get_users()

        sql = mock_cursor.execute.call_args.args[0]
        assert "PasswordHash" not in sql
        assert "ResetToken" not in sql
        assert "ResetTokenExpiresAt" not in sql

    def test_maps_rows_to_camelcase_dicts(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            ("user1", "Jane Doe", "jane@example.com", "Customer Admin", "Active", "cust1", "Acme Corp"),
        ]

        result = users_api.get_users()

        assert len(result) == 1
        user = result[0]
        assert user == {
            "id": "user1",
            "name": "Jane Doe",
            "email": "jane@example.com",
            "role": "Customer Admin",
            "status": "Active",
            "customerId": "cust1",
            "customerName": "Acme Corp",
        }
        assert "Id" not in user  # PascalCase keys must not leak through

    def test_customer_less_user_has_null_customer_name(
        self, patch_get_connection_users_api, mock_cursor
    ):
        """e.g. a Streetleaf Admin -- CustomerId and customerName both
        null, not fabricated or omitted."""
        mock_cursor.fetchall.return_value = [
            ("user1", "Admin User", "admin@streetleaf.com", "Streetleaf Admin", "Active", None, None),
        ]

        result = users_api.get_users()

        assert result[0]["customerId"] is None
        assert result[0]["customerName"] is None

    def test_empty_result_returns_empty_list(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert users_api.get_users() == []

    def test_closes_cursor_and_connection(
        self, patch_get_connection_users_api, mock_conn, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_closes_cursor_and_connection_even_on_failure(
        self, patch_get_connection_users_api, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            users_api.get_users()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestGetUsersUserIdFilter:
    def test_user_id_filters_by_id_not_top_n(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users(user_id="user1")

        sql, uid = mock_cursor.execute.call_args.args
        assert "WHERE u.Id = ?" in sql
        assert "TOP" not in sql
        assert uid == "user1"

    def test_nonexistent_user_returns_empty_list(
        self, patch_get_connection_users_api, mock_cursor
    ):
        """get_users() always returns a list -- 0 or 1 elements for a
        user_id lookup, same contract as customers_api.get_customers().
        The HTTP layer decides single-object-or-404 shaping."""
        mock_cursor.fetchall.return_value = []
        assert users_api.get_users(user_id="does-not-exist") == []

    def test_limit_is_ignored_when_user_id_given(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users(user_id="user1", limit=5)

        args = mock_cursor.execute.call_args.args
        assert len(args) == 2  # sql, user_id -- no limit param bound


class TestGetUsersCustomerIdFilter:
    def test_customer_id_alone_filters_by_customer_id_column(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users(customer_id="cust1")

        sql, cid = mock_cursor.execute.call_args.args
        assert "WHERE u.CustomerId = ?" in sql
        assert "TOP" not in sql
        assert cid == "cust1"

    def test_customer_with_zero_users_returns_empty_list(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert users_api.get_users(customer_id="cust-with-no-users") == []

    def test_customer_id_alone_can_return_multiple_users(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            ("user1", "Jane Doe", "jane@example.com", "Customer Admin", "Active", "cust1", "Acme Corp"),
            ("user2", "John Smith", "john@example.com", "Customer Admin", "Pending", "cust1", "Acme Corp"),
        ]

        result = users_api.get_users(customer_id="cust1")

        assert len(result) == 2


class TestGetUsersCombinedFilter:
    """
    Built as an AND-combination from the start (not an if/elif chain
    where userId could silently ignore customerId) -- the same bug
    pattern found and fixed in shared/poles_api.py earlier, avoided
    here by construction rather than by a later fix.
    """

    def test_user_id_and_customer_id_combine_with_and(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        users_api.get_users(user_id="user1", customer_id="cust1")

        sql, uid, cid = mock_cursor.execute.call_args.args
        assert "WHERE u.Id = ? AND u.CustomerId = ?" in sql
        assert uid == "user1"
        assert cid == "cust1"

    def test_user_belonging_to_different_customer_returns_empty_list(
        self, patch_get_connection_users_api, mock_cursor
    ):
        """e.g. a real user Id that belongs to a DIFFERENT customer than
        the one specified -- the AND in SQL means zero rows come back."""
        mock_cursor.fetchall.return_value = []
        result = users_api.get_users(user_id="user1", customer_id="wrong-customer")
        assert result == []
