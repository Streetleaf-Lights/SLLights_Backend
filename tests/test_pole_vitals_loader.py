"""Tests for shared/pole_vitals_loader.py"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared import pole_vitals_loader

EASTERN = ZoneInfo("America/New_York")


def _eastern(*args):
    """Builds an aware Eastern datetime, matching what now_eastern() returns
    in production -- _compute_cutoff()/to_dto_string() require tzinfo."""
    return datetime(*args, tzinfo=EASTERN)


# --------------------------------------------------------------------------
# _compute_cutoff -- pure function, no database needed
# --------------------------------------------------------------------------


class TestComputeCutoff:
    def test_hour_default_lookback(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=False)
        expected = now - timedelta(hours=3)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_day_default_lookback(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Day", backfill=False)
        expected = now - timedelta(days=2)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_backfill_uses_wide_window_regardless_of_period_type(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in pole_vitals_loader.PERIOD_TYPES:
            cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=True)
            expected = now - timedelta(days=400)
            assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M")), period_type

    def test_backfill_window_wider_than_default_for_every_period_type(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in pole_vitals_loader.PERIOD_TYPES:
            default_cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=False)
            backfill_cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=True)
            # Earlier cutoff = wider lookback window
            assert backfill_cutoff < default_cutoff, period_type

    def test_returns_dto_formatted_string(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=False)
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$", cutoff)


# --------------------------------------------------------------------------
# Structural checks on each period type's MERGE SQL. These can't verify
# actual aggregation correctness (that needs a real SQL Server -- not
# available in this sandbox), but they do catch structural drift/typos and
# document exactly what's expected of each statement.
# --------------------------------------------------------------------------


class TestMergeSqlStructureCommon:
    """Checks that apply identically to all four period types."""

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_has_expected_placeholder_count(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        # cutoff, sentinel-exclusion, Source, SP_ExecId -- same shape for
        # all 4 period types. The "last 6 hours" IsOnline/LightStatus
        # window for Day/Week/Month is pure SQL date arithmetic relative
        # to each bucket's own end (see TestDayWeekMonthRecentActivityWindow),
        # not an externally-bound parameter, so it adds no placeholders.
        assert sql.count("?") == 4

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_excludes_missing_last_upload_sentinel(self, period_type):
        """
        PoleTelemetry rows with a genuinely-missing LastUpload get the
        far-future sentinel timestamp (see pole_telemetry_loader.py) so
        their composite PK stays valid -- but that sentinel is always
        ">= cutoff" for any reasonable lookback window, and DATEADD-ing a
        day/month onto a bucket derived from it overflows DATE's max
        value (SQLSTATE 22007). Must be explicitly excluded.
        """
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "AND t.LastUpload <> ?" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_joins_pole_models_on_model_id(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_battery_percentage_formula(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_percentage_formula_uses_sunboard_power_with_nullif_guard(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_percentage_formula_uses_light_power_with_nullif_guard(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_filters_by_last_upload_cutoff(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "WHERE t.LastUpload >= ?" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_uses_per_pole_timezone_with_eastern_fallback(self, period_type):
        """
        Bucketing now uses each pole's own resolved timezone (from
        PoleTimeZones, via its Longitude/Latitude) instead of hardcoding
        Eastern for every pole regardless of where it actually is.
        Eastern remains only as the ISNULL() fallback for a LocationId
        PoleTimeZones doesn't have an entry for yet.
        """
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId" in sql
        assert "ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time')" in sql
        # The final SELECT's PeriodStart/PeriodEnd must reference the
        # carried-through TimeZoneName column, not a bare hardcoded
        # literal -- a bare literal here would mean Eastern is still being
        # applied to every pole regardless of PoleTimeZones.
        assert "AT TIME ZONE TimeZoneName" in sql
        assert "AT TIME ZONE 'Eastern Standard Time' AS Period" not in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_match_key_is_location_period_type_period_start(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "ON target.LocationId = source.LocationId" in sql
        assert "AND target.PeriodType = source.PeriodType" in sql
        assert "AND target.PeriodStart = source.PeriodStart" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_aggregates_with_avg_and_count(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "AVG(BatteryPercentage)" in sql
        assert "AVG(PanelPercentage)" in sql
        assert "AVG(LightPercentage)" in sql
        assert "COUNT(*)" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_is_merge_not_plain_insert(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "MERGE PoleVitals AS target" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_no_fk_references(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "REFERENCES" not in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_correct_period_type_literal(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert f"'{period_type}' AS PeriodType" in sql


class TestHourMergeSqlBucketing:
    def test_truncates_to_the_hour(self):
        sql = pole_vitals_loader._HOUR_MERGE_SQL
        assert "DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')" in sql

    def test_period_end_is_one_hour_after_start(self):
        sql = pole_vitals_loader._HOUR_MERGE_SQL
        assert "DATEADD(HOUR, 1, BucketStart) AT TIME ZONE TimeZoneName AS PeriodEnd" in sql


class TestDayMergeSqlBucketing:
    def test_truncates_to_the_date(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "CAST(LocalTime AS DATE) AS BucketStart" in sql

    def test_period_end_is_one_day_after_start(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "DATEADD(DAY, 1, BucketStart)" in sql


class TestIsOnlineAndLightStatusLogic:
    """
    Checks are prefix-agnostic (matching IsOnline/IsDaylight/LampPower1
    etc. with or without a `t.`/`tv.` prefix) since Hour's classification
    logic still lives in TelemetryWithVitals (t.-prefixed), while
    Day/Week/Month's now lives in Bucketed instead (bare column names,
    or tv.-prefixed for Week specifically) -- see TestDayWeekMonth
    RecentActivityWindow below for why that move was necessary.
    """

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_status_offline_reading_classified_as_working(self, period_type):
        """IsOnline=0 -> 'Working', NOT 'Not Working' -- deliberately
        lenient: no data to judge a malfunction from, so an offline
        reading must not itself be treated as evidence of a broken lamp."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert re.search(r"WHEN\s+\w*\.?IsOnline\s*=\s*0\s+THEN\s+'Working'", sql)

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_status_unresolved_daylight_excluded_not_guessed(self, period_type):
        """A reading whose IsDaylight is NULL (not yet computed, or an
        untrusted location) must contribute NULL, not be silently treated
        as day or night."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert re.search(r"WHEN\s+\w*\.?IsDaylight\s+IS\s+NULL\s+THEN\s+NULL", sql)

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_status_daylight_reading_classified_as_daylight(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert re.search(r"WHEN\s+\w*\.?IsDaylight\s*=\s*1\s+THEN\s+'DayLight'", sql)

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_status_lit_lamp_at_night_classified_as_working(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert re.search(
            r"WHEN\s*\(\s*\w*\.?LampPower1\s*>\s*0\s+OR\s+\w*\.?LampPower2\s*>\s*0\s*\)\s+THEN\s+'Working'",
            sql,
        )

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_status_falls_through_to_not_working(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        per_row_case = sql.split("LightStatusPerRow")[0].split("CASE")[-1]
        assert "ELSE 'Not Working'" in per_row_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_bucket_aggregation_prioritizes_not_working_first(self, period_type):
        """'at least 1 Not Working -> Not Working' must be checked BEFORE
        the 'Working' check -- priority order matters, this isn't a
        majority vote."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        match = re.search(r"CASE\s+WHEN MAX.*?END\s+AS LightStatusAgg", sql, re.DOTALL)
        assert match is not None, "couldn't find the LightStatusAgg CASE block"
        agg_case = match.group(0)
        not_working_pos = agg_case.find("THEN 'Not Working'")
        working_pos = agg_case.find("THEN 'Working'")
        assert 0 <= not_working_pos < working_pos

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_bucket_aggregation_defaults_to_daylight(self, period_type):
        """If neither 'Not Working' nor 'Working' was seen in the bucket
        (e.g. every reading was daytime, or all were unclassifiable),
        the bucket falls back to 'DayLight'."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        match = re.search(r"CASE\s+WHEN MAX.*?END\s+AS LightStatusAgg", sql, re.DOTALL)
        assert match is not None
        agg_case = match.group(0)
        assert "ELSE 'DayLight'" in agg_case
        # Must be the LAST branch (right before END), not just present
        # somewhere -- an ELSE mid-expression wouldn't be a real fallback.
        assert re.search(r"ELSE 'DayLight'\s*\n\s*END\s+AS LightStatusAgg", sql)

    def test_hour_is_online_uses_the_hour_bucket_not_a_six_hour_window(self):
        """Hour's IsOnline is just 'any reading in this hour', no extra
        time restriction -- distinct from Day/Week/Month."""
        sql = pole_vitals_loader._HOUR_MERGE_SQL
        assert "CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag" in sql
        assert "IsOnlineRecentFlag" not in sql


class TestDayRecentActivityWindow:
    """
    Confirms the fix for a real bug caught before shipping: IsOnline/
    LightStatus's "last 6 hours" window for Day must be relative to
    THAT BUCKET'S OWN end (e.g. a historical Day recomputed later always
    checks that same day's own last-6-hours-before-midnight), not
    relative to "now" (when load_pole_vitals() happens to run) -- the
    two produce identical results only for the single bucket that
    happens to be currently in progress, and silently wrong ones for
    every already-completed bucket recomputed in the same run.

    (This class used to also cover Week and Month, back when those
    period types existed -- see the README for why they were removed.)
    """

    def test_recent_window_uses_dateadd_hour_minus_6_not_a_placeholder(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "DATEADD(HOUR, -6," in sql

    def test_recent_window_does_not_use_an_external_cutoff_placeholder(self):
        """
        The bug being guarded against: IsOnlineRecentFlag/LightStatusPerRow
        must not compare against a `?`-bound parameter (which would mean
        an externally-supplied "now"-based cutoff, shared across every
        bucket in the same run) -- only the well-understood outer lookback
        cutoff and sentinel exclusion should use placeholders (confirmed
        by the total count below), not this per-row classification.
        """
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert sql.count("?") == 4  # cutoff, sentinel, source, sp_exec_id -- same as Hour

    def test_day_recent_window_is_relative_to_next_days_start(self):
        """
        Day's own end (exclusive) is DATEADD(DAY, 1, BucketStart) -- the
        recent window must be 6 hours before THAT, not before some other
        reference point.

        The extra CAST(... AS DATETIME2(3)) here is required, not
        decorative -- a real production bug: CAST(LocalTime AS DATE)
        produces a DATE value, and DATEADD(DAY, 1, <DATE>) still returns
        DATE (day-level arithmetic is valid on DATE) -- but DATEADD(HOUR,
        -6, <DATE>) then fails outright ("The datepart hour is not
        supported by date function dateadd for data type date", SQLSTATE
        42000), since DATE has no time component for HOUR-level
        arithmetic to apply to. Casting to DATETIME2(3) before the DAY
        arithmetic keeps every subsequent DATEADD operating on a
        time-capable type.
        """
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert (
            "DATEADD(HOUR, -6, DATEADD(DAY, 1, CAST(CAST(LocalTime AS DATE) AS DATETIME2(3))))"
            in sql
        )

    def test_recent_window_never_applies_hour_dateadd_directly_to_a_date(self):
        """
        Regression guard, phrased structurally rather than as an exact
        string match: Day's recent-activity window expression should
        never apply DATEADD(HOUR, ...) directly around a bare
        DATE-producing expression (CAST(x AS DATE)) without an
        intervening CAST to a time-capable type. Catches the same class
        of bug even if the exact expression text changes later.
        """
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "DATEADD(HOUR, -6, DATEADD(" in sql
        assert "AS DATETIME2(3))))" in sql

    def test_raw_fields_needed_for_classification_flow_into_bucketed(self):
        """
        The classification logic (IsOnline, IsDaylight, LampPower1/2)
        moved from TelemetryWithVitals into Bucketed for Day,
        specifically because it needs the bucket's own BucketStart --
        which isn't known yet at the TelemetryWithVitals stage. Confirms
        the raw columns actually get selected through so Bucketed can
        reference them.
        """
        sql = pole_vitals_loader._DAY_MERGE_SQL
        telemetry_cte = sql.split("Bucketed AS (")[0]
        assert "t.IsOnline" in telemetry_cte
        assert "t.IsDaylight" in telemetry_cte
        assert "t.LampPower1" in telemetry_cte
        assert "t.LampPower2" in telemetry_cte


class TestPerPoleTimeZonePropagation:
    """
    Dedicated coverage for the per-pole (not hardcoded-Eastern) timezone
    feature -- specifically that TimeZoneName survives the GROUP BY
    intact for every period type. Week's Bucketed CTE originally selected
    from TelemetryWithVitals via a table alias (tv.*) and it was easy to
    add TimeZoneName to Aggregated's SELECT/GROUP BY without actually
    threading tv.TimeZoneName through Bucketed first -- which would have
    referenced a column that doesn't exist. These tests exist specifically
    to catch that class of mistake if it's ever reintroduced.
    """

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_bucketed_cte_selects_time_zone_name(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        bucketed_cte = sql.split("Bucketed AS (")[1].split("Aggregated AS (")[0]
        assert "TimeZoneName" in bucketed_cte

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_aggregated_cte_groups_by_time_zone_name(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "GROUP BY LocationId, TimeZoneName" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_time_zone_name_defined_before_bucketed_cte_uses_it(self, period_type):
        """
        Sanity check on CTE ordering: TimeZoneName must be defined in
        TelemetryWithVitals (via the ISNULL fallback expression) before
        Bucketed can reference it.
        """
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        telemetry_cte_end = sql.index("Bucketed AS (")
        definition = sql[:telemetry_cte_end]
        assert "ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName" in definition


# --------------------------------------------------------------------------
# load_pole_vitals() -- full flow
# --------------------------------------------------------------------------


class TestLoadPoleVitalsSuccessFlow:
    def test_full_success_flow_executes_both_period_types_in_order(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (77,)
        mock_cursor.rowcount = 5

        pole_vitals_loader.load_pole_vitals()

        calls = mock_cursor.execute.call_args_list
        # insert SP_Execution, 2x MERGE (Hour, Day), final update
        assert len(calls) == 4

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleVitals", "Dev", "Leadsun")

        merge_calls = calls[1:3]
        for period_type, call in zip(pole_vitals_loader.PERIOD_TYPES, merge_calls):
            merge_sql = call.args[0]
            assert len(call.args) == 5  # merge_sql + cutoff, sentinel, source, sp_exec_id
            _, sentinel, source_name, sp_exec_id = call.args[1:]
            assert f"'{period_type}' AS PeriodType" in merge_sql
            assert sentinel == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
            assert source_name == "Leadsun"
            assert sp_exec_id == 77

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[3].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (10, 0, 2, 77)  # 5 rows x 2 period types

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_default_run_uses_small_lookback_not_backfill_window(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=False)

        merge_calls = mock_cursor.execute.call_args_list[1:3]
        for period_type, call in zip(pole_vitals_loader.PERIOD_TYPES, merge_calls):
            cutoff = call.args[-3]  # cutoff is always 3rd-from-last, regardless of shape
            # None of the default-run cutoffs should be as far back as the
            # ~400-day backfill window would produce.
            assert cutoff > "2025-06-01", period_type  # comfortably within ~13 months, not 400 days

    def test_backfill_true_uses_wide_lookback_for_every_period_type(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=True)

        merge_calls = mock_cursor.execute.call_args_list[1:3]
        cutoffs = [call.args[-3] for call in merge_calls]  # cutoff, not recent-activity cutoff
        # Both period types should use the SAME wide backfill cutoff.
        assert len(set(cutoffs)) == 1

    def test_zero_rowcount_does_not_go_negative_or_none(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        pole_vitals_loader.load_pole_vitals()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert success == 0
        assert errors == 0


class TestIsBenignNullAggregateWarning:
    def test_recognizes_sqlstate_01003(self):
        exc = Exception(
            "01003",
            "[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Warning: Null value is eliminated by an aggregate or other SET operation. "
            "(8153) (SQLExecDirectW)",
        )
        assert pole_vitals_loader._is_benign_null_aggregate_warning(exc) is True

    def test_does_not_recognize_a_genuine_error(self):
        exc = Exception(
            "22007",
            "[22007] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Adding a value to a 'date' column caused an overflow. (517) (SQLExecDirectW)",
        )
        assert pole_vitals_loader._is_benign_null_aggregate_warning(exc) is False

    def test_does_not_recognize_a_plain_exception_with_no_sqlstate(self):
        assert pole_vitals_loader._is_benign_null_aggregate_warning(RuntimeError("boom")) is False

    def test_does_not_crash_on_an_exception_with_no_args(self):
        assert pole_vitals_loader._is_benign_null_aggregate_warning(Exception()) is False


class TestLoadPoleVitalsBenignWarningHandling:
    def _make_01003_exception(self):
        return Exception(
            "01003",
            "[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Warning: Null value is eliminated by an aggregate or other SET operation. "
            "(8153) (SQLExecDirectW)",
        )

    def test_01003_warning_is_not_counted_as_an_error(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 7
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            self._make_01003_exception(),  # Hour MERGE -- benign
            None,  # Day
            None,  # final update
        ]

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 0  # the 01003 "failure" must not count as an error
        assert success == 14  # 2 period types x 7 rows each, including Hour

    def test_01003_warning_logs_as_info_not_error(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 3
        mock_cursor.execute.side_effect = [
            None,
            self._make_01003_exception(),
            None,
            None,
        ]

        with caplog.at_level("INFO"):
            pole_vitals_loader.load_pole_vitals()

        info_messages = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("Hour period recomputed" in m and "expected, not an error" in m for m in info_messages)
        assert not any("failed to recompute Hour" in m for m in error_messages)

    def test_genuine_22007_overflow_still_counts_as_a_real_error(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        """Sanity check that the benign-warning carve-out doesn't
        accidentally swallow a real error too -- e.g. the date-overflow
        bug this exact carve-out was added alongside a fix for."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        overflow_exc = Exception(
            "22007",
            "[22007] ... Adding a value to a 'date' column caused an overflow. (517)",
        )
        mock_cursor.execute.side_effect = [None, None, overflow_exc, None]

        pole_vitals_loader.load_pole_vitals()  # must not raise (per-period isolation)

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1


