"""Tests for shared/projects_loader.py"""

import json
import re

import pytest

from shared import projects_loader

DTO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$")


# --------------------------------------------------------------------------
# _map_record_to_project
# --------------------------------------------------------------------------


class TestMapRecordToProject:
    def test_maps_all_fields_with_list_values_json_encoded(self, make_project_record):
        record = make_project_record(
            record_id="recProj001",
            name="Downtown Fiber Rollout",
            pole_numbers=["P-1", "P-2"],
            pole_ids=["pole_a", "pole_b"],
            customer_ids=["recCustomerXYZ"],
            poles_under_contract=42,
            effective_date="2026-02-01",
            install_dates=["2026-04-15", "2026-05-01"],
        )

        result = projects_loader._map_record_to_project(record)

        assert result["Id"] == "recProj001"
        assert result["Name"] == "Downtown Fiber Rollout"
        assert result["PoleNumbers"] == json.dumps(["P-1", "P-2"])
        assert result["PoleIds"] == json.dumps(["pole_a", "pole_b"])
        assert result["CustomerId"] == "recCustomerXYZ"  # first (only) linked id
        assert result["PolesUnderContract"] == 42
        assert result["EffectiveDate"] == "2026-02-01"
        assert result["InstallDates"] == json.dumps(["2026-04-15", "2026-05-01"])

    def test_non_list_pole_and_install_dates_fields_pass_through_unchanged(self, make_project_record):
        record = make_project_record(
            pole_numbers="SinglePole", pole_ids="pole_a", install_dates="2026-04-15"
        )
        result = projects_loader._map_record_to_project(record)
        assert result["PoleNumbers"] == "SinglePole"
        assert result["PoleIds"] == "pole_a"
        assert result["InstallDates"] == "2026-04-15"

    def test_multiple_linked_customers_takes_first_id(self, make_project_record):
        record = make_project_record(customer_ids=["recFirst", "recSecond"])
        result = projects_loader._map_record_to_project(record)
        assert result["CustomerId"] == "recFirst"

    def test_missing_customer_link_becomes_none(self, make_project_record):
        record = make_project_record(customer_ids=[])
        result = projects_loader._map_record_to_project(record)
        assert result["CustomerId"] is None

    def test_missing_optional_fields_become_none(self):
        record = {"id": "recXYZ", "createdTime": None, "fields": {}}

        result = projects_loader._map_record_to_project(record)

        assert result["Name"] is None
        assert result["CustomerId"] is None
        assert result["PolesUnderContract"] is None
        assert result["EffectiveDate"] is None
        assert result["AirTableCreatedDateTime"] is None
        assert result["PoleNumbers"] == "[]"
        assert result["PoleIds"] == "[]"
        assert result["InstallDates"] == "[]"

    def test_missing_id_raises_keyerror(self):
        record = {"createdTime": "2026-01-01T00:00:00.000Z", "fields": {}}
        with pytest.raises(KeyError):
            projects_loader._map_record_to_project(record)

    def test_created_time_uses_eastern_conversion(self, make_project_record):
        record = make_project_record(created_time="2026-07-02T18:00:00.000Z")
        result = projects_loader._map_record_to_project(record)
        assert result["AirTableCreatedDateTime"] == "2026-07-02 14:00:00.000 -04:00"


# --------------------------------------------------------------------------
# _PROJECT_UPSERT_SQL structural checks
# --------------------------------------------------------------------------


