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

    def test_last_48_hours_default_lookback_is_exactly_48_hours(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Last48Hours", backfill=False)
        expected = now - timedelta(hours=48)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_backfill_uses_wide_window_for_hour_and_day(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in ("Hour", "Day"):
            cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=True)
            expected = now - timedelta(days=400)
            assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M")), period_type

    def test_backfill_is_ignored_for_last_48_hours(self):
        """Last48Hours has no "backfill history" concept -- it's always a
        rolling 48-hour window regardless of when this loader last ran,
        so backfill=True must produce the exact same cutoff as
        backfill=False for this period type specifically."""
        now = _eastern(2026, 7, 15, 14, 30, 0)
        default_cutoff = pole_vitals_loader._compute_cutoff(now, "Last48Hours", backfill=False)
        backfill_cutoff = pole_vitals_loader._compute_cutoff(now, "Last48Hours", backfill=True)
        assert default_cutoff == backfill_cutoff

    def test_backfill_window_wider_than_default_for_hour_and_day(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in ("Hour", "Day"):
            default_cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=False)
            backfill_cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=True)
            # Earlier cutoff = wider lookback window
            assert backfill_cutoff < default_cutoff, period_type

    def test_returns_dto_formatted_string(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in pole_vitals_loader.PERIOD_TYPES:
            cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=False)
            assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$", cutoff)


# --------------------------------------------------------------------------
# Structural checks on each period type's MERGE SQL. These can't verify
# actual aggregation correctness (that needs a real SQL Server -- not
# available in this sandbox), but they do catch structural drift/typos and
# document exactly what's expected of each statement.
# --------------------------------------------------------------------------