class TestLoadPoleVitalsPerPeriodTypeCommits:
    """
    Confirms load_pole_vitals() commits after EACH period type
    individually, not once at the end for both -- built specifically so
    a slow/failing later period type (real production scenario: severe
    database CPU contention leaving Week's MERGE stuck for 20+ minutes,
    back when Week still existed -- see the README for that history)
    can no longer roll back an earlier period type's already-successful,
    already-computed results by sharing one long-lived transaction.

    Uses call_order side_effect tracking (appending to a shared list from
    both cursor.execute() and conn.commit()/rollback()), not
    mock_conn.mock_calls -- this project's own mock_cursor/mock_conn
    fixtures create explicitly-named MagicMocks (name="cursor"/
    name="connection"), and unittest.mock does not reliably propagate
    calls on an explicitly-named child mock into its parent's mock_calls
    the way it does for an auto-generated (unnamed) child -- confirmed
    directly before writing these, rather than assumed.
    """

    def _track_calls(self, mock_conn, mock_cursor, exceptions_by_execute_index=None):
        """
        Wires side_effects on both mock_cursor.execute and
        mock_conn.commit/rollback so calls to any of them append to one
        shared, ordered list -- returns that list. exceptions_by_execute_index
        maps a 0-based execute() call index to an exception to raise on
        that specific call (index 0 is always the SP_Execution insert).
        """
        call_order = []
        exceptions_by_execute_index = exceptions_by_execute_index or {}
        counter = {"n": 0}

        def _execute_side_effect(*args, **kwargs):
            idx = counter["n"]
            counter["n"] += 1
            call_order.append("execute")
            if idx in exceptions_by_execute_index:
                raise exceptions_by_execute_index[idx]

        mock_cursor.execute.side_effect = _execute_side_effect
        mock_conn.commit.side_effect = lambda: call_order.append("commit")
        mock_conn.rollback.side_effect = lambda: call_order.append("rollback")
        return call_order

    def test_commits_after_each_successful_period_type_individually(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        call_order = self._track_calls(mock_conn, mock_cursor)

        pole_vitals_loader.load_pole_vitals()

        # insert+commit, then (execute, commit) once per period type,
        # then final-update+commit -- NOT both executes followed by a
        # single commit at the end.
        assert call_order == [
            "execute", "commit",  # SP_Execution insert
            "execute", "commit",  # Hour
            "execute", "commit",  # Day
            "execute", "commit",  # SP_Execution final update
        ]

    def test_benign_null_aggregate_warning_still_commits_that_period_type(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        """The underlying MERGE actually succeeded despite the SQLSTATE
        01003 warning -- see _is_benign_null_aggregate_warning -- so this
        must still commit, not just avoid counting it as an error."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 3
        benign_exc = Exception("01003", "Warning: Null value is eliminated by an aggregate...")
        # index 0 = SP_Execution insert, index 1 = Hour (benign warning)
        call_order = self._track_calls(mock_conn, mock_cursor, {1: benign_exc})

        pole_vitals_loader.load_pole_vitals()

        assert "rollback" not in call_order
        assert call_order.count("commit") == 4  # insert + 2 period types + final update

    def test_genuine_failure_rolls_back_and_does_not_block_later_period_types(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 0 = insert, index 1 = Hour (genuine failure)
        call_order = self._track_calls(mock_conn, mock_cursor, {1: RuntimeError("Hour failed")})

        pole_vitals_loader.load_pole_vitals()  # must not raise

        assert call_order == [
            "execute", "commit",    # SP_Execution insert
            "execute", "rollback",  # Hour -- fails, rolled back
            "execute", "commit",    # Day -- still attempted, succeeds
            "execute", "commit",    # SP_Execution final update
        ]

    def test_an_earlier_period_types_commit_already_happened_before_a_later_failure(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        """
        The actual property this whole change exists for: by the time a
        LATER period type fails, an EARLIER one's commit() call has
        already happened -- so nothing that occurs afterward (another
        period type's rollback, or even the whole process being killed)
        can undo it.
        """
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 0=insert, 1=Hour(ok), 2=Day(FAILS)
        call_order = self._track_calls(mock_conn, mock_cursor, {2: RuntimeError("Day failed")})

        pole_vitals_loader.load_pole_vitals()

        assert call_order == [
            "execute", "commit",    # SP_Execution insert
            "execute", "commit",    # Hour -- already committed here
            "execute", "rollback",  # Day -- fails AFTER Hour's commit above
            "execute", "commit",    # SP_Execution final update
        ]

    def test_final_sp_execution_update_still_reflects_totals_across_all_period_types(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        """The move to per-period commits shouldn't change what the
        SP_Execution row's final tally reports -- still one row
        summarizing the whole run."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        self._track_calls(mock_conn, mock_cursor, {1: RuntimeError("Hour failed")})

        pole_vitals_loader.load_pole_vitals()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (5, 1)  # 1 successful period type x 5 rows, 1 failed


class TestLoadPoleVitalsPartialFailure:
    def test_one_period_type_failing_does_not_block_the_others(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        # insert SP_Execution succeeds, then Hour fails, Day succeeds,
        # then final update succeeds.
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            RuntimeError("Hour failed"),  # Hour MERGE
            None,  # Day MERGE
            None,  # final update
        ]
        mock_cursor.rowcount = 3

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1
        assert success == 3  # 1 successful period type x 3 rows

    def test_logs_error_for_failed_period_type(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.execute.side_effect = [
            None,
            RuntimeError("boom"),
            None,
            None,
        ]
        mock_cursor.rowcount = 0

        with caplog.at_level("ERROR"):
            pole_vitals_loader.load_pole_vitals()

        assert any(
            "failed to recompute Hour period" in rec.message for rec in caplog.records
        )


class TestLoadPoleVitalsTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db connection lost")

        with pytest.raises(RuntimeError, match="db connection lost"):
            pole_vitals_loader.load_pole_vitals()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
