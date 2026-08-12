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
            county_fips="12057",
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
        assert result["CountyFips"] == "12057"
        assert result["ProjectId"] == "recProjABC"  # first (only) linked id
        assert result["CustomerId"] == "recCustXYZ"  # first (only) linked id
        assert result["InstallDate"] == "2026-05-01"
        assert result["Lat"] == 27.9
        assert result["Long"] == -82.4

    def test_location_id_and_county_fips_are_read_from_separate_fields(
        self, make_pole_record
    ):
        """"Location ID" (the pole's own identifier) and "CountyFips"
        (that same pole's county) are unrelated Airtable fields -- must
        never get merged/confused."""
        record = make_pole_record(location_id="LOC-99", county_fips="06037")
        result = poles_loader._map_record_to_pole(record)
        assert result["LocationId"] == "LOC-99"
        assert result["CountyFips"] == "06037"
        assert result["LocationId"] != result["CountyFips"]

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
        assert result["CountyFips"] is None
        assert result["ProjectId"] is None
        assert result["CustomerId"] is None
        assert result["InstallDate"] is None
        assert result["Lat"] is None
        assert result["Long"] is None
        assert result["AirTableCreatedDateTime"] is None


class TestCleanCountyFips:
    """
    Dedicated coverage for two real, specific risks, both confirmed in
    practice against the real Airtable base, not just theoretical:

    1. If Airtable's "CountyFips" field is (or ever becomes) a
       Number-type field rather than Text, a leading zero in the FIPS
       code (e.g. "01001" for Autauga County, AL) is already gone by the
       time Python ever sees the value -- Airtable would hand back the
       plain int 1001, not the string "01001". str(1001).zfill(5)
       correctly recovers "01001" since zfill pads on the LEFT.

    2. Airtable's "CountyFips" field returns its value wrapped in a
       list (e.g. ["12101"]) -- the same shape a lookup/linked-record
       field produces regardless of cardinality, same as ProjectId/
       CustomerId elsewhere in this file. Without unwrapping this first,
       str(["12101"]) produces the literal string "['12101']", which
       correctly (but unhelpfully) fails the purely-numeric check and
       gets rejected -- a real bug this project hit in production.
    """

    def test_none_becomes_none(self):
        assert poles_loader._clean_county_fips(None) is None

    def test_blank_string_becomes_none(self):
        assert poles_loader._clean_county_fips("") is None
        assert poles_loader._clean_county_fips("   ") is None

    def test_correctly_formatted_five_digit_string_passes_through(self):
        assert poles_loader._clean_county_fips("12057") == "12057"

    def test_string_missing_leading_zero_gets_zero_padded(self):
        assert poles_loader._clean_county_fips("1001") == "01001"

    def test_int_gets_converted_and_zero_padded(self):
        """The core risk this function exists for: Airtable handing back
        a plain int (leading zero already lost) instead of a string."""
        assert poles_loader._clean_county_fips(1001) == "01001"

    def test_float_gets_converted_and_zero_padded(self):
        """Airtable's API can return Number-type fields as floats even for
        whole numbers (e.g. 1001.0) -- must still recover correctly, not
        produce "1001.0" or crash."""
        assert poles_loader._clean_county_fips(1001.0) == "01001"

    def test_single_item_list_gets_unwrapped(self):
        """The exact real-world bug this covers: Airtable returning
        ["12101"] rather than the plain string "12101"."""
        assert poles_loader._clean_county_fips(["12101"]) == "12101"

    def test_single_item_list_with_missing_leading_zero_still_gets_padded(self):
        """Unwrapping happens BEFORE the zero-pad/numeric checks, so both
        fixes compose correctly rather than only one applying."""
        assert poles_loader._clean_county_fips(["1101"]) == "01101"

    def test_single_item_list_with_int_gets_unwrapped_and_padded(self):
        assert poles_loader._clean_county_fips([1101]) == "01101"

    def test_empty_list_becomes_none(self):
        assert poles_loader._clean_county_fips([]) is None

    def test_multi_item_list_uses_first_and_logs_a_warning(self, caplog):
        """Unexpected for what should be one pole -> one county, but
        takes the first rather than failing this pole's entire load --
        same "don't let one odd field block everything else" philosophy
        as the rest of this loader."""
        with caplog.at_level("WARNING"):
            result = poles_loader._clean_county_fips(["12101", "12102"])

        assert result == "12101"
        warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert any("returned 2 values" in w for w in warnings)

    def test_whitespace_is_stripped_before_padding(self):
        assert poles_loader._clean_county_fips("  12057  ") == "12057"

    def test_non_numeric_string_becomes_none_not_garbage(self):
        """A FIPS code is never anything but digits -- something like
        "N/A" or a stray formula-error string must not silently become a
        5-character, zfill-padded garbage value that could coincidentally
        collide with a real FIPS code."""
        assert poles_loader._clean_county_fips("N/A") is None
        assert poles_loader._clean_county_fips("#ERROR!") is None

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

        assert mock_cursor.executemany.call_count == 1
        sql_text, batch = mock_cursor.executemany.call_args.args
        assert "INSERT INTO #PolesStaging" in sql_text
        assert len(batch) == 1
        assert sql_text.count("?") == len(batch[0]) == 11

    def test_merge_from_staging_is_executed_after_staging_insert(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor, make_pole_record
    ):
        patch_fetch_all_records_poles.return_value = ([make_pole_record()], [])
        poles_loader.load_poles()

        merge_calls = [
            call for call in mock_cursor.execute.call_args_list if "MERGE Poles" in call.args[0]
        ]
        assert len(merge_calls) == 1
        assert "#PolesStaging" in merge_calls[0].args[0]

    def test_insert_column_list_matches_values_list_length(self):
        sql = poles_loader._POLE_UPSERT_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        values_cols = re.search(r"VALUES \(([^)]+)\)", sql, re.DOTALL).group(1)
        assert len(insert_cols.split(",")) == len(values_cols.split(",")) == 11

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


