"""Tests for shared/pole_telemetry_loader.py"""

import json
import re

import pytest

from shared import pole_telemetry_loader

DTO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$")


# --------------------------------------------------------------------------
# _capitalize_key
# --------------------------------------------------------------------------


class TestCapitalizeKey:
    def test_camel_case_becomes_pascal_case(self):
        assert pole_telemetry_loader._capitalize_key("lastUpload") == "LastUpload"
        assert pole_telemetry_loader._capitalize_key("productName") == "ProductName"
        assert pole_telemetry_loader._capitalize_key("batteryVoltage1") == "BatteryVoltage1"

    def test_does_not_lowercase_the_rest_of_the_string(self):
        """
        Regression guard: Python's str.capitalize() would turn "lastUpload"
        into "Lastupload" (lowercasing everything after the first letter),
        which breaks PascalCase. _capitalize_key must only touch the first
        character.
        """
        assert pole_telemetry_loader._capitalize_key("lastUpload") != "lastUpload".capitalize()

    def test_empty_string_is_unchanged(self):
        assert pole_telemetry_loader._capitalize_key("") == ""


# --------------------------------------------------------------------------
# _parse_iso_datetime
# --------------------------------------------------------------------------


class TestParseIsoDatetime:
    def test_none_returns_none(self):
        assert pole_telemetry_loader._parse_iso_datetime(None) is None

    def test_empty_string_returns_none(self):
        assert pole_telemetry_loader._parse_iso_datetime("") is None

    def test_confirmed_leadsun_format_parses(self):
        """Exact format confirmed from a real Leadsun response."""
        result = pole_telemetry_loader._parse_iso_datetime("2026-07-15T12:35:30.000+00:00")
        assert result == "2026-07-15 12:35:30.000 +00:00"

    def test_z_suffixed_utc_string_parses(self):
        result = pole_telemetry_loader._parse_iso_datetime("2026-07-02T18:00:00.000Z")
        assert DTO_PATTERN.match(result)

    def test_garbage_string_returns_none(self):
        assert pole_telemetry_loader._parse_iso_datetime("not-a-date") is None

    def test_unexpected_type_returns_none_instead_of_raising(self):
        assert pole_telemetry_loader._parse_iso_datetime(object()) is None


# --------------------------------------------------------------------------
# _map_lamp_record -- against the real confirmed Leadsun response shape
# --------------------------------------------------------------------------