class TestMergeSqlStructureCommon:
    """Checks that apply identically to all three period types."""

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_has_expected_placeholder_count(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        # cutoff, sentinel-exclusion, Source, SP_ExecId -- same shape for
        # all 3 period types.
        assert sql.count("?") == 4

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_excludes_missing_last_upload_sentinel(self, period_type):
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

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_no_light_status_or_is_daylight_anywhere(self, period_type):
        """The whole point of this redesign -- Daylight-based
        classification is gone entirely, replaced by fault flags."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "LightStatus" not in sql
        assert "IsDaylight" not in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_ansi_warnings_disabled_around_the_merge_then_restored(self, period_type):
        """Real production bug: SQL Server's "NULL value is eliminated by
        an aggregate" notice (SQLSTATE 01003) -- which AVG(PanelPercentage)/
        AVG(LightPercentage) trigger constantly here, since NULLIF(...,0)
        deliberately produces NULL for plenty of readings -- was observed
        to prevent the MERGE's write from actually landing, contrary to
        this loader's own prior assumption (baked into
        _is_benign_null_aggregate_warning) that the warning was purely
        informational and didn't block the underlying write. Rather than
        continuing to rely on "catch the exception and assume success",
        ANSI_WARNINGS is turned off for the duration of the MERGE (a
        documented SQL Server setting that specifically controls whether
        this notice is raised at all) and restored to ON immediately
        after, so the warning -- and the exception pyodbc would otherwise
        raise for it -- never happens in the first place."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        off_index = sql.find("SET ANSI_WARNINGS OFF;")
        on_index = sql.find("SET ANSI_WARNINGS ON;")
        assert off_index != -1, "ANSI_WARNINGS must be turned off before the MERGE"
        assert on_index != -1, "ANSI_WARNINGS must be restored to on after the MERGE"
        assert off_index < sql.find("MERGE PoleVitals") < on_index, (
            "OFF must come before, and ON after, the MERGE statement itself"
        )

    @pytest.mark.parametrize("period_type", ("Hour", "Day"))
    def test_hour_and_day_match_key_includes_period_start(self, period_type):
        """Hour/Day are genuine historical buckets -- PeriodStart is part
        of their identity, unlike Last48Hours (see
        TestLast48HoursMergeSqlStructure for why that one differs)."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "ON target.LocationId = source.LocationId" in sql
        assert "AND target.PeriodType = source.PeriodType" in sql
        assert "AND target.PeriodStart = source.PeriodStart" in sql


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


class TestLast48HoursMergeSqlStructure:
    """
    Last48Hours is a genuinely different kind of "period" from Hour/Day:
    a single, continuously-updated rolling window per pole, not one of a
    sequence of discrete historical buckets. These tests cover exactly
    the properties that make it different.
    """

    def test_match_key_is_location_and_period_type_only_not_period_start(self):
        """The core design choice: PeriodStart shifts forward every run
        (always "now - 48h"), so matching on it would mean this could
        only ever INSERT, never UPDATE the same row -- violating the
        "only 1 row per pole" guarantee."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "ON target.LocationId = source.LocationId" in sql
        assert "AND target.PeriodType = source.PeriodType" in sql
        assert "PeriodStart = source.PeriodStart" not in sql

    def test_period_start_and_end_are_updated_on_match(self):
        """Even though PeriodStart isn't part of the match key, it (and
        PeriodEnd) must still be refreshed on every run -- otherwise an
        existing row's window would silently go stale."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        update_set = sql.split("WHEN MATCHED THEN UPDATE SET")[1].split("WHEN NOT MATCHED")[0]
        assert "PeriodStart" in update_set
        assert "PeriodEnd" in update_set

    def test_window_is_48_hours_via_sysdatetimeoffset_not_a_placeholder(self):
        """The window bounds are computed directly in SQL (relative to
        whenever the MERGE actually runs), not passed in as a bound
        parameter -- there's no "PeriodStart placeholder" the way Hour/
        Day don't need one either."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "DATEADD(HOUR, -48, SYSDATETIMEOFFSET())" in sql
        assert "SYSDATETIMEOFFSET() AS PeriodEnd" in sql

    def test_no_pole_timezones_join(self):
        """No local-time bucketing at all -- this is a pure duration
        window, not calendar-aligned, so there's nothing for a timezone
        conversion to add."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "PoleTimeZones" not in sql

    def test_no_bucketed_cte_groups_directly_from_telemetry(self):
        """Unlike Hour/Day, there's only ever one 'bucket' per
        LocationId (the whole window), so there's no separate Bucketed
        CTE -- TelemetryWithVitals feeds Aggregated directly."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "Bucketed AS (" not in sql

    def test_groups_by_location_id_alone(self):
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "GROUP BY LocationId" in sql


# --------------------------------------------------------------------------
# Fault-flag design -- replaces the earlier Daylight-based LightStatus
# classification entirely.
# --------------------------------------------------------------------------


class TestFaultFlagFormulas:
    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_led_fault_formula(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1 ELSE 0 END AS IsLedFaultFlag" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_battery_fault_formula(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert (
            "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag"
            in sql
        )

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_formula(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert (
            "WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1 ELSE 0 END AS IsPanelFaultFlag" in sql
        )

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_open_issue_fault_read_directly_not_recomputed(self, period_type):
        """IsOpenIssueFault is already computed and stored per reading by
        pole_telemetry_loader.py -- this must read t.IsOpenIssueFault
        directly, not recompute it via a join against
        Poles/PoleOpenIssues here."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "t.IsOpenIssueFault" in sql
        assert "PoleOpenIssues" not in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_bucket_level_fault_flags_are_any_in_window(self, period_type):
        """A single confirmed fault anywhere in the window makes the
        whole bucket faulted -- MAX(), not AVG() or a majority vote."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "MAX(IsLedFaultFlag)" in sql
        assert "MAX(IsBatteryFaultFlag)" in sql
        assert "MAX(IsPanelFaultFlag)" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_is_pole_fault_is_or_of_all_four(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        is_pole_fault_expr = sql.split("AS IsPoleFault")[0].split("CAST(\n            CASE")[-1]
        assert "IsLedFaultAgg = 1" in is_pole_fault_expr
        assert "IsBatteryFaultAgg = 1" in is_pole_fault_expr
        assert "IsPanelFaultAgg = 1" in is_pole_fault_expr
        assert "IsOpenIssueFaultAgg, 0) = 1" in is_pole_fault_expr

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_fault_columns_cast_to_bit(self, period_type):
        """Same reasoning as IsOnline elsewhere in this project: without
        an explicit CAST, pyodbc would hand back a plain int (0/1), so
        the API layer would serialize these as 1/0 instead of true/false."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "CAST(IsLedFaultAgg AS BIT)" in sql
        assert "CAST(IsBatteryFaultAgg AS BIT)" in sql
        assert "CAST(IsPanelFaultAgg AS BIT)" in sql
        assert "AS BIT) AS IsOpenIssueFault" in sql
        assert "AS BIT) AS IsPoleFault" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_null_open_issue_fault_treated_as_not_faulted(self, period_type):
        """An existing row from before IsOpenIssueFault existed (or a
        reading that somehow never got it set) must not silently become
        a fault -- ISNULL(..., 0) before the BIT cast and the OR check."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "ISNULL(IsOpenIssueFaultAgg, 0)" in sql


class TestTakeLastTelemetryForOpenIssueFault:
    """
    IsOpenIssueFault is NOT an any-in-window aggregate like the other
    three fault flags -- it takes the single most recent reading's own
    value, identified via ROW_NUMBER() ... ORDER BY LastUpload DESC, then
    extracted via MAX(CASE WHEN rn = 1 THEN ...) -- a standard "pick a
    column from the row with max of another column, within a group"
    pattern.
    """

    def test_hour_partitions_row_number_by_location_and_bucket(self):
        sql = pole_vitals_loader._HOUR_MERGE_SQL
        assert "ROW_NUMBER() OVER (" in sql
        assert "ORDER BY LastUpload DESC" in sql
        assert "AS LatestInBucket" in sql

    def test_day_partitions_row_number_by_location_and_bucket(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "PARTITION BY LocationId, CAST(LocalTime AS DATE)" in sql
        assert "AS LatestInBucket" in sql

    def test_last_48_hours_partitions_row_number_by_location_only(self):
        """No bucket dimension to partition by -- the whole window IS
        the one bucket per pole."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "PARTITION BY t.LocationId ORDER BY t.LastUpload DESC" in sql
        assert "AS LatestOverall" in sql

    @pytest.mark.parametrize("period_type", ("Hour", "Day"))
    def test_hour_and_day_extract_via_max_case_when_rn_equals_1(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "MAX(CASE WHEN LatestInBucket = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END)" in sql

    def test_last_48_hours_extracts_via_max_case_when_rn_equals_1(self):
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "MAX(CASE WHEN LatestOverall = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END)" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_is_open_issue_fault_is_cast_before_max_not_passed_through_raw(self, period_type):
        """Regression test for a real production bug: IsOpenIssueFault
        is a BIT column in PoleTelemetry, and SQL Server's MAX() cannot
        operate on a BIT-typed expression at all -- not even one nested
        inside a CASE WHEN...THEN...END, since the CASE expression's own
        result type is still BIT when its branches are (SQLSTATE 42000,
        error 8117: "Operand data type bit is invalid for max operator").
        Every OTHER fault flag avoids this because it's computed via
        CASE WHEN <condition> THEN 1 ELSE 0 END (an INT literal, not a
        raw BIT column) -- this is the one place a raw BIT column value
        flows into an aggregate directly, so it needs an explicit CAST
        to an integer type first."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "THEN CAST(IsOpenIssueFault AS TINYINT) END" in sql
        assert "THEN IsOpenIssueFault END" not in sql


class TestDayRecentActivityWindow:
    """
    Confirms Day's IsOnline "last 6 hours" window is relative to THAT
    BUCKET'S OWN end (e.g. a historical Day recomputed later always
    checks that same day's own last-6-hours-before-midnight), not
    relative to "now" (when load_pole_vitals() happens to run).
    Deliberately NOT shared with the fault flags -- only IsOnline uses
    this narrower window, an explicit requirement.
    """

    def test_recent_window_uses_dateadd_hour_minus_6_not_a_placeholder(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "DATEADD(HOUR, -6," in sql

    def test_recent_window_does_not_use_an_external_cutoff_placeholder(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert sql.count("?") == 4  # cutoff, sentinel, source, sp_exec_id -- same as Hour

    def test_day_recent_window_is_relative_to_next_days_start(self):
        """
        The extra CAST(... AS DATETIME2(3)) here is required, not
        decorative -- CAST(LocalTime AS DATE) produces a DATE value, and
        DATEADD(HOUR, -6, <DATE>) fails outright ("The datepart hour is
        not supported by date function dateadd for data type date",
        SQLSTATE 42000), since DATE has no time component. Casting to
        DATETIME2(3) before the arithmetic keeps it operating on a
        time-capable type.
        """
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert (
            "DATEADD(HOUR, -6, DATEADD(DAY, 1, CAST(CAST(LocalTime AS DATE) AS DATETIME2(3))))"
            in sql
        )

    def test_recent_window_never_applies_hour_dateadd_directly_to_a_date(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "DATEADD(HOUR, -6, DATEADD(" in sql
        assert "AS DATETIME2(3))))" in sql

    def test_is_online_flows_into_bucketed_for_the_recent_window_check(self):
        """IsOnlineRecentFlag is computed in Bucketed (needs BucketStart,
        via CAST(LocalTime AS DATE)), so the raw IsOnline/LocalTime it
        depends on must actually flow through from TelemetryWithVitals.
        Unlike the fault flags (computed directly in TelemetryWithVitals,
        since they need no bucket context at all), this is the one piece
        of Day-specific classification logic that does."""
        sql = pole_vitals_loader._DAY_MERGE_SQL
        telemetry_cte = sql.split("Bucketed AS (")[0]
        assert "t.IsOnline" in telemetry_cte
        bucketed_cte = sql.split("Bucketed AS (")[1].split("Aggregated AS (")[0]
        assert "LocalTime >=" in bucketed_cte


class TestPerPoleTimeZonePropagation:
    """
    Dedicated coverage for the per-pole (not hardcoded-Eastern) timezone
    feature -- specifically that TimeZoneName survives the GROUP BY
    intact. Hour/Day only -- Last48Hours has no timezone handling at all
    (see TestLast48HoursMergeSqlStructure.test_no_pole_timezones_join).
    """

    @pytest.mark.parametrize("period_type", ("Hour", "Day"))
    def test_bucketed_cte_selects_time_zone_name(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        bucketed_cte = sql.split("Bucketed AS (")[1].split("Aggregated AS (")[0]
        assert "TimeZoneName" in bucketed_cte

    @pytest.mark.parametrize("period_type", ("Hour", "Day"))
    def test_aggregated_cte_groups_by_time_zone_name(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "GROUP BY LocationId, TimeZoneName" in sql

    @pytest.mark.parametrize("period_type", ("Hour", "Day"))
    def test_time_zone_name_defined_before_bucketed_cte_uses_it(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        telemetry_cte_end = sql.index("Bucketed AS (")
        definition = sql[:telemetry_cte_end]
        assert "ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName" in definition

    @pytest.mark.parametrize("period_type", ("Hour", "Day"))
    def test_uses_per_pole_timezone_with_eastern_fallback(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId" in sql
        assert "ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time')" in sql
        assert "AT TIME ZONE TimeZoneName" in sql


# --------------------------------------------------------------------------
# Retention pruning -- genuinely new: this table had no pruning at all
# before this change.
# --------------------------------------------------------------------------


class TestRetentionPruneSql:
    def test_ranks_by_period_start_descending_per_location(self):
        sql = pole_vitals_loader._RETENTION_PRUNE_SQL
        assert "ROW_NUMBER() OVER (PARTITION BY LocationId ORDER BY PeriodStart DESC)" in sql

    def test_deletes_rows_beyond_the_limit(self):
        sql = pole_vitals_loader._RETENTION_PRUNE_SQL
        assert "DELETE pv" in sql
        assert "WHERE pv.PeriodType = ? AND r.rn > ?" in sql

    def test_retention_limits_only_defined_for_hour_and_day(self):
        """Last48Hours needs no pruning -- it's structurally always
        exactly one row per pole (matched on LocationId+PeriodType alone,
        not PeriodStart -- see _LAST_48_HOURS_MERGE_SQL)."""
        assert pole_vitals_loader._RETENTION_LIMITS == {"Hour": 168, "Day": 7}
        assert "Last48Hours" not in pole_vitals_loader._RETENTION_LIMITS


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


# --------------------------------------------------------------------------
# load_pole_vitals() -- full flow.
#
# Execute-call sequence on full success (7 calls total):
#   0: INSERT SP_Execution
#   1: Hour MERGE      2: Hour retention prune
#   3: Day MERGE       4: Day retention prune
#   5: Last48Hours MERGE (no prune -- see _RETENTION_LIMITS)
#   6: UPDATE SP_Execution (final)
# --------------------------------------------------------------------------


class TestLoadPoleVitalsSuccessFlow:
    def test_full_success_flow_executes_all_three_period_types_in_order(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (77,)
        mock_cursor.rowcount = 5

        pole_vitals_loader.load_pole_vitals()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 7

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleVitals", "Dev", "Leadsun")

        # MERGE calls are at indices 1 (Hour), 3 (Day), 5 (Last48Hours)
        merge_indices = {"Hour": 1, "Day": 3, "Last48Hours": 5}
        for period_type, idx in merge_indices.items():
            call = calls[idx]
            merge_sql = call.args[0]
            assert len(call.args) == 5  # merge_sql + cutoff, sentinel, source, sp_exec_id
            _, sentinel, source_name, sp_exec_id = call.args[1:]
            assert f"'{period_type}' AS PeriodType" in merge_sql
            assert sentinel == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
            assert source_name == "Leadsun"
            assert sp_exec_id == 77

        # Retention prune calls are at indices 2 (Hour), 4 (Day)
        hour_prune = calls[2].args
        assert hour_prune == (pole_vitals_loader._RETENTION_PRUNE_SQL, "Hour", "Hour", 168)
        day_prune = calls[4].args
        assert day_prune == (pole_vitals_loader._RETENTION_PRUNE_SQL, "Day", "Day", 7)

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[6].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (15, 0, 3, 77)  # 5 rows x 3 period types

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_default_run_uses_small_lookback_not_backfill_window(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=False)

        for idx in (1, 3, 5):  # Hour, Day, Last48Hours MERGE calls
            cutoff = mock_cursor.execute.call_args_list[idx].args[-4]
            assert cutoff > "2025-06-01", idx  # comfortably within ~13 months, not 400 days

    def test_backfill_true_widens_hour_and_day_but_not_last_48_hours(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=True)

        hour_cutoff = mock_cursor.execute.call_args_list[1].args[-4]
        day_cutoff = mock_cursor.execute.call_args_list[3].args[-4]
        last48_cutoff = mock_cursor.execute.call_args_list[5].args[-4]
        assert hour_cutoff == day_cutoff  # both use the same wide backfill window
        assert last48_cutoff > hour_cutoff  # NOT widened -- always just 48 hours back

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
            None,  # Hour prune (still runs -- the MERGE "succeeded")
            None,  # Day MERGE
            None,  # Day prune
            None,  # Last48Hours MERGE
            None,  # final update
        ]

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 0  # the 01003 "failure" must not count as an error
        assert success == 21  # 3 period types x 7 rows each, including Hour

    def test_01003_warning_logs_as_info_not_error(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 3
        mock_cursor.execute.side_effect = [
            None,
            self._make_01003_exception(),
            None, None, None, None, None,
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
        accidentally swallow a real error too."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        overflow_exc = Exception(
            "22007",
            "[22007] ... Adding a value to a 'date' column caused an overflow. (517)",
        )
        # index 0=insert, 1=Hour(ok), 2=Hour prune(ok), 3=Day(FAILS)
        mock_cursor.execute.side_effect = [None, None, None, overflow_exc, None, None, None]

        pole_vitals_loader.load_pole_vitals()  # must not raise (per-period isolation)

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1


class TestLoadPoleVitalsPerPeriodTypeCommits:
    """
    Confirms load_pole_vitals() commits after EACH period type
    individually (MERGE + retention prune together, where applicable),
    not once at the end for all three.

    Uses call_order side_effect tracking (appending to a shared list from
    both cursor.execute() and conn.commit()/rollback()), not
    mock_conn.mock_calls -- this project's own mock_cursor/mock_conn
    fixtures create explicitly-named MagicMocks, and unittest.mock does
    not reliably propagate calls on an explicitly-named child mock into
    its parent's mock_calls the way it does for an auto-generated one.
    """

    def _track_calls(self, mock_conn, mock_cursor, exceptions_by_execute_index=None):
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

        # insert+commit, then (MERGE, prune, commit) for Hour, then Day,
        # then (MERGE, commit) for Last48Hours (no prune), then final
        # update+commit.
        assert call_order == [
            "execute", "commit",             # SP_Execution insert
            "execute", "execute", "commit",  # Hour MERGE + prune
            "execute", "execute", "commit",  # Day MERGE + prune
            "execute", "commit",             # Last48Hours MERGE (no prune)
            "execute", "commit",             # SP_Execution final update
        ]

    def test_benign_null_aggregate_warning_still_commits_that_period_type(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 3
        benign_exc = Exception("01003", "Warning: Null value is eliminated by an aggregate...")
        # index 0 = SP_Execution insert, index 1 = Hour MERGE (benign warning)
        call_order = self._track_calls(mock_conn, mock_cursor, {1: benign_exc})

        pole_vitals_loader.load_pole_vitals()

        assert "rollback" not in call_order
        assert call_order.count("commit") == 5  # insert + 3 period types + final update

    def test_genuine_failure_rolls_back_and_does_not_block_later_period_types(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 0 = insert, index 1 = Hour MERGE (genuine failure)
        call_order = self._track_calls(mock_conn, mock_cursor, {1: RuntimeError("Hour failed")})

        pole_vitals_loader.load_pole_vitals()  # must not raise

        assert call_order == [
            "execute", "commit",             # SP_Execution insert
            "execute", "rollback",           # Hour MERGE -- fails, rolled back (prune never runs)
            "execute", "execute", "commit",  # Day -- still attempted, succeeds
            "execute", "commit",             # Last48Hours -- still attempted, succeeds
            "execute", "commit",             # SP_Execution final update
        ]

    def test_an_earlier_period_types_commit_already_happened_before_a_later_failure(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 0=insert, 1=Hour MERGE(ok), 2=Hour prune(ok), 3=Day MERGE(FAILS)
        call_order = self._track_calls(mock_conn, mock_cursor, {3: RuntimeError("Day failed")})

        pole_vitals_loader.load_pole_vitals()

        assert call_order == [
            "execute", "commit",             # SP_Execution insert
            "execute", "execute", "commit",  # Hour -- already committed here
            "execute", "rollback",           # Day MERGE -- fails AFTER Hour's commit above
            "execute", "commit",             # Last48Hours -- still attempted, succeeds
            "execute", "commit",             # SP_Execution final update
        ]

    def test_final_sp_execution_update_still_reflects_totals_across_all_period_types(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        self._track_calls(mock_conn, mock_cursor, {1: RuntimeError("Hour failed")})

        pole_vitals_loader.load_pole_vitals()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (10, 1)  # 2 successful period types x 5 rows, 1 failed


class TestLoadPoleVitalsPartialFailure:
    def test_one_period_type_failing_does_not_block_the_others(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            RuntimeError("Hour failed"),  # Hour MERGE
            None,  # Day MERGE
            None,  # Day prune
            None,  # Last48Hours MERGE
            None,  # final update
        ]
        mock_cursor.rowcount = 3

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1
        assert success == 6  # 2 successful period types (Day, Last48Hours) x 3 rows

    def test_logs_error_for_failed_period_type(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.execute.side_effect = [
            None,
            RuntimeError("boom"),
            None, None, None, None,
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