class TestStagingMergeSqlStructure:
    """Structural checks for the bulk staging-table path's SQL constants."""

    def test_staging_table_ddl_has_defensive_guard_and_creates_table(self):
        sql = poles_loader._STAGING_TABLE_SQL
        assert "IF OBJECT_ID('tempdb..#PolesStaging')" in sql
        assert "DROP TABLE #PolesStaging" in sql
        assert "CREATE TABLE #PolesStaging" in sql

    def test_staging_insert_placeholder_count_matches_column_count(self):
        sql = poles_loader._STAGING_INSERT_SQL
        insert_cols = re.search(r"INSERT INTO #PolesStaging \(([^)]+)\)", sql).group(1)
        assert len(insert_cols.split(",")) == sql.count("?") == 11

    def test_merge_from_staging_sources_the_staging_table(self):
        assert "USING #PolesStaging AS source" in poles_loader._MERGE_FROM_STAGING_SQL

    def test_merge_from_staging_match_key_is_id(self):
        assert "ON target.Id = source.Id" in poles_loader._MERGE_FROM_STAGING_SQL

    def test_merge_from_staging_uses_intersect(self):
        assert "INTERSECT" in poles_loader._MERGE_FROM_STAGING_SQL

    def test_merge_from_staging_has_no_placeholders(self):
        """It reads from the already-staged table, not from bound params."""
        assert poles_loader._MERGE_FROM_STAGING_SQL.count("?") == 0

    def test_merge_from_staging_insert_column_list_matches_values(self):
        sql = poles_loader._MERGE_FROM_STAGING_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        values_cols = re.search(r"VALUES \(([^)]+)\)", sql, re.DOTALL).group(1)
        assert len(insert_cols.split(",")) == len(values_cols.split(",")) == 11

    def test_truncate_staging_sql_targets_staging_table(self):
        assert poles_loader._TRUNCATE_STAGING_SQL == "TRUNCATE TABLE #PolesStaging"


class TestPolesBatchingPerformance:
    """
    Covers the fast_executemany batching added to fix the ~12-minute load
    time for 14k+ poles (one cursor.execute() round trip per row was the
    dominant cost, not the Airtable fetch).
    """

    def test_fast_executemany_is_enabled(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor, make_pole_record
    ):
        patch_fetch_all_records_poles.return_value = ([make_pole_record()], [])
        poles_loader.load_poles()
        assert mock_cursor.fast_executemany is True

    def test_batches_are_chunked_by_upsert_batch_size(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor, make_pole_record
    ):
        batch_size = poles_loader._UPSERT_BATCH_SIZE
        records = [make_pole_record(record_id=f"recPole{i}") for i in range(batch_size + 1)]
        patch_fetch_all_records_poles.return_value = (records, [])

        poles_loader.load_poles()

        assert mock_cursor.executemany.call_count == 2
        first_batch = mock_cursor.executemany.call_args_list[0].args[1]
        second_batch = mock_cursor.executemany.call_args_list[1].args[1]
        assert len(first_batch) == batch_size
        assert len(second_batch) == 1

    def test_single_batch_for_small_record_counts(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor, make_pole_record
    ):
        records = [make_pole_record(record_id=f"recPole{i}") for i in range(5)]
        patch_fetch_all_records_poles.return_value = (records, [])

        poles_loader.load_poles()

        assert mock_cursor.executemany.call_count == 1
        batch = mock_cursor.executemany.call_args.args[1]
        assert len(batch) == 5