class TestMapLampRecord:
    def test_product_name_renamed_to_location_id(self, make_lamp_record):
        record = make_lamp_record(product_name="12009-1000")
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LocationId"] == "12009-1000"
        assert "ProductName" not in result

    def test_last_upload_parsed(self, make_lamp_record):
        record = make_lamp_record(last_upload="2026-07-15T12:35:30.000+00:00")
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LastUpload"] == "2026-07-15 12:35:30.000 +00:00"

    def test_leadsun_id_renamed_from_bare_id(self, make_lamp_record):
        """
        Leadsun's own "id" must not land in a column called "Id" -- that
        would look like this table's primary key (it isn't).
        """
        record = make_lamp_record()
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LeadsunId"] == 10358
        assert "Id" not in result

    def test_leadsun_project_fields_renamed(self, make_lamp_record):
        """
        Leadsun's own "projectId"/"projectName" must not land in columns
        called "ProjectId"/"ProjectName" -- those would look like a
        reference to our own Airtable-sourced Projects table.
        """
        record = make_lamp_record()
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LeadsunProjectId"] == 482
        assert result["LeadsunProjectName"] == "Chaparral"
        assert "ProjectId" not in result
        assert "ProjectName" not in result

    def test_product_id_is_kept_distinct_from_product_name(self, make_lamp_record):
        """productId (Leadsun's own product identifier string) is a
        different field from productName (-> LocationId) and should not be
        confused with it."""
        record = make_lamp_record()
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["ProductId"] == "AE3SAP7323113143"
        assert result["LocationId"] == "12009-1000"

    def test_lighting_state_trailing_space_is_trimmed(self, make_lamp_record):
        record = make_lamp_record()
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LightingState"] == "lighting-off"

    def test_all_string_fields_are_trimmed(self, make_lamp_record):
        record = make_lamp_record(extra_fields={"userName": "  spacey-user  "})
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["UserName"] == "spacey-user"

    def test_numeric_fields_pass_through(self, make_lamp_record):
        record = make_lamp_record()
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["BatteryVoltage1"] == 13.52
        assert result["Longitude"] == -80.7236
        assert result["Latitude"] == 27.99507
        assert result["IsOnline"] is True
        assert result["DcInState"] == 3

    def test_null_create_time_stays_none(self, make_lamp_record):
        record = make_lamp_record()  # createTime is None in the confirmed sample
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["CreateTime"] is None

    def test_all_known_fields_from_real_sample_produce_empty_extra_json(self, make_lamp_record):
        """
        The confirmed sample record has no fields outside _ALL_COLUMNS, so
        ExtraFieldsJson should be empty/None for it.
        """
        record = make_lamp_record()
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["ExtraFieldsJson"] is None

    def test_unexpected_field_is_captured_in_extra_fields_json(self, make_lamp_record):
        record = make_lamp_record(extra_fields={"brandNewSensorField": 42})
        result = pole_telemetry_loader._map_lamp_record(record)
        extra = json.loads(result["ExtraFieldsJson"])
        assert extra["BrandNewSensorField"] == 42

    def test_missing_product_name_becomes_none_location_id(self):
        record = {"lastUpload": "2026-01-01T00:00:00Z"}
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LocationId"] is None

    def test_missing_last_upload_gets_sentinel_not_none(self, make_lamp_record):
        """
        LastUpload is part of the primary key, so it can't be NULL --
        a genuinely missing value gets the stable far-future sentinel
        instead of being dropped as an error.
        """
        record = {"productName": "POLE-1"}
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LastUpload"] == pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL

    def test_null_last_upload_gets_sentinel(self, make_lamp_record):
        record = make_lamp_record(last_upload=None)
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LastUpload"] == pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL

    def test_sentinel_is_stable_across_repeated_calls(self, make_lamp_record):
        """
        The sentinel must be the SAME value every time (not e.g. "now"),
        so a device that keeps reporting a null lastUpload gets its one
        row updated in place on each run rather than a new row inserted
        every cycle.
        """
        record = make_lamp_record(last_upload=None)
        first = pole_telemetry_loader._map_lamp_record(record)["LastUpload"]
        second = pole_telemetry_loader._map_lamp_record(record)["LastUpload"]
        assert first == second

    def test_sentinel_matches_dto_format(self, make_lamp_record):
        record = make_lamp_record(last_upload=None)
        result = pole_telemetry_loader._map_lamp_record(record)
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$", result["LastUpload"])

    def test_present_but_unparseable_last_upload_stays_none_not_sentinel(self, make_lamp_record):
        """
        A non-null LastUpload that fails to parse is a real bug/format
        surprise, not a legitimately-missing value -- it should still
        surface as a row-level error (via None), not get silently
        sentineled over.
        """
        record = make_lamp_record(last_upload="not-a-real-timestamp")
        result = pole_telemetry_loader._map_lamp_record(record)
        assert result["LastUpload"] is None


class TestBuildRow:
    def test_row_length_matches_all_columns(self, make_lamp_record):
        mapped = pole_telemetry_loader._map_lamp_record(make_lamp_record())
        row = pole_telemetry_loader._build_row(mapped, sp_exec_id=42)
        assert len(row) == len(pole_telemetry_loader._ALL_COLUMNS)

    def test_row_order_matches_all_columns(self, make_lamp_record):
        mapped = pole_telemetry_loader._map_lamp_record(make_lamp_record(product_name="LOC-X"))
        row = pole_telemetry_loader._build_row(mapped, sp_exec_id=99)

        as_dict = dict(zip(pole_telemetry_loader._ALL_COLUMNS, row))
        assert as_dict["LocationId"] == "LOC-X"
        assert as_dict["Source"] == "Leadsun"
        assert as_dict["SP_ExecId"] == 99


# --------------------------------------------------------------------------
# Staging / MERGE SQL structural checks
# --------------------------------------------------------------------------


