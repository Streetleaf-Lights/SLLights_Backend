"""Tests for shared/pole_telemetry_loader.py"""

import json
import re
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from shared import pole_telemetry_loader

EASTERN = ZoneInfo("America/New_York")
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
        row = pole_telemetry_loader._build_row(mapped, sp_exec_id=42, is_open_issue_fault=False)
        assert len(row) == len(pole_telemetry_loader._ALL_COLUMNS)

    def test_row_order_matches_all_columns(self, make_lamp_record):
        mapped = pole_telemetry_loader._map_lamp_record(make_lamp_record(product_name="LOC-X"))
        row = pole_telemetry_loader._build_row(mapped, sp_exec_id=99, is_open_issue_fault=True)

        as_dict = dict(zip(pole_telemetry_loader._ALL_COLUMNS, row))
        assert as_dict["LocationId"] == "LOC-X"
        assert as_dict["Source"] == "Leadsun"
        assert as_dict["SP_ExecId"] == 99
        assert as_dict["IsOpenIssueFault"] is True


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
        # insert SP_Execution, open-issues lookup, staging create,
        # merge-from-staging, truncate, retention purge, final update
        assert len(calls) == 7

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleTelemetry", "Dev", "Leadsun")
        assert DTO_PATTERN.match(start_time)

        assert "JOIN PoleOpenIssues" in calls[1].args[0]
        assert "CREATE TABLE #PoleTelemetryStaging" in calls[2].args[0]
        assert "MERGE PoleTelemetry" in calls[3].args[0]
        assert calls[4].args[0] == "TRUNCATE TABLE #PoleTelemetryStaging"
        assert "DELETE FROM PoleTelemetry" in calls[5].args[0]

        assert mock_cursor.executemany.call_count == 1
        staging_sql, batch = mock_cursor.executemany.call_args.args
        assert "INSERT INTO #PoleTelemetryStaging" in staging_sql
        assert len(batch) == 2
        assert batch[0][0] == "POLE-1"
        assert batch[0][3] == 11  # SP_ExecId position
        assert batch[1][0] == "POLE-2"

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[6].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 11)
        assert DTO_PATTERN.match(end_time)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_empty_lamp_list_still_closes_out_execution_row(
        self, patch_get_connection_pole_telemetry, patch_fetch_lamps, mock_cursor
    ):
        patch_fetch_lamps.return_value = []
        mock_cursor.fetchall.return_value = []  # no LocationIds with open issues

        pole_telemetry_loader.load_pole_telemetry()

        calls = mock_cursor.execute.call_args_list
        # insert, open-issues lookup, retention purge, final update -- no
        # staging table needed
        assert len(calls) == 4
        assert "JOIN PoleOpenIssues" in calls[1].args[0]
        assert "DELETE FROM PoleTelemetry" in calls[2].args[0]
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[3].args
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
        # insert, open-issues lookup, staging create, truncate-after-failure,
        # row1, row2 (fails), retention purge, final update
        mock_cursor.execute.side_effect = [
            None, None, None, None, None, RuntimeError("bad row"), None, None,
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


# --------------------------------------------------------------------------
# backfill_is_open_issue_fault_for_all_poles() -- one-off correction for
# IsOpenIssueFault values already written wrong on existing PoleTelemetry
# rows, before PoleOpenIssues.PoleId was fixed to source from Airtable's
# "PoleRecordID" field.
# --------------------------------------------------------------------------


class TestBackfillIsOpenIssueFaultPerPoleSqlStructure:
    def test_has_no_global_last_upload_cutoff(self):
        """Same defining property as pole_vitals_loader.py's own
        backfill_last_48_hours_of_hour_for_all_poles(): no
        "t.LastUpload >= ?" anywhere -- each pole's own window is
        determined entirely from its own data, not a value relative to
        "now"."""
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert "LastUpload >= ?" not in sql

    def test_excludes_the_sentinel_last_upload_value(self):
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert "WHERE t.LastUpload <> ?" in sql

    def test_finds_each_poles_own_max_last_upload(self):
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        max_reading_cte = sql.split("MaxReadingPerPole AS (")[1].split(")\nUPDATE")[0]
        assert "MAX(t.LastUpload) AS MaxLastUpload" in max_reading_cte
        assert "GROUP BY t.LocationId" in max_reading_cte

    def test_scopes_each_poles_correction_to_a_48_hour_range_ending_at_its_own_max(self):
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert "JOIN MaxReadingPerPole mr ON t.LocationId = mr.LocationId" in sql
        assert "WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)" in sql
        assert "AND t.LastUpload <= mr.MaxLastUpload" in sql

    def test_joins_pole_open_issues_via_the_corrected_pole_id_column(self):
        """This is exactly the join that was broken -- confirms the
        backfill uses the SAME (now-corrected) column
        pole_telemetry_loader.py's own
        _fetch_location_ids_with_open_issues() does, not some
        independently-written path that could disagree with it."""
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert "JOIN PoleOpenIssues poi ON poi.PoleId = p.Id" in sql

    def test_only_updates_rows_where_the_value_would_actually_change(self):
        """Avoids rewriting rows that already have the correct value --
        same "don't touch what's already right" convention as this
        project's own MERGE-based upserts elsewhere."""
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert (
            "AND ISNULL(t.IsOpenIssueFault, 0) <> CASE WHEN loi.LocationId IS NOT NULL THEN 1 ELSE 0 END"
            in sql
        )

    def test_sets_one_when_matched_zero_when_not(self):
        sql = pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert (
            "SET t.IsOpenIssueFault = CASE WHEN loi.LocationId IS NOT NULL THEN 1 ELSE 0 END" in sql
        )


class TestBackfillIsOpenIssueFaultForAllPolesSuccessFlow:
    def test_full_success_flow_call_sequence(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.rowcount = 137

        pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 3

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("backfillIsOpenIssueFault", "Dev", "Leadsun")

        update_sql, sentinel = calls[1].args
        assert update_sql == pole_telemetry_loader._BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL
        assert sentinel == pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL

        final_sql, end_time, success, errors, batch_count, sp_exec_id = calls[2].args
        assert "UPDATE SP_Execution" in final_sql
        assert (success, errors, batch_count, sp_exec_id) == (137, 0, 1, 99)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_zero_rowcount_does_not_go_negative(
        self, patch_get_connection_pole_telemetry, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (0, 0)


class TestBackfillIsOpenIssueFaultForAllPolesTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db connection lost")

        with pytest.raises(RuntimeError, match="db connection lost"):
            pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_genuine_update_failure_reraises(
        self, patch_get_connection_pole_telemetry, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        mock_cursor.execute.side_effect = [None, RuntimeError("deadlock")]

        with pytest.raises(RuntimeError, match="deadlock"):
            pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles()


class TestBackfillIsOpenIssueFaultFailureRecordingUsesAFreshConnection:
    def _make_conn(self):
        conn = MagicMock(name="conn")
        cursor = MagicMock(name="cursor")
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_original_exception_still_raised_when_recording_succeeds(self, mocker):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.execute.side_effect = [None, RuntimeError("communication link failure")]

        recovery_conn, recovery_cursor = self._make_conn()

        mocker.patch(
            "shared.pole_telemetry_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="communication link failure"):
            pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles()

        assert recovery_cursor.execute.called
        update_sql, end_time, error_message, success, errors, sp_exec_id = (
            recovery_cursor.execute.call_args.args
        )
        assert "UPDATE SP_Execution" in update_sql
        assert "communication link failure" in error_message
        assert sp_exec_id == 55
        recovery_conn.commit.assert_called_once()
        recovery_cursor.close.assert_called_once()
        recovery_conn.close.assert_called_once()
        main_cursor.close.assert_called_once()
        main_conn.close.assert_called_once()

    def test_original_exception_still_raised_when_recording_also_fails(self, mocker, caplog):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.execute.side_effect = [None, RuntimeError("original communication failure")]

        recovery_conn, recovery_cursor = self._make_conn()
        recovery_cursor.execute.side_effect = RuntimeError("recovery also failed")

        mocker.patch(
            "shared.pole_telemetry_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="original communication failure"):
                pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles()

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("original communication failure" in msg for msg in error_messages)
        assert any(
            "additionally failed to record this run's failure" in msg and "recovery also failed" in msg
            for msg in error_messages
        )


# --------------------------------------------------------------------------
# _aggregate_telemetry_by_leadsun_project() -- pure grouping/reshaping
# logic, no database involved -- see that function's own docstring for
# the full field-mapping reasoning this locks in.
# --------------------------------------------------------------------------


def _telemetry_row(
    leadsun_project_id=482,
    leadsun_project_name="Chaparral",
    user_name="12009-brevard",
    group_id=1149,
    group_name="Chaparral Ph3",
    gateway_code="GT18L94A25082883",
    leadsun_id=10358,
    location_id="12009-1000",
    controller_code="A3P70LA323110598",
    product_id="AE3SAP7323113143",
):
    """Matches _FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL's own column
    order exactly."""
    return (
        leadsun_project_id,
        leadsun_project_name,
        user_name,
        group_id,
        group_name,
        gateway_code,
        leadsun_id,
        location_id,
        controller_code,
        product_id,
    )


class TestAggregateTelemetryByLeadsunProject:
    def test_single_reading_produces_expected_shape(self):
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(
            [_telemetry_row()]
        )

        assert list(result.keys()) == ["482"]
        project = result["482"]
        assert project["ProjectName"] == "Chaparral"
        assert project["UserName"] == "12009-brevard"
        assert len(project["groups"]) == 1

        group = project["groups"][0]
        assert group["GroupId"] == 1149
        assert group["GroupName"] == "Chaparral Ph3"
        assert group["GatewayCode"] == "GT18L94A25082883"
        assert len(group["products"]) == 1

        product = group["products"][0]
        assert product == {
            "ProductId": 10358,
            "ProductName": "12009-1000",
            "ControllerCode": "A3P70LA323110598",
            "ProvidedProductId": "AE3SAP7323113143",
        }

    def test_field_mapping_disambiguates_id_from_provided_product_id(self):
        """Regression guard for the exact confusion this structure was
        designed to resolve: ProductId is Leadsun's raw "id"/LeadsunId (a
        plain integer), ProvidedProductId is Leadsun's raw "productId"
        (a separate, alphanumeric value) -- confirmed against a real
        /lamps response where these two are genuinely different values
        for the same pole, not two names for the same thing."""
        row = _telemetry_row(leadsun_id=10358, product_id="AE3SAP7323113143")
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project([row])
        product = result["482"]["groups"][0]["products"][0]
        assert product["ProductId"] == 10358
        assert product["ProvidedProductId"] == "AE3SAP7323113143"
        assert product["ProductId"] != product["ProvidedProductId"]

    def test_project_key_is_a_string_not_the_original_int(self):
        """Matches how Projects.LeadsunProject's own "ProjectId" is
        stored -- a JSON STRING, from Airtable, via json.dumps() -- even
        though PoleTelemetry.LeadsunProjectId itself is a plain INT
        column. Both sides need to agree on one representation to match
        correctly later in update_leadsun_project_details()."""
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(
            [_telemetry_row(leadsun_project_id=482)]
        )
        assert "482" in result
        assert 482 not in result

    def test_multiple_products_in_the_same_group_are_both_kept(self):
        rows = [
            _telemetry_row(leadsun_id=1, location_id="LOC-1", product_id="PROD-1"),
            _telemetry_row(leadsun_id=2, location_id="LOC-2", product_id="PROD-2"),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        products = result["482"]["groups"][0]["products"]
        assert len(products) == 2
        assert {p["ProductId"] for p in products} == {1, 2}

    def test_multiple_groups_in_the_same_project_are_both_kept(self):
        rows = [
            _telemetry_row(group_id=100, group_name="Group A", leadsun_id=1),
            _telemetry_row(group_id=200, group_name="Group B", leadsun_id=2),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        groups = result["482"]["groups"]
        assert len(groups) == 2
        assert {g["GroupId"] for g in groups} == {100, 200}

    def test_multiple_projects_are_each_aggregated_independently(self):
        rows = [
            _telemetry_row(leadsun_project_id=1, leadsun_project_name="Project One"),
            _telemetry_row(leadsun_project_id=2, leadsun_project_name="Project Two"),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        assert set(result.keys()) == {"1", "2"}
        assert result["1"]["ProjectName"] == "Project One"
        assert result["2"]["ProjectName"] == "Project Two"

    def test_row_with_null_leadsun_project_id_is_skipped(self):
        rows = [_telemetry_row(leadsun_project_id=None), _telemetry_row(leadsun_project_id=482)]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        assert list(result.keys()) == ["482"]

    def test_row_with_null_group_id_is_skipped(self):
        rows = [_telemetry_row(group_id=None), _telemetry_row(group_id=1149)]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        assert len(result["482"]["groups"]) == 1

    def test_empty_input_produces_empty_result(self):
        assert pole_telemetry_loader._aggregate_telemetry_by_leadsun_project([]) == {}

    def test_total_gateways_counts_distinct_groups(self):
        rows = [
            _telemetry_row(group_id=100, leadsun_id=1),
            _telemetry_row(group_id=200, leadsun_id=2),
            _telemetry_row(group_id=300, leadsun_id=3),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        assert result["482"]["totalGateways"] == 3

    def test_total_poles_at_project_level_sums_across_all_groups(self):
        rows = [
            _telemetry_row(group_id=100, leadsun_id=1),
            _telemetry_row(group_id=100, leadsun_id=2),
            _telemetry_row(group_id=200, leadsun_id=3),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        assert result["482"]["totalPoles"] == 3

    def test_total_poles_at_group_level_counts_only_that_group(self):
        rows = [
            _telemetry_row(group_id=100, leadsun_id=1),
            _telemetry_row(group_id=100, leadsun_id=2),
            _telemetry_row(group_id=200, leadsun_id=3),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        groups_by_id = {g["GroupId"]: g for g in result["482"]["groups"]}
        assert groups_by_id[100]["totalPoles"] == 2
        assert groups_by_id[200]["totalPoles"] == 1

    def test_total_gateways_and_poles_are_independent_per_project(self):
        rows = [
            _telemetry_row(leadsun_project_id=1, group_id=10, leadsun_id=1),
            _telemetry_row(leadsun_project_id=1, group_id=20, leadsun_id=2),
            _telemetry_row(leadsun_project_id=2, group_id=30, leadsun_id=3),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        assert result["1"]["totalGateways"] == 2
        assert result["1"]["totalPoles"] == 2
        assert result["2"]["totalGateways"] == 1
        assert result["2"]["totalPoles"] == 1

    def test_duplicate_product_within_a_group_is_not_double_counted(self):
        """A second reading for the same pole (same leadsun_id) within
        the same group overwrites the first entry rather than adding a
        second one -- totalPoles must reflect that same deduplication,
        not the raw row count."""
        rows = [
            _telemetry_row(group_id=100, leadsun_id=1, location_id="LOC-A"),
            _telemetry_row(group_id=100, leadsun_id=1, location_id="LOC-A-updated"),
        ]
        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)
        group = result["482"]["groups"][0]
        assert group["totalPoles"] == 1
        assert result["482"]["totalPoles"] == 1

    def test_real_dataset_total_poles_matches_total_products(self):
        """Regression guard using the same real 11,837-record fixture as
        test_real_dataset_matches_the_known_totals -- totalPoles summed
        across every project must equal the same total_products count
        that test already validates independently."""
        import json as jsonlib
        import os as oslib

        fixture_path = oslib.path.join(
            oslib.path.dirname(__file__), "fixtures", "leadsun_lamps_sample.json"
        )
        if not oslib.path.exists(fixture_path):
            pytest.skip("Real-data fixture not present in this environment")

        with open(fixture_path) as f:
            records = jsonlib.load(f)

        rows = [
            (
                r.get("projectId"), r.get("projectName"), r.get("userName"),
                r.get("groupId"), r.get("groupName"), r.get("gatewayCode"),
                r.get("id"), r.get("productName"), r.get("controllerCode"),
                r.get("productId"),
            )
            for r in records
        ]

        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)

        total_poles_via_project_summary = sum(p["totalPoles"] for p in result.values())
        total_poles_via_group_summary = sum(
            group["totalPoles"] for project in result.values() for group in project["groups"]
        )
        assert total_poles_via_project_summary == len(records)
        assert total_poles_via_group_summary == len(records)

    def test_real_dataset_matches_the_known_totals(self):
        """Regression guard using the exact numbers this function was
        validated against, from a real 11,837-record Leadsun /lamps
        response: 176 distinct projects, and every single reading
        accounted for exactly once across all groups/products (no loss,
        no duplication)."""
        import json as jsonlib
        import os as oslib

        fixture_path = oslib.path.join(
            oslib.path.dirname(__file__), "fixtures", "leadsun_lamps_sample.json"
        )
        if not oslib.path.exists(fixture_path):
            pytest.skip("Real-data fixture not present in this environment")

        with open(fixture_path) as f:
            records = jsonlib.load(f)

        rows = [
            (
                r.get("projectId"), r.get("projectName"), r.get("userName"),
                r.get("groupId"), r.get("groupName"), r.get("gatewayCode"),
                r.get("id"), r.get("productName"), r.get("controllerCode"),
                r.get("productId"),
            )
            for r in records
        ]

        result = pole_telemetry_loader._aggregate_telemetry_by_leadsun_project(rows)

        assert len(result) == 176
        total_products = sum(
            len(group["products"]) for project in result.values() for group in project["groups"]
        )
        assert total_products == len(records)


# --------------------------------------------------------------------------
# update_leadsun_project_details()
# --------------------------------------------------------------------------


class TestUpdateLeadsunProjectDetailsSuccessFlow:
    def test_matching_project_gets_updated_with_full_json(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [
            [("recProj1", "482")],  # Projects with a Leadsun ProjectID recorded
            [_telemetry_row()],  # matching telemetry
        ]

        pole_telemetry_loader.update_leadsun_project_details()

        update_call = next(
            c for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_telemetry_loader._UPDATE_PROJECT_LEADSUN_PROJECT_SQL
        )
        leadsun_project_json, project_id = update_call.args[1:]
        assert project_id == "recProj1"
        parsed = json.loads(leadsun_project_json)
        assert parsed["ProjectId"] == "482"
        assert parsed["ProjectName"] == "Chaparral"
        assert parsed["UserName"] == "12009-brevard"
        assert len(parsed["groups"]) == 1

    def test_written_json_includes_total_gateways_and_poles(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [
            [("recProj1", "482")],
            [
                _telemetry_row(group_id=100, leadsun_id=1),
                _telemetry_row(group_id=200, leadsun_id=2),
            ],
        ]

        pole_telemetry_loader.update_leadsun_project_details()

        update_call = next(
            c for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_telemetry_loader._UPDATE_PROJECT_LEADSUN_PROJECT_SQL
        )
        parsed = json.loads(update_call.args[1])
        assert parsed["totalGateways"] == 2
        assert parsed["totalPoles"] == 2

    def test_project_with_no_matching_telemetry_is_left_untouched(
        self, patch_get_connection_pole_telemetry, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [
            [("recProj1", "999")],  # this project's own ProjectId has no matching telemetry
            [_telemetry_row(leadsun_project_id=482)],  # only 482 has telemetry
        ]

        pole_telemetry_loader.update_leadsun_project_details()

        update_calls = [
            c for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_telemetry_loader._UPDATE_PROJECT_LEADSUN_PROJECT_SQL
        ]
        assert update_calls == []

    def test_telemetry_with_no_matching_project_is_not_an_error(
        self, patch_get_connection_pole_telemetry, mock_cursor
    ):
        """Telemetry for a LeadsunProjectId no Project in Airtable has
        recorded yet -- simply nothing to update, not a failure."""
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [
            [],  # no Projects have a Leadsun ProjectID recorded at all
            [_telemetry_row()],
        ]

        pole_telemetry_loader.update_leadsun_project_details()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (0, 0)

    def test_final_sp_execution_update_counts_successful_projects(
        self, patch_get_connection_pole_telemetry, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [
            [("recProj1", "482"), ("recProj2", "999")],
            [_telemetry_row(leadsun_project_id=482)],  # only 482 has matching telemetry
        ]

        pole_telemetry_loader.update_leadsun_project_details()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 0)

    def test_commits_and_closes_cursor_and_connection(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [[], []]

        pole_telemetry_loader.update_leadsun_project_details()

        assert mock_conn.commit.called
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestFetchTelemetryForProjectAggregationSqlIsBounded:
    """
    Regression guard for a real production incident: an earlier version
    of _FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL filtered ONLY on
    "LeadsunProjectId IS NOT NULL", with no time bound at all --
    LeadsunProjectId doesn't change over a pole's own history, so that
    matched EVERY historical row for EVERY pole PoleTelemetry has ever
    recorded (6 months of retention, potentially many readings per pole
    per day), not just each pole's own current state. update_leadsun_
    project_details() ran immediately after load_pole_telemetry()
    finished successfully, then itself failed after an unexplained ~30
    second delay -- exactly the shape of a query trying to scan far more
    data than intended.
    """

    def test_sql_has_a_last_upload_lower_bound(self):
        sql = pole_telemetry_loader._FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL
        assert "LastUpload >= ?" in sql

    def test_sql_takes_only_the_latest_row_per_pole_within_the_window(self):
        """Not just time-bounded -- also deduplicated to ONE row per
        LocationId (via ROW_NUMBER), since a pole can still have
        multiple readings within the lookback window."""
        sql = pole_telemetry_loader._FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL
        assert "ROW_NUMBER() OVER (PARTITION BY LocationId ORDER BY LastUpload DESC)" in sql
        assert "WHERE rn = 1" in sql

    def test_lookback_constant_is_a_small_bounded_window_not_the_full_retention(self):
        """3 hours (or anything similarly small), never anywhere close
        to PoleTelemetry's own 6-month retention window -- this
        function runs every 30 minutes, immediately after
        load_pole_telemetry() has just refreshed every currently-
        reporting pole, so a wide lookback here was never actually
        needed to find "current" poles."""
        assert pole_telemetry_loader._PROJECT_DETAILS_LOOKBACK <= timedelta(hours=6)

    def test_cutoff_is_passed_as_a_single_bound_parameter(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [[], []]

        pole_telemetry_loader.update_leadsun_project_details()

        telemetry_call = next(
            c for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_telemetry_loader._FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL
        )
        assert len(telemetry_call.args) == 2  # sql + exactly one bound cutoff
        cutoff = telemetry_call.args[1]
        assert isinstance(cutoff, str)  # _to_dto_string()'d, not a raw datetime -- see that call's own comment

    def test_cutoff_reflects_the_configured_lookback(
        self, patch_get_connection_pole_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = [[], []]
        frozen_now = datetime(2026, 8, 26, 12, 0, 0, tzinfo=EASTERN)

        with freeze_time(frozen_now):
            pole_telemetry_loader.update_leadsun_project_details()

        telemetry_call = next(
            c for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_telemetry_loader._FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL
        )
        cutoff = telemetry_call.args[1]
        expected = frozen_now - pole_telemetry_loader._PROJECT_DETAILS_LOOKBACK
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))


class TestUpdateLeadsunProjectDetailsFailureHandling:
    def test_genuine_failure_propagates_and_is_recorded(
        self, patch_get_connection_pole_telemetry, mocker, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.side_effect = RuntimeError("connection lost mid-fetch")

        recovery_conn = mocker.MagicMock(name="recovery_conn")
        recovery_cursor = mocker.MagicMock(name="recovery_cursor")
        recovery_conn.cursor.return_value = recovery_cursor
        mocker.patch(
            "shared.pole_telemetry_loader.get_connection",
            side_effect=[mock_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="connection lost mid-fetch"):
            pole_telemetry_loader.update_leadsun_project_details()

        assert recovery_cursor.execute.called
        update_sql, end_time, error_message, success, errors, sp_exec_id = (
            recovery_cursor.execute.call_args.args
        )
        assert "UPDATE SP_Execution" in update_sql
        assert "connection lost mid-fetch" in error_message
        assert sp_exec_id == 99
        recovery_conn.commit.assert_called_once()
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