# --------------------------------------------------------------------------
# load_poles() -- full flow
# --------------------------------------------------------------------------


class TestLoadPolesSuccessFlow:
    def test_requests_only_the_fields_it_needs(
        self, patch_get_connection_poles, patch_fetch_all_records_poles, mock_cursor, make_pole_record
    ):
        """
        Shrinking the Airtable response payload to just the fields
        _map_record_to_pole() reads can meaningfully cut fetch latency on
        a table with many unused columns.
        """
        patch_fetch_all_records_poles.return_value = ([make_pole_record()], [])

        poles_loader.load_poles()

        patch_fetch_all_records_poles.assert_called_once_with(
            poles_loader.AIRTABLE_POLES_TABLE, fields=poles_loader.AIRTABLE_POLES_FIELDS
        )

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
        # insert SP_Execution, staging table create, merge-from-staging,
        # truncate staging, final update (upserts themselves go via
        # executemany into the staging table)
        assert len(calls) == 5

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoles", "Dev", "AirTable")
        assert DTO_PATTERN.match(start_time)

        staging_create_sql = calls[1].args[0]
        assert "CREATE TABLE #PolesStaging" in staging_create_sql

        merge_sql = calls[2].args[0]
        assert "MERGE Poles" in merge_sql
        assert "#PolesStaging" in merge_sql

        truncate_sql = calls[3].args[0]
        assert "TRUNCATE TABLE #PolesStaging" in truncate_sql

        assert mock_cursor.executemany.call_count == 1
        staging_insert_sql, batch = mock_cursor.executemany.call_args.args
        assert "INSERT INTO #PolesStaging" in staging_insert_sql
        assert len(batch) == 2
        assert batch[0][0] == "recPole1"
        assert batch[0][3] == "12057"  # CountyFips position
        assert batch[0][9] == 7  # SP_ExecId position
        assert batch[1][0] == "recPole2"
        assert batch[1][9] == 7

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[4].args
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
        assert len(calls) == 2  # insert + final update -- no staging table needed for zero rows
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[1].args
        assert (success, errors, batch_count) == (0, 0, 1)
        mock_cursor.executemany.assert_not_called()


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
        # The chunk's bulk staging+merge fails, so load_poles() falls back
        # to executing each row individually; the second one fails there.
        mock_cursor.executemany.side_effect = RuntimeError("chunk failed")
        mock_cursor.execute.side_effect = [
            None,  # insert SP_Execution
            None,  # staging table create
            None,  # truncate after chunk failure
            None,  # row1 fallback succeeds
            RuntimeError("bad row"),  # row2 fallback fails
            None,  # final update
        ]

        poles_loader.load_poles()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)

    def test_chunk_failure_falls_back_to_row_by_row_execute(
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
        mock_cursor.executemany.side_effect = RuntimeError("chunk failed")
        mock_cursor.execute.side_effect = [None] * 6  # insert, staging create, truncate, row1, row2, final update

        poles_loader.load_poles()

        # insert, staging create, truncate-after-failure, 2 fallback row
        # upserts, final update
        assert mock_cursor.execute.call_count == 6
        row1_args = mock_cursor.execute.call_args_list[3].args
        row2_args = mock_cursor.execute.call_args_list[4].args
        assert row1_args[1][0] == "recPole1"
        assert row2_args[1][0] == "recPole2"

    def test_staging_table_is_truncated_before_row_by_row_fallback(
        self,
        patch_get_connection_poles,
        patch_fetch_all_records_poles,
        mock_cursor,
        make_pole_record,
    ):
        """
        A partially-staged chunk (some rows may have inserted into
        #PolesStaging before the failure) must be cleared before falling
        back to row-by-row, so a retried run doesn't re-merge stale rows.
        """
        patch_fetch_all_records_poles.return_value = ([make_pole_record(record_id="recPole1")], [])
        mock_cursor.executemany.side_effect = RuntimeError("chunk failed")
        mock_cursor.execute.side_effect = [None] * 5  # insert, staging create, truncate, row1, final update

        poles_loader.load_poles()

        truncate_calls = [
            call
            for call in mock_cursor.execute.call_args_list
            if call.args[0] == "TRUNCATE TABLE #PolesStaging"
        ]
        assert len(truncate_calls) == 1


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