class TestStagingMergeSqlStructure:
    def test_staging_table_ddl_has_guard_and_matches_all_columns(self):
        sql = pole_telemetry_loader._STAGING_TABLE_SQL
        assert "IF OBJECT_ID('tempdb..#PoleTelemetryStaging')" in sql
        match = re.search(r"CREATE TABLE #PoleTelemetryStaging \((.+)\);", sql, re.DOTALL)
        cols = [line.strip().split()[0] for line in match.group(1).strip().split(",")]
        assert cols == pole_telemetry_loader._ALL_COLUMNS

    def test_staging_insert_placeholder_count_matches_all_columns(self):
        sql = pole_telemetry_loader._STAGING_INSERT_SQL
        assert sql.count("?") == len(pole_telemetry_loader._ALL_COLUMNS)

    def test_merge_from_staging_match_key_is_location_and_last_upload(self):
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        assert "target.LocationId = source.LocationId" in sql
        assert "target.LastUpload = source.LastUpload" in sql

    def test_merge_from_staging_uses_intersect(self):
        assert "INTERSECT" in pole_telemetry_loader._MERGE_FROM_STAGING_SQL

    def test_merge_from_staging_casts_extra_fields_json_to_avoid_ntext_bug(self):
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        assert "CAST(target.ExtraFieldsJson AS NVARCHAR(MAX))" in sql
        assert "CAST(source.ExtraFieldsJson AS NVARCHAR(MAX))" in sql

    def test_merge_from_staging_insert_and_update_cover_all_non_key_columns(self):
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        cols = {c.strip() for c in insert_cols.split(",")}
        assert cols == set(pole_telemetry_loader._ALL_COLUMNS)

    def test_row_upsert_placeholder_count_matches_all_columns(self):
        sql = pole_telemetry_loader._ROW_UPSERT_SQL
        assert sql.count("?") == len(pole_telemetry_loader._ALL_COLUMNS)

    def test_sp_exec_id_excluded_from_diff_check_but_present_in_update(self):
        assert "SP_ExecId" not in pole_telemetry_loader._DIFF_CHECK_COLUMNS
        assert "SP_ExecId" in pole_telemetry_loader._NON_KEY_COLUMNS

    def test_retention_purge_uses_configured_month_count(self):
        sql = pole_telemetry_loader._RETENTION_PURGE_SQL
        assert f"-{pole_telemetry_loader.RETENTION_MONTHS}" in sql
        assert "LastUpload <" in sql
        assert "SYSDATETIMEOFFSET()" in sql

    def test_missing_last_upload_sentinel_is_never_eligible_for_retention_purge(self):
        """
        The whole point of the sentinel is that it's far enough in the
        future that `LastUpload < DATEADD(MONTH, -N, SYSDATETIMEOFFSET())`
        can never be true for it, for any reasonable retention window --
        i.e. it never gets purged. Guard against someone "fixing" the
        sentinel to a near-future or past date and silently breaking that.
        """
        sentinel_year = int(pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL[:4])
        from datetime import datetime as _dt

        assert sentinel_year > _dt.now().year + 50

    def test_no_fk_references(self):
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        assert "REFERENCES" not in sql


# --------------------------------------------------------------------------
# load_pole_telemetry() -- full flow
# --------------------------------------------------------------------------


