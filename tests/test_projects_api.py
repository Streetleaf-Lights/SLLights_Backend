"""Tests for shared/projects_api.py"""

from datetime import date, datetime, timezone

import pytest

from shared import api_utils, projects_api


class TestGetProjects:
    def test_no_project_id_queries_top_n_ordered_by_name(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects()

        sql, limit = mock_cursor.execute.call_args.args
        assert "SELECT TOP (?)" in sql
        assert "FROM Projects" in sql
        assert "ORDER BY Name" in sql
        assert limit == api_utils.DEFAULT_LIMIT

    def test_custom_limit_is_passed_through_clamped(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(limit=5)

        _, limit = mock_cursor.execute.call_args.args
        assert limit == 5

    def test_limit_above_max_is_capped_in_query(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(limit=99999)

        _, limit = mock_cursor.execute.call_args.args
        assert limit == api_utils.MAX_LIMIT

    def test_project_id_queries_by_id_not_top_n(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(project_id="rec456")

        sql, pid = mock_cursor.execute.call_args.args
        assert "WHERE Id = ?" in sql
        assert "TOP" not in sql
        assert pid == "rec456"

    def test_customer_id_alone_filters_by_customer_sorted_by_effective_date_desc(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(customer_id="recwx649JfiRmWqxF")

        sql, limit, cid = mock_cursor.execute.call_args.args
        assert "SELECT TOP (?)" in sql
        assert "WHERE CustomerId = ?" in sql
        assert "ORDER BY EffectiveDate DESC" in sql
        assert limit == api_utils.DEFAULT_LIMIT
        assert cid == "recwx649JfiRmWqxF"

    def test_customer_id_filtered_sort_is_deliberately_different_from_unfiltered_sort(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        """
        Locks in that only the customer_id-filtered list was asked to
        sort by EffectiveDate -- the unfiltered ("all projects") list
        still sorts by Name. Guards against a future edit accidentally
        applying one sort order to both paths.
        """
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(customer_id="recwx649JfiRmWqxF")
        customer_filtered_sql = mock_cursor.execute.call_args.args[0]

        projects_api.get_projects()
        unfiltered_sql = mock_cursor.execute.call_args.args[0]

        assert "ORDER BY EffectiveDate DESC" in customer_filtered_sql
        assert "ORDER BY Name" not in customer_filtered_sql
        assert "ORDER BY Name" in unfiltered_sql
        assert "ORDER BY EffectiveDate" not in unfiltered_sql

    def test_customer_id_alone_respects_limit(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(customer_id="recwx649JfiRmWqxF", limit=5)

        _, limit, _ = mock_cursor.execute.call_args.args
        assert limit == 5

    def test_project_id_and_customer_id_together_filters_by_both(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects(project_id="rec456", customer_id="recwx649JfiRmWqxF")

        sql, pid, cid = mock_cursor.execute.call_args.args
        assert "WHERE Id = ? AND CustomerId = ?" in sql
        assert "TOP" not in sql
        assert (pid, cid) == ("rec456", "recwx649JfiRmWqxF")

    def test_customer_id_can_return_multiple_projects(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            ("rec456", "Chaparral Ph3", "[]", "[]", "recwx649JfiRmWqxF", 42,
             date(2026, 1, 1), "[]", datetime(2026, 1, 1, tzinfo=timezone.utc)),
            ("rec789", "Elm St", "[]", "[]", "recwx649JfiRmWqxF", 10,
             date(2026, 2, 1), "[]", datetime(2026, 2, 1, tzinfo=timezone.utc)),
        ]

        result = projects_api.get_projects(customer_id="recwx649JfiRmWqxF")

        assert len(result) == 2
        assert {p["id"] for p in result} == {"rec456", "rec789"}

    def test_does_not_select_sp_exec_id(self, patch_get_connection_projects_api, mock_cursor):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects()

        sql = mock_cursor.execute.call_args.args[0]
        assert "SP_ExecId" not in sql

    def test_maps_rows_to_camelcase_dicts(self, patch_get_connection_projects_api, mock_cursor):
        mock_cursor.fetchall.return_value = [
            ("rec456", "Chaparral Ph3", "[]", "[]", "recwx649JfiRmWqxF", 42,
             date(2026, 1, 1), "[]", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]

        result = projects_api.get_projects()

        assert len(result) == 1
        project = result[0]
        assert project["id"] == "rec456"
        assert project["name"] == "Chaparral Ph3"
        assert project["customerId"] == "recwx649JfiRmWqxF"
        assert project["polesUnderContract"] == 42
        assert isinstance(project["effectiveDate"], str)  # DATE isn't natively JSON-safe either
        assert isinstance(project["createdAt"], str)
        assert "Id" not in project  # PascalCase keys must not leak through

    def test_empty_result_returns_empty_list(self, patch_get_connection_projects_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        assert projects_api.get_projects() == []

    def test_closes_cursor_and_connection(
        self, patch_get_connection_projects_api, mock_conn, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        projects_api.get_projects()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_closes_cursor_and_connection_even_on_failure(
        self, patch_get_connection_projects_api, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            projects_api.get_projects()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
