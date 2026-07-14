"""Tests for shared/poles_loader.py"""

import re

import pytest

from shared import poles_loader

DTO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$")


# --------------------------------------------------------------------------
# _map_record_to_pole
# --------------------------------------------------------------------------


class TestMapRecordToPole:
    def test_maps_all_fields(self, make_pole_record):
        record = make_pole_record(
            record_id="recPole001",
            pole_number="P-2002",
            location_id="LOC-7",
            project_ids=["recProjABC"],
            customer_ids=["recCustXYZ"],
            install_date="2026-05-01",
            lat=27.9,
            long=-82.4,
        )

        result = poles_loader._map_record_to_pole(record)

        assert result["Id"] == "recPole001"
        assert result["PoleNumber"] == "P-2002"
        assert result["LocationId"] == "LOC-7"
        assert result["ProjectId"] == "recProjABC"  # first (only) linked id
        assert result["CustomerId"] == "recCustXYZ"  # first (only) linked id
        assert result["InstallDate"] == "2026-05-01"
        assert result["Lat"] == 27.9
        assert result["Long"] == -82.4

    def test_multiple_linked_projects_takes_first_id(self, make_pole_record):
        record = make_pole_record(project_ids=["recFirst", "recSecond"])
        result = poles_loader._map_record_to_pole(record)
        assert result["ProjectId"] == "recFirst"

    def test_missing_project_link_becomes_none(self, make_pole_record):
        record = make_pole_record(project_ids=[])
        result = poles_loader._map_record_to_pole(record)
        assert result["ProjectId"] is None

    def test_non_list_project_field_passes_through_unchanged(self, make_pole_record):
        record = make_pole_record(project_ids="recSingle")
        result = poles_loader._map_record_to_pole(record)
        assert result["ProjectId"] == "recSingle"

    def test_multiple_linked_customers_takes_first_id(self, make_pole_record):
        record = make_pole_record(customer_ids=["recCustFirst", "recCustSecond"])
        result = poles_loader._map_record_to_pole(record)
        assert result["CustomerId"] == "recCustFirst"

    def test_missing_customer_link_becomes_none(self, make_pole_record):
        record = make_pole_record(customer_ids=[])
        result = poles_loader._map_record_to_pole(record)
        assert result["CustomerId"] is None

    def test_non_list_customer_field_passes_through_unchanged(self, make_pole_record):
        record = make_pole_record(customer_ids="recCustSingle")
        result = poles_loader._map_record_to_pole(record)
        assert result["CustomerId"] == "recCustSingle"

    def test_missing_optional_fields_become_none(self):
        record = {"id": "recXYZ", "createdTime": None, "fields": {}}

        result = poles_loader._map_record_to_pole(record)

        assert result["PoleNumber"] is None
        assert result["LocationId"] is None
        assert result["ProjectId"] is None
        assert result["CustomerId"] is None
        assert result["InstallDate"] is None
        assert result["Lat"] is None
        assert result["Long"] is None
        assert result["AirTableCreatedDateTime"] is None

    def test_missing_id_raises_keyerror(self):
        record = {"createdTime": "2026-01-01T00:00:00.000Z", "fields": {}}
        with pytest.raises(KeyError):
            poles_loader._map_record_to_pole(record)

    def test_created_time_uses_eastern_conversion(self, make_pole_record):
        record = make_pole_record(created_time="2026-07-02T18:00:00.000Z")
        result = poles_loader._map_record_to_pole(record)
        assert result["AirTableCreatedDateTime"] == "2026-07-02 14:00:00.000 -04:00"

    def test_lat_na_string_becomes_zero(self, make_pole_record):
        record = make_pole_record(lat="#NA")
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == 0

    def test_long_na_string_becomes_zero(self, make_pole_record):
        record = make_pole_record(long="#NA")
        result = poles_loader._map_record_to_pole(record)
        assert result["Long"] == 0

    def test_both_lat_and_long_na_become_zero(self, make_pole_record):
        record = make_pole_record(lat="#NA", long="#NA")
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == 0
        assert result["Long"] == 0

    def test_na_with_surrounding_whitespace_becomes_zero(self, make_pole_record):
        record = make_pole_record(lat=" #NA ")
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == 0

    @pytest.mark.parametrize("error_string", ["#NA", "#ERROR!", "#DIV/0!"])
    def test_known_error_strings_become_zero_for_lat(self, make_pole_record, error_string):
        record = make_pole_record(lat=error_string)
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == 0

    @pytest.mark.parametrize("error_string", ["#NA", "#ERROR!", "#DIV/0!"])
    def test_known_error_strings_become_zero_for_long(self, make_pole_record, error_string):
        record = make_pole_record(long=error_string)
        result = poles_loader._map_record_to_pole(record)
        assert result["Long"] == 0

    def test_valid_numeric_lat_long_pass_through_unchanged(self, make_pole_record):
        record = make_pole_record(lat=27.9506, long=-82.4572)
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == 27.9506
        assert result["Long"] == -82.4572

    def test_lat_with_leading_and_trailing_spaces_is_trimmed(self, make_pole_record):
        record = make_pole_record(lat=" 27.9506 ")
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == "27.9506"

    def test_long_with_leading_and_trailing_spaces_is_trimmed(self, make_pole_record):
        record = make_pole_record(long=" -82.4572 ")
        result = poles_loader._map_record_to_pole(record)
        assert result["Long"] == "-82.4572"

    def test_lat_with_only_leading_space_is_trimmed(self, make_pole_record):
        record = make_pole_record(lat=" 27.9506")
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == "27.9506"

    def test_lat_with_only_trailing_space_is_trimmed(self, make_pole_record):
        record = make_pole_record(lat="27.9506 ")
        result = poles_loader._map_record_to_pole(record)
        assert result["Lat"] == "27.9506"


