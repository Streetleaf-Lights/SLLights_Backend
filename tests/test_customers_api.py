"""Tests for shared/customers_api.py"""

from datetime import datetime, timezone

import pytest

from shared import customers_api


# --------------------------------------------------------------------------
# _json_safe
# --------------------------------------------------------------------------


class TestJsonSafe:
    def test_none_passes_through(self):
        assert customers_api._json_safe(None) is None

    def test_str_int_float_bool_pass_through_unchanged(self):
        assert customers_api._json_safe("abc") == "abc"
        assert customers_api._json_safe(42) == 42
        assert customers_api._json_safe(3.14) == 3.14
        assert customers_api._json_safe(True) is True

    def test_datetime_converted_to_string(self):
        dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = customers_api._json_safe(dt)
        assert isinstance(result, str)

    def test_unknown_type_converted_to_string(self):
        class Weird:
            def __str__(self):
                return "weird-value"

        assert customers_api._json_safe(Weird()) == "weird-value"


# --------------------------------------------------------------------------
# _clamp_limit
# --------------------------------------------------------------------------


class TestClampLimit:
    def test_default_limit_equals_max_limit(self):
        """
        No limit specified should mean "everything, up to the ceiling",
        not some arbitrarily lower default -- a business's customer
        roster is very unlikely to need pagination, so silently
        truncating to a low default just loses real results for anyone
        who doesn't know to pass ?limit= explicitly.
        """
        assert customers_api.DEFAULT_LIMIT == customers_api.MAX_LIMIT

    def test_none_returns_default(self):
        assert customers_api._clamp_limit(None) == customers_api.DEFAULT_LIMIT

    def test_zero_returns_default(self):
        assert customers_api._clamp_limit(0) == customers_api.DEFAULT_LIMIT

    def test_normal_value_passes_through(self):
        assert customers_api._clamp_limit(50) == 50

    def test_value_above_max_is_capped(self):
        assert customers_api._clamp_limit(999999) == customers_api.MAX_LIMIT

    def test_negative_value_is_clamped_to_one(self):
        assert customers_api._clamp_limit(-5) == 1

    def test_string_digit_is_coerced_to_int(self):
        assert customers_api._clamp_limit("50") == 50


# --------------------------------------------------------------------------
# get_customers
# --------------------------------------------------------------------------


class TestGetCustomers:
    def test_no_customer_id_queries_top_n_ordered_by_name(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        customers_api.get_customers()

        sql, limit = mock_cursor.execute.call_args.args
        assert "SELECT TOP (?)" in sql
        assert "FROM Customers" in sql
        assert "ORDER BY Name" in sql
        assert limit == customers_api.DEFAULT_LIMIT

    def test_custom_limit_is_passed_through_clamped(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        customers_api.get_customers(limit=5)

        _, limit = mock_cursor.execute.call_args.args
        assert limit == 5

    def test_limit_above_max_is_capped_in_query(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        customers_api.get_customers(limit=99999)

        _, limit = mock_cursor.execute.call_args.args
        assert limit == customers_api.MAX_LIMIT

    def test_customer_id_queries_by_id_not_top_n(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        customers_api.get_customers(customer_id="rec123")

        sql, cid = mock_cursor.execute.call_args.args
        assert "WHERE Id = ?" in sql
        assert "TOP" not in sql
        assert cid == "rec123"

    def test_does_not_select_sp_exec_id(self, patch_get_connection_customers_api, mock_cursor):
        mock_cursor.fetchall.return_value = []

        customers_api.get_customers()

        sql = mock_cursor.execute.call_args.args[0]
        assert "SP_ExecId" not in sql

    def test_maps_rows_to_camelcase_dicts(self, patch_get_connection_customers_api, mock_cursor):
        mock_cursor.fetchall.return_value = [
            ("rec123", "Acme Corp", "[]", "[]", "123 Main St", "Springfield", "IL", "62701",
             "555-1234", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]

        result = customers_api.get_customers()

        assert len(result) == 1
        customer = result[0]
        assert customer["id"] == "rec123"
        assert customer["name"] == "Acme Corp"
        assert customer["city"] == "Springfield"
        assert customer["state"] == "IL"
        assert isinstance(customer["createdAt"], str)
        assert "Id" not in customer  # PascalCase keys must not leak through

    def test_empty_result_returns_empty_list(self, patch_get_connection_customers_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        assert customers_api.get_customers() == []

    def test_closes_cursor_and_connection(
        self, patch_get_connection_customers_api, mock_conn, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        customers_api.get_customers()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_closes_cursor_and_connection_even_on_failure(
        self, patch_get_connection_customers_api, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            customers_api.get_customers()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
