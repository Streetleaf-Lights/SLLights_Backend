"""Tests for shared/customers_loader.py"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from shared import customers_loader


# --------------------------------------------------------------------------
# _to_dto_string
# --------------------------------------------------------------------------

DTO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$")


class TestToDtoString:
    def test_formats_negative_offset(self):
        dt = datetime(2026, 7, 2, 14, 14, 39, 901000, tzinfo=timezone(timedelta(hours=-4)))
        assert customers_loader._to_dto_string(dt) == "2026-07-02 14:14:39.901 -04:00"

    def test_formats_positive_offset(self):
        dt = datetime(2026, 1, 15, 9, 0, 0, 500000, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        assert customers_loader._to_dto_string(dt) == "2026-01-15 09:00:00.500 +05:30"

    def test_formats_utc_zero_offset(self):
        dt = datetime(2025, 11, 17, 19, 56, 44, 0, tzinfo=timezone.utc)
        assert customers_loader._to_dto_string(dt) == "2025-11-17 19:56:44.000 +00:00"

    def test_truncates_microseconds_to_milliseconds(self):
        dt = datetime(2026, 3, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5)))
        result = customers_loader._to_dto_string(dt)
        assert result.endswith(".123 -05:00")

    def test_output_matches_dto_shape(self):
        dt = datetime.now(customers_loader.EASTERN)
        assert DTO_PATTERN.match(customers_loader._to_dto_string(dt))


# --------------------------------------------------------------------------
# _airtable_created_time_to_eastern
# --------------------------------------------------------------------------


class TestAirtableCreatedTimeToEastern:
    def test_none_returns_none(self):
        assert customers_loader._airtable_created_time_to_eastern(None) is None

    def test_empty_string_returns_none(self):
        assert customers_loader._airtable_created_time_to_eastern("") is None

    def test_winter_utc_converts_to_est_minus_5(self):
        # Nov 17 is outside US DST -> EST, UTC-5
        result = customers_loader._airtable_created_time_to_eastern(
            "2025-11-17T19:56:44.000Z"
        )
        assert result == "2025-11-17 14:56:44.000 -05:00"

    def test_summer_utc_converts_to_edt_minus_4(self):
        # July 2 is inside US DST -> EDT, UTC-4
        result = customers_loader._airtable_created_time_to_eastern(
            "2026-07-02T18:00:00.000Z"
        )
        assert result == "2026-07-02 14:00:00.000 -04:00"

    def test_result_matches_dto_shape(self):
        result = customers_loader._airtable_created_time_to_eastern(
            "2026-01-01T00:00:00.000Z"
        )
        assert DTO_PATTERN.match(result)


# --------------------------------------------------------------------------
# _map_record_to_customer
# --------------------------------------------------------------------------


class TestMapRecordToCustomer:
    def test_maps_all_fields_with_list_values_json_encoded(self, make_airtable_record):
        record = make_airtable_record(
            record_id="recABC123",
            name="Acme Corp",
            project_names=["Alpha", "Beta"],
            executed_projects=["p1", "p2"],
        )

        result = customers_loader._map_record_to_customer(record)

        assert result["Id"] == "recABC123"
        assert result["Name"] == "Acme Corp"
        assert result["ProjectNames"] == json.dumps(["Alpha", "Beta"])
        assert result["ProjectIds"] == json.dumps(["p1", "p2"])
        assert result["Address"] == "123 Main St"
        assert result["City"] == "Clearwater"
        assert result["State"] == "FL"
        assert result["Zip"] == "33755"
        assert result["Phone"] == "(727) 555-0100"

    def test_non_list_project_fields_pass_through_unchanged(self, make_airtable_record):
        record = make_airtable_record(project_names="SingleProject", executed_projects="p1")

        result = customers_loader._map_record_to_customer(record)

        assert result["ProjectNames"] == "SingleProject"
        assert result["ProjectIds"] == "p1"

    def test_missing_optional_fields_become_none(self):
        record = {"id": "recXYZ", "createdTime": None, "fields": {}}

        result = customers_loader._map_record_to_customer(record)

        assert result["Name"] is None
        assert result["Address"] is None
        assert result["City"] is None
        assert result["State"] is None
        assert result["Zip"] is None
        assert result["Phone"] is None
        assert result["AirTableCreatedDateTime"] is None
        # ProjectNames/ProjectIds default to [] -> json-encoded empty list
        assert result["ProjectNames"] == "[]"
        assert result["ProjectIds"] == "[]"

    def test_missing_id_raises_keyerror(self):
        record = {"createdTime": "2026-01-01T00:00:00.000Z", "fields": {}}
        with pytest.raises(KeyError):
            customers_loader._map_record_to_customer(record)

    def test_field_name_mapping_street_and_phone_number(self, make_airtable_record):
        record = make_airtable_record(street="456 Oak Ave", phone_number="555-1234")
        result = customers_loader._map_record_to_customer(record)
        assert result["Address"] == "456 Oak Ave"
        assert result["Phone"] == "555-1234"

    def test_created_time_uses_eastern_conversion(self, make_airtable_record):
        record = make_airtable_record(created_time="2026-07-02T18:00:00.000Z")
        result = customers_loader._map_record_to_customer(record)
        assert result["AirTableCreatedDateTime"] == "2026-07-02 14:00:00.000 -04:00"


# --------------------------------------------------------------------------
# _UPSERT_SQL structural / "schema" consistency checks
# --------------------------------------------------------------------------


class TestUpsertSqlStructure:
    """
    These don't hit a real database (no live Azure SQL reachable from here),
    but they catch the class of bug where the parameter list, the SELECT
    aliases, and the INSERT column list drift out of sync with each other.
    """

    def test_placeholder_count_matches_call_site_arg_count(
        self, patch_get_connection, patch_fetch_all_records, mock_cursor, make_airtable_record
    ):
        patch_fetch_all_records.return_value = ([make_airtable_record()], [])
        customers_loader.load_customers()

        upsert_calls = [
            call
            for call in mock_cursor.execute.call_args_list
            if "MERGE Customers" in call.args[0]
        ]
        assert len(upsert_calls) == 1
        sql_text, *params = upsert_calls[0].args
        placeholder_count = sql_text.count("?")
        assert placeholder_count == len(params) == 11

    def test_insert_column_list_matches_values_list_length(self):
        sql = customers_loader._UPSERT_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        values_cols = re.search(r"VALUES \(([^)]+)\)", sql, re.DOTALL).group(1)

        insert_col_count = len(insert_cols.split(","))
        values_col_count = len(values_cols.split(","))
        assert insert_col_count == values_col_count == 11

    def test_upsert_sql_references_sp_exec_id_column(self):
        """
        NOTE: earlier schema design in this project used a column named
        `BatchId` on Customers. The current code writes to `SP_ExecId`
        instead. This test locks in what the *code* currently expects --
        double check your live Customers table actually has an `SP_ExecId`
        column (or that it was renamed from `BatchId`), or this MERGE will
        fail at runtime with an invalid column name error.
        """
        assert "SP_ExecId" in customers_loader._UPSERT_SQL


# --------------------------------------------------------------------------
# load_customers() -- full flow, mocking only the two external boundaries:
# the DB connection and the Airtable fetch.
# --------------------------------------------------------------------------


class TestLoadCustomersSuccessFlow:
    def test_full_success_flow_two_records(
        self, patch_get_connection, patch_fetch_all_records, mock_conn, mock_cursor, make_airtable_record
    ):
        mock_cursor.fetchone.return_value = (42,)
        record1 = make_airtable_record(record_id="rec1")
        record2 = make_airtable_record(record_id="rec2")
        patch_fetch_all_records.return_value = ([record1, record2], [])

        customers_loader.load_customers()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 4  # insert SP_Execution + 2 upserts + final update

        # 1. Opening SP_Execution insert
        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadCustomers", "Dev", "AirTable")
        assert DTO_PATTERN.match(start_time)

        # 2 & 3. Upserts, in Airtable order, carrying the sp_exec_id (42)
        upsert1_args = calls[1].args
        upsert2_args = calls[2].args
        assert upsert1_args[1] == "rec1"
        assert upsert1_args[5] == 42  # SP_ExecId position
        assert upsert2_args[1] == "rec2"
        assert upsert2_args[5] == 42

        # 4. Final SP_Execution update with success/error counts
        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[3].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 42)
        assert DTO_PATTERN.match(end_time)

        assert mock_conn.commit.call_count == 3  # after insert, after loop, after final update
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_empty_airtable_result_still_closes_out_execution_row(
        self, patch_get_connection, patch_fetch_all_records, mock_cursor
    ):
        patch_fetch_all_records.return_value = ([], [])

        customers_loader.load_customers()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2  # insert + final update, no upserts
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[1].args
        assert (success, errors, batch_count) == (0, 0, 1)

    def test_multi_page_batch_count_reflects_offsets_seen(
        self, patch_get_connection, patch_fetch_all_records, mock_cursor, make_airtable_record
    ):
        patch_fetch_all_records.return_value = ([make_airtable_record()], ["off1", "off2"])

        customers_loader.load_customers()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        batch_count = final_update_args[4]
        assert batch_count == 3  # len(offsets_seen) + 1


class TestLoadCustomersPartialFailure:
    def test_one_bad_row_is_counted_but_does_not_abort_the_run(
        self, patch_get_connection, patch_fetch_all_records, mock_cursor, make_airtable_record
    ):
        patch_fetch_all_records.return_value = (
            [make_airtable_record(record_id="rec1"), make_airtable_record(record_id="rec2")],
            [],
        )
        # calls in order: insert SP_Execution, upsert rec1, upsert rec2 (fails), final update
        mock_cursor.execute.side_effect = [None, None, RuntimeError("bad row"), None]

        customers_loader.load_customers()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)
        mock_conn = patch_get_connection.return_value
        mock_conn.close.assert_called_once()


class TestLoadCustomersTopLevelFailure:
    def test_airtable_fetch_failure_updates_error_message_and_reraises(
        self, patch_get_connection, patch_fetch_all_records, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        patch_fetch_all_records.side_effect = RuntimeError("airtable is down")

        with pytest.raises(RuntimeError, match="airtable is down"):
            customers_loader.load_customers()

        error_update_calls = [
            call
            for call in mock_cursor.execute.call_args_list
            if "ErrorMessage" in call.args[0]
        ]
        assert len(error_update_calls) == 1
        _, end_time, err_msg, success, errors, sp_exec_id = error_update_calls[0].args
        assert err_msg == "airtable is down"
        assert sp_exec_id == 42

        # cursor/connection must still be cleaned up even on failure
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_failure_before_sp_exec_id_assigned_skips_error_update(
        self, patch_get_connection, patch_fetch_all_records, mock_cursor
    ):
        """
        If the very first INSERT INTO SP_Execution fails, sp_exec_id never
        gets set, so the `if sp_exec_id:` guard in the except block means no
        ErrorMessage UPDATE is attempted (there's no row to update anyway).
        The exception must still propagate and cleanup must still happen.
        """
        mock_cursor.execute.side_effect = RuntimeError("insert failed")

        with pytest.raises(RuntimeError, match="insert failed"):
            customers_loader.load_customers()

        assert mock_cursor.execute.call_count == 1  # never got past the first call
        mock_cursor.close.assert_called_once()

    def test_sp_exec_id_of_zero_is_falsy_and_skips_error_update(
        self, patch_get_connection, patch_fetch_all_records, mock_conn, mock_cursor
    ):
        """
        Documents a subtle current edge case: `if sp_exec_id:` treats an Id
        of 0 as falsy, same as None. If your SP_Execution.Id could ever
        legitimately be 0, the error-path UPDATE would silently be skipped
        for that run. In practice IDENTITY columns start at 1, so this is
        informational rather than a live bug -- but it's worth knowing the
        code relies on that assumption.
        """
        mock_cursor.fetchone.return_value = (0,)
        patch_fetch_all_records.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            customers_loader.load_customers()

        error_update_calls = [
            call
            for call in mock_cursor.execute.call_args_list
            if "ErrorMessage" in call.args[0]
        ]
        assert len(error_update_calls) == 0