# --------------------------------------------------------------------------
# _POLE_UPSERT_SQL structural checks
# --------------------------------------------------------------------------


class TestPoleUpsertSqlStructure:
    def test_placeholder_count_matches_call_site_arg_count(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor, make_pole_record
    ):
        patch_fetch_all_records_poles.return_value = ([make_pole_record()], [])
        poles_loader.load_poles()

        upsert_calls = [
            call
            for call in mock_cursor.execute.call_args_list
            if "MERGE Poles" in call.args[0]
        ]
        assert len(upsert_calls) == 1
        sql_text, *params = upsert_calls[0].args
        assert sql_text.count("?") == len(params) == 10

    def test_insert_column_list_matches_values_list_length(self):
        sql = poles_loader._POLE_UPSERT_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        values_cols = re.search(r"VALUES \(([^)]+)\)", sql, re.DOTALL).group(1)
        assert len(insert_cols.split(",")) == len(values_cols.split(",")) == 10

    def test_merge_match_key_is_id(self):
        assert "ON target.Id = source.Id" in poles_loader._POLE_UPSERT_SQL

    def test_diff_check_uses_intersect(self):
        assert "INTERSECT" in poles_loader._POLE_UPSERT_SQL

    def test_no_fk_references(self):
        """
        Locks in the deliberate design choice: Poles.ProjectId/CustomerId
        have no FK, because load_poles() runs before both load_projects()
        and load_customers() in function_app.py.
        """
        sql = poles_loader._POLE_UPSERT_SQL
        assert "REFERENCES Projects" not in sql
        assert "REFERENCES Customers" not in sql


# --------------------------------------------------------------------------
# load_poles() -- full flow
# --------------------------------------------------------------------------


class TestLoadPolesSuccessFlow:
    def test_full_success_flow_two_records(
        self,
        patch_get_connection_poles,
        patch_fetch_all_records_poles,
        mock_conn,
        mock_cursor,
        make_pole_record,
    ):
        mock_cursor.fetchone.return_value = (7,)
        record1 = make_pole_record(record_id="recPole1")
        record2 = make_pole_record(record_id="recPole2")
        patch_fetch_all_records_poles.return_value = ([record1, record2], [])

        poles_loader.load_poles()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 4  # insert SP_Execution + 2 upserts + final update

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoles", "Dev", "AirTable")
        assert DTO_PATTERN.match(start_time)

        upsert1_args = calls[1].args
        upsert2_args = calls[2].args
        assert upsert1_args[1] == "recPole1"
        assert upsert1_args[9] == 7  # SP_ExecId position
        assert upsert2_args[1] == "recPole2"
        assert upsert2_args[9] == 7

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[3].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 7)
        assert DTO_PATTERN.match(end_time)

        assert mock_conn.commit.call_count == 3
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_empty_airtable_result_still_closes_out_execution_row(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor
    ):
        patch_fetch_all_records_poles.return_value = ([], [])

        poles_loader.load_poles()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[1].args
        assert (success, errors, batch_count) == (0, 0, 1)


class TestLoadPolesPartialFailure:
    def test_one_bad_row_is_counted_but_does_not_abort_the_run(
        self,
        patch_get_connection_poles,
        patch_fetch_all_records_poles,
        mock_cursor,
        make_pole_record,
    ):
        patch_fetch_all_records_poles.return_value = (
            [make_pole_record(record_id="recPole1"), make_pole_record(record_id="recPole2")],
            [],
        )
        mock_cursor.execute.side_effect = [None, None, RuntimeError("bad row"), None]

        poles_loader.load_poles()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)


class TestLoadPolesTopLevelFailure:
    def test_airtable_fetch_failure_updates_error_message_and_reraises(
        self,
        patch_get_connection_poles,
        patch_fetch_all_records_poles,
        mock_conn,
        mock_cursor,
    ):
        mock_cursor.fetchone.return_value = (7,)
        patch_fetch_all_records_poles.side_effect = RuntimeError("airtable is down")

        with pytest.raises(RuntimeError, match="airtable is down"):
            poles_loader.load_poles()

        error_update_calls = [
            call for call in mock_cursor.execute.call_args_list if "ErrorMessage" in call.args[0]
        ]
        assert len(error_update_calls) == 1
        _, _end_time, err_msg, _success, _errors, sp_exec_id = error_update_calls[0].args
        assert err_msg == "airtable is down"
        assert sp_exec_id == 7

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