class TestLoadPoleTelemetrySuccessFlow:
    def test_full_success_flow_two_records(
        self,
        patch_get_connection_pole_telemetry,
        patch_fetch_lamps,
        mock_conn,
        mock_cursor,
        make_lamp_record,
    ):
        mock_cursor.fetchone.return_value = (11,)
        record1 = make_lamp_record(product_name="POLE-1")
        record2 = make_lamp_record(product_name="POLE-2")
        patch_fetch_lamps.return_value = [record1, record2]

        pole_telemetry_loader.load_pole_telemetry()

        calls = mock_cursor.execute.call_args_list
        # insert SP_Execution, staging create, merge-from-staging, truncate,
        # retention purge, final update
        assert len(calls) == 6

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleTelemetry", "Dev", "Leadsun")
        assert DTO_PATTERN.match(start_time)

        assert "CREATE TABLE #PoleTelemetryStaging" in calls[1].args[0]
        assert "MERGE PoleTelemetry" in calls[2].args[0]
        assert calls[3].args[0] == "TRUNCATE TABLE #PoleTelemetryStaging"
        assert "DELETE FROM PoleTelemetry" in calls[4].args[0]

        assert mock_cursor.executemany.call_count == 1
        staging_sql, batch = mock_cursor.executemany.call_args.args
        assert "INSERT INTO #PoleTelemetryStaging" in staging_sql
        assert len(batch) == 2
        assert batch[0][0] == "POLE-1"
        assert batch[0][3] == 11  # SP_ExecId position
        assert batch[1][0] == "POLE-2"

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[5].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 11)
        assert DTO_PATTERN.match(end_time)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_empty_lamp_list_still_closes_out_execution_row(
        self, patch_get_connection_pole_telemetry, patch_fetch_lamps, mock_cursor
    ):
        patch_fetch_lamps.return_value = []

        pole_telemetry_loader.load_pole_telemetry()

        calls = mock_cursor.execute.call_args_list
        # insert, retention purge, final update -- no staging table needed
        assert len(calls) == 3
        assert "DELETE FROM PoleTelemetry" in calls[1].args[0]
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[2].args
        assert (success, errors, batch_count) == (0, 0, 1)
        mock_cursor.executemany.assert_not_called()

    def test_records_missing_location_id_or_last_upload_are_counted_as_errors(
        self, patch_get_connection_pole_telemetry, patch_fetch_lamps, mock_cursor, make_lamp_record
    ):
        good_record = make_lamp_record(product_name="POLE-1")
        bad_record = {"lastUpload": "2026-01-01T00:00:00Z"}  # missing productName
        patch_fetch_lamps.return_value = [good_record, bad_record]

        pole_telemetry_loader.load_pole_telemetry()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)

    def test_null_last_upload_record_is_retained_not_dropped(
        self, patch_get_connection_pole_telemetry, patch_fetch_lamps, mock_cursor, make_lamp_record
    ):
        """
        A record with a genuinely-missing lastUpload (LocationId still
        present) must be upserted using the sentinel, not counted as an
        error -- this is the whole point of the sentinel.
        """
        record_with_null_upload = make_lamp_record(product_name="POLE-NEVER-UPLOADED", last_upload=None)
        patch_fetch_lamps.return_value = [record_with_null_upload]

        pole_telemetry_loader.load_pole_telemetry()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 0)

        staging_sql, batch = mock_cursor.executemany.call_args.args
        assert batch[0][0] == "POLE-NEVER-UPLOADED"
        assert batch[0][1] == pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL

    def test_retention_purge_logs_rowcount(
        self, patch_get_connection_pole_telemetry, patch_fetch_lamps, mock_cursor, caplog
    ):
        patch_fetch_lamps.return_value = []
        mock_cursor.rowcount = 42

        with caplog.at_level("INFO"):
            pole_telemetry_loader.load_pole_telemetry()

        messages = [rec.message for rec in caplog.records]
        assert any("purged 42 record(s)" in m for m in messages)


class TestLoadPoleTelemetryPartialFailure:
    def test_chunk_failure_falls_back_to_row_by_row(
        self,
        patch_get_connection_pole_telemetry,
        patch_fetch_lamps,
        mock_cursor,
        make_lamp_record,
    ):
        patch_fetch_lamps.return_value = [
            make_lamp_record(product_name="POLE-1"),
            make_lamp_record(product_name="POLE-2"),
        ]
        mock_cursor.executemany.side_effect = RuntimeError("chunk failed")
        # insert, staging create, truncate-after-failure, row1, row2 (fails),
        # retention purge, final update
        mock_cursor.execute.side_effect = [
            None, None, None, None, RuntimeError("bad row"), None, None,
        ]

        pole_telemetry_loader.load_pole_telemetry()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)


class TestLoadPoleTelemetryTopLevelFailure:
    def test_fetch_failure_updates_error_message_and_reraises(
        self, patch_get_connection_pole_telemetry, patch_fetch_lamps, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (11,)
        patch_fetch_lamps.side_effect = RuntimeError("leadsun api is down")

        with pytest.raises(RuntimeError, match="leadsun api is down"):
            pole_telemetry_loader.load_pole_telemetry()

        error_update_calls = [
            call for call in mock_cursor.execute.call_args_list if "ErrorMessage" in call.args[0]
        ]
        assert len(error_update_calls) == 1
        _, _end_time, err_msg, _success, _errors, sp_exec_id = error_update_calls[0].args
        assert err_msg == "leadsun api is down"
        assert sp_exec_id == 11

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