class TestProjectUpsertSqlStructure:
    def test_placeholder_count_matches_call_site_arg_count(
        self,
        patch_get_connection_projects,
        patch_fetch_all_records_projects,
        mock_cursor,
        make_project_record,
    ):
        patch_fetch_all_records_projects.return_value = ([make_project_record()], [])
        projects_loader.load_projects()

        upsert_calls = [
            call
            for call in mock_cursor.execute.call_args_list
            if "MERGE Projects" in call.args[0]
        ]
        assert len(upsert_calls) == 1
        sql_text, *params = upsert_calls[0].args
        assert sql_text.count("?") == len(params) == 10

    def test_insert_column_list_matches_values_list_length(self):
        sql = projects_loader._PROJECT_UPSERT_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        values_cols = re.search(r"VALUES \(([^)]+)\)", sql, re.DOTALL).group(1)
        assert len(insert_cols.split(",")) == len(values_cols.split(",")) == 10

    def test_merge_match_key_is_id(self):
        assert "ON target.Id = source.Id" in projects_loader._PROJECT_UPSERT_SQL

    def test_diff_check_uses_intersect_not_isnull_empty_string(self):
        """
        Customers' MERGE uses ISNULL(col, '') <> ISNULL(col, ''), which
        breaks for non-string columns. Projects has a DATE column
        (EffectiveDate), so it should use the NULL-safe INTERSECT pattern
        instead.
        """
        sql = projects_loader._PROJECT_UPSERT_SQL
        assert "INTERSECT" in sql
        assert "ISNULL(target.EffectiveDate" not in sql

    def test_long_text_columns_are_cast_to_nvarchar_max(self):
        """
        Regression test for: 'The data type ntext cannot be used as an
        operand to the UNION, INTERSECT or EXCEPT operators'. pyodbc binds
        long string parameters (JSON-encoded PoleNumbers/PoleIds/
        InstallDates for records with many entries) as ntext unless the SQL
        explicitly casts them to NVARCHAR(MAX) first.
        """
        sql = projects_loader._PROJECT_UPSERT_SQL
        assert "CAST(? AS NVARCHAR(MAX)) AS PoleNumbers" in sql
        assert "CAST(? AS NVARCHAR(MAX)) AS PoleIds" in sql
        assert "CAST(? AS NVARCHAR(MAX)) AS InstallDates" in sql


# --------------------------------------------------------------------------
# load_projects() -- full flow
# --------------------------------------------------------------------------


class TestLoadProjectsSuccessFlow:
    def test_full_success_flow_two_records(
        self,
        patch_get_connection_projects,
        patch_fetch_all_records_projects,
        mock_conn,
        mock_cursor,
        make_project_record,
    ):
        mock_cursor.fetchone.return_value = (99,)
        record1 = make_project_record(record_id="recProj1")
        record2 = make_project_record(record_id="recProj2")
        patch_fetch_all_records_projects.return_value = ([record1, record2], [])

        projects_loader.load_projects()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 4  # insert SP_Execution + 2 upserts + final update

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadProjects", "Dev", "AirTable")
        assert DTO_PATTERN.match(start_time)

        upsert1_args = calls[1].args
        upsert2_args = calls[2].args
        assert upsert1_args[1] == "recProj1"
        assert upsert1_args[5] == 99  # SP_ExecId position
        assert upsert2_args[1] == "recProj2"
        assert upsert2_args[5] == 99

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[3].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 99)
        assert DTO_PATTERN.match(end_time)

        assert mock_conn.commit.call_count == 3
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_empty_airtable_result_still_closes_out_execution_row(
        self, patch_get_connection_projects, patch_fetch_all_records_projects, mock_cursor
    ):
        patch_fetch_all_records_projects.return_value = ([], [])

        projects_loader.load_projects()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[1].args
        assert (success, errors, batch_count) == (0, 0, 1)


class TestLoadProjectsPartialFailure:
    def test_one_bad_row_is_counted_but_does_not_abort_the_run(
        self,
        patch_get_connection_projects,
        patch_fetch_all_records_projects,
        mock_cursor,
        make_project_record,
    ):
        patch_fetch_all_records_projects.return_value = (
            [make_project_record(record_id="recProj1"), make_project_record(record_id="recProj2")],
            [],
        )
        mock_cursor.execute.side_effect = [None, None, RuntimeError("bad row"), None]

        projects_loader.load_projects()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)


class TestLoadProjectsTopLevelFailure:
    def test_airtable_fetch_failure_updates_error_message_and_reraises(
        self,
        patch_get_connection_projects,
        patch_fetch_all_records_projects,
        mock_conn,
        mock_cursor,
    ):
        mock_cursor.fetchone.return_value = (99,)
        patch_fetch_all_records_projects.side_effect = RuntimeError("airtable is down")

        with pytest.raises(RuntimeError, match="airtable is down"):
            projects_loader.load_projects()

        error_update_calls = [
            call for call in mock_cursor.execute.call_args_list if "ErrorMessage" in call.args[0]
        ]
        assert len(error_update_calls) == 1
        _, _end_time, err_msg, _success, _errors, sp_exec_id = error_update_calls[0].args
        assert err_msg == "airtable is down"
        assert sp_exec_id == 99

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
