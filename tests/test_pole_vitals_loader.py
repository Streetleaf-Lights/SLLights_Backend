"""Tests for shared/pole_vitals_loader.py"""

import re
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pyodbc
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

    def test_last_48_hours_default_lookback_is_exactly_48_hours(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Last48Hours", backfill=False)
        expected = now - timedelta(hours=48)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_backfill_uses_wide_window_for_hour(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=True)
        expected = now - timedelta(days=400)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_backfill_is_ignored_for_last_48_hours(self):
        """Last48Hours has no "backfill history" concept -- it's always a
        rolling 48-hour window regardless of when this loader last ran,
        so backfill=True must produce the exact same cutoff as
        backfill=False for this period type specifically."""
        now = _eastern(2026, 7, 15, 14, 30, 0)
        default_cutoff = pole_vitals_loader._compute_cutoff(now, "Last48Hours", backfill=False)
        backfill_cutoff = pole_vitals_loader._compute_cutoff(now, "Last48Hours", backfill=True)
        assert default_cutoff == backfill_cutoff

    def test_backfill_window_wider_than_default_for_hour(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        default_cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=False)
        backfill_cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=True)
        # Earlier cutoff = wider lookback window
        assert backfill_cutoff < default_cutoff

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
        assert (
            "(t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0"
            in sql
        )

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_percentage_formula_uses_light_power_with_nullif_guard(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_sunboard_power_defaults_to_80_when_model_unmatched(self, period_type):
        """A ModelId with no PoleModels match at all now defaults to a
        representative rated capacity (80) instead of leaving
        PanelPercentage NULL for that reading -- treat an unmatched
        model the same as a matched one with a sensible default, not
        "unknown"."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "ISNULL(pm.SunboardPower, 80)" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_power_defaults_to_30_when_model_unmatched(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "ISNULL(pm.LightPower, 30)" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_nullif_zero_guard_still_wraps_the_defaulted_value(self, period_type):
        """The divide-by-zero guard must wrap AROUND the ISNULL default,
        not the other way around -- NULLIF(ISNULL(x, 80), 0), not
        ISNULL(NULLIF(x, 0), 80) -- so it still catches a genuinely-
        matched PoleModels row that explicitly has SunboardPower/
        LightPower = 0 (the defaults themselves, 80/30, are never 0, so
        this ordering doesn't change behavior for the default case, but
        DOES matter for the explicit-zero case)."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "NULLIF(ISNULL(pm.SunboardPower, 80), 0)" in sql
        assert "NULLIF(ISNULL(pm.LightPower, 30), 0)" in sql
        assert "ISNULL(NULLIF(pm.SunboardPower, 0), 80)" not in sql
        assert "ISNULL(NULLIF(pm.LightPower, 0), 30)" not in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_filters_by_last_upload_cutoff(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "WHERE t.LastUpload >= ?" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_aggregates_with_avg_and_count(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "AVG(BatteryPercentage)" in sql
        # AS AvgPanelPercentage/AvgLightPercentage (the column aliases),
        # not the bare "AVG(PanelPercentage)"/"AVG(LightPercentage)"
        # expressions -- Last48Hours now computes these two conditionally
        # (see TestLast48HoursConditionalPanelAndLightAverages below),
        # while Hour/Day still use the plain, unconditional AVG().
        assert "AS AvgPanelPercentage" in sql
        assert "AS AvgLightPercentage" in sql
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
    def test_no_light_status_anywhere(self, period_type):
        """LightStatus (the old Working/DayLight/Not Working
        classification) is gone entirely, replaced by fault flags --
        unlike IsDaylight, which was restored specifically to drive
        IsLedFault with real sunrise/sunset math (see this file's own
        module-level comment for that history)."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "LightStatus" not in sql

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

    def test_hour_match_key_includes_period_start(self):
        """Hour is a genuine historical bucket sequence -- PeriodStart is
        part of its identity, unlike Last48Hours (see
        TestLast48HoursMergeSqlStructure for why that one differs)."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
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


class TestLast48HoursMergeSqlStructure:
    """
    Last48Hours is a genuinely different kind of "period" from Hour: a
    single, continuously-updated rolling window per pole, not one of a
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
        assert "DATEADD(HOUR, -48, SYSDATETIMEOFFSET()" in sql
        assert "SYSDATETIMEOFFSET() AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd" in sql

    def test_period_start_and_end_are_converted_to_eastern(self):
        """SYSDATETIMEOFFSET() alone reflects the SERVER's own time zone
        -- Azure SQL Database runs in UTC regardless of physical region
        -- which would otherwise show PeriodStart/PeriodEnd as +00:00
        while every other "now"-style timestamp in this project (e.g.
        SP_Execution's StartDateTime/EndDateTime) is Eastern. Applying AT
        TIME ZONE before subtracting 48 hours doesn't change WHICH
        absolute instant PeriodStart lands on -- DATEADD operates on the
        underlying instant, not the display offset -- only how that same
        instant is displayed."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "SYSDATETIMEOFFSET() AT TIME ZONE 'Eastern Standard Time'" in sql
        # Both PeriodStart and PeriodEnd get the conversion, not just one.
        assert sql.count("SYSDATETIMEOFFSET() AT TIME ZONE 'Eastern Standard Time'") == 2

    def test_no_pole_timezones_join_needed(self):
        """IsLedFault now reads t.IsDaylight directly (computed and
        cached by pole_daylight_flags_loader.py), not a local-time clock
        check -- so unlike the brief period where this DID need
        PoleTimeZones (for a since-removed fixed clock window), there's
        no reason for this period type to join it at all anymore. Still
        no local-time bucketing either way: this is a pure duration
        window, not calendar-aligned."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "PoleTimeZones" not in sql
        assert "TimeZoneName" not in sql

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
        assert "WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_led_fault_excludes_daylight_via_is_daylight_for_led_fault_column(self, period_type):
        """Solar-powered lights are supposed to be off during daylight --
        LampPower1+LampPower2=0 while t.IsDaylightForLedFault=1 must
        never be flagged as a fault, regardless of the LampPower values
        -- the daylight check must come first in the CASE expression and
        unconditionally return 0. Uses t.IsDaylightForLedFault, NOT the
        stricter t.IsDaylight used by IsPanelFaultFlag -- a deliberately
        more forgiving definition (true at the exact moment, or up to a
        1-hour grace period before it -- see
        pole_daylight_flags_loader.py's own _LED_FAULT_GRACE_PERIOD),
        confirmed necessary in practice: a real lamp doesn't always turn
        on the instant the sun crosses the sunset threshold."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        led_fault_case = sql.split("AS IsLedFaultFlag")[0].split("CASE")[-1]
        assert "WHEN t.IsDaylightForLedFault = 1 THEN 0" in led_fault_case
        # The daylight WHEN must appear before the LampPower WHEN, so
        # it's checked first and short-circuits regardless of LampPower.
        daylight_pos = led_fault_case.find("IsDaylightForLedFault")
        lamp_power_pos = led_fault_case.find("LampPower1")
        assert daylight_pos < lamp_power_pos

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_led_fault_does_not_use_the_strict_is_daylight_column(self, period_type):
        """Regression guard for the two flags needing genuinely
        different daylight definitions: IsLedFaultFlag's own CASE
        expression must reference IsDaylightForLedFault, never the
        plain, strict IsDaylight that IsPanelFaultFlag uses instead."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        led_fault_case = sql.split("AS IsLedFaultFlag")[0].split("CASE")[-1]
        assert "t.IsDaylight = " not in led_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_led_fault_null_is_daylight_for_led_fault_falls_through_to_lamp_power_check(self, period_type):
        """A reading pole_daylight_flags_loader.py hasn't processed yet
        (t.IsDaylightForLedFault IS NULL) must be treated the same as
        "confirmed dark" -- subject to the normal LampPower check -- not
        silently exempted from fault detection just because its daylight
        status isn't known yet. NULL = 1 is UNKNOWN (not TRUE) in T-SQL,
        so this falls out of the CASE's own NULL-propagation naturally,
        without needing an explicit ISNULL() guard."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        led_fault_case = sql.split("AS IsLedFaultFlag")[0].split("CASE")[-1]
        # No ISNULL/COALESCE guard around IsDaylightForLedFault -- NULL
        # propagation handles it correctly on its own; adding one would
        # be redundant, not incorrect, so this documents the intentional
        # simplicity rather than testing for an absence that would
        # otherwise be meaningless.
        assert "WHEN t.IsDaylightForLedFault = 1 THEN 0" in led_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_uses_its_own_daylight_column_not_the_led_variant(self, period_type):
        """IsPanelFaultFlag has its own daylight definition
        (IsDaylightForPanelFault, a sunrise-only warmup grace period) --
        must never reference IsDaylightForLedFault (a completely
        different, symmetric grace period tuned for lamp response lag)
        or the plain, unmodified IsDaylight (which neither fault flag
        reads directly anymore)."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in panel_fault_case
        assert "IsDaylightForLedFault" not in panel_fault_case
        assert "t.IsDaylight = " not in panel_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_led_fault_no_longer_references_local_time_or_clock_window(self, period_type):
        """Regression guard: this used to be a fixed clock-time window
        (BETWEEN '07:00:00' AND '20:00:00', via a LocalTime computation)
        before being replaced by real sunrise/sunset math -- neither
        should be present in the CASE expression anymore."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        led_fault_case = sql.split("AS IsLedFaultFlag")[0].split("CASE")[-1]
        assert "LocalTime" not in led_fault_case
        assert "BETWEEN" not in led_fault_case
        assert "07:00:00" not in led_fault_case

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
        assert "WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_excludes_nighttime_and_sunrise_warmup_via_is_daylight_for_panel_fault(
        self, period_type
    ):
        """Solar panels only charge once past the sunrise warmup period
        -- zero panel output while t.IsDaylightForPanelFault=0 (either
        confirmed night, OR daylight but still within the first hour
        after sunrise) must never be flagged as a fault, regardless of
        the panel-output values -- this check must come first in the
        CASE expression and unconditionally return 0."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in panel_fault_case
        # The daylight-for-panel-fault WHEN must appear before both the
        # battery-current WHEN and the panel-output WHEN, so it's
        # checked first and short-circuits regardless of either.
        daylight_pos = panel_fault_case.find("IsDaylightForPanelFault")
        battery_current_pos = panel_fault_case.find("BatteryElecCurrent1")
        panel_output_pos = panel_fault_case.find("SolarBoardVoltage")
        assert daylight_pos < battery_current_pos < panel_output_pos

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_null_is_daylight_for_panel_fault_falls_through_to_panel_output_check(self, period_type):
        """A reading pole_daylight_flags_loader.py hasn't processed yet
        (t.IsDaylightForPanelFault IS NULL) must be treated the same as
        "confirmed past warmup" -- subject to the normal panel-output
        check -- not silently exempted from fault detection just because
        its daylight status isn't known yet. NULL = 0 is UNKNOWN (not
        TRUE) in T-SQL, so this falls out of the CASE's own
        NULL-propagation naturally, without needing an explicit ISNULL()
        guard. The mirror image of IsLedFault's own NULL handling, which
        falls through to being treated as "confirmed dark" instead."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in panel_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_excludes_when_total_battery_current_is_exactly_200(self, period_type):
        """A solar panel only needs to charge when the battery actually
        needs it -- when the TOTAL (not average) of
        BatteryElecCurrent1 + BatteryElecCurrent2 is exactly 200, zero
        panel output is expected (nothing left to charge), not a fault,
        even during daylight. Replaces an earlier version of this check
        (average BatteryVoltage1/BatteryVoltage2 against a per-model
        BatteryChargingMin threshold from PoleModels) by explicit
        request."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0" in panel_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_uses_battery_elec_current_not_battery_voltage(self, period_type):
        """Easy to confuse with IsBatteryFaultFlag's own
        BatteryElecCurrent1/BatteryElecCurrent2 (a genuinely different
        fault check, using the same columns) -- but IsPanelFaultFlag now
        ALSO uses BatteryElecCurrent1/2 (their TOTAL, not average, and
        compared to exactly 200, not < 10) since the earlier
        BatteryVoltage-based check was replaced. BatteryVoltage1/2
        themselves are no longer referenced anywhere in this CASE
        expression at all."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "BatteryElecCurrent1" in panel_fault_case
        assert "BatteryElecCurrent2" in panel_fault_case
        assert "BatteryVoltage" not in panel_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_missing_battery_current_still_falls_through_to_panel_output_check(
        self, period_type
    ):
        """Missing BatteryElecCurrent1/2 readings (a NULL total) must
        still be treated as "unknown whether the battery needs
        charging" -- falls through to the normal panel-output check,
        not silently exempted. NULL = 200 is UNKNOWN, not TRUE, in
        T-SQL, so this falls out of the CASE's own NULL-propagation
        naturally."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0" in panel_fault_case

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_fault_no_longer_involves_pole_models_or_battery_charging_min(self, period_type):
        """DELIBERATE CHANGE: the earlier version of this check depended
        on PoleModels (a per-model BatteryChargingMin threshold,
        defaulting to 13.5 for an unmatched ModelId) -- the new
        total-battery-current check needs no PoleModels involvement at
        all. BatteryChargingMin has been removed from PoleModels
        entirely (see "sql/PoleModels/Drop BatteryChargingMin
        column.sql")."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        panel_fault_case = sql.split("AS IsPanelFaultFlag")[0].split("CASE")[-1]
        assert "BatteryChargingMin" not in panel_fault_case
        assert "pm." not in panel_fault_case

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

    def test_last_48_hours_partitions_row_number_by_location_only(self):
        """No bucket dimension to partition by -- the whole window IS
        the one bucket per pole."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "PARTITION BY t.LocationId ORDER BY t.LastUpload DESC" in sql
        assert "AS LatestOverall" in sql

    def test_hour_extracts_via_max_case_when_rn_equals_1(self):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
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


class TestPerPoleTimeZonePropagation:
    """
    Dedicated coverage for the per-pole (not hardcoded-Eastern) timezone
    feature -- specifically that TimeZoneName survives the GROUP BY
    intact. Hour only -- Last48Hours joins PoleTimeZones too now (for
    IsLedFaultFlag's daytime check), but never carries TimeZoneName
    through to a GROUP BY the way Hour does (see
    TestLast48HoursMergeSqlStructure.test_pole_time_zones_joined_only_for_led_fault_not_bucketing).
    """

    def test_bucketed_cte_selects_time_zone_name(self):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
        bucketed_cte = sql.split("Bucketed AS (")[1].split("Aggregated AS (")[0]
        assert "TimeZoneName" in bucketed_cte

    def test_aggregated_cte_groups_by_time_zone_name(self):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
        assert "GROUP BY LocationId, TimeZoneName" in sql

    def test_time_zone_name_defined_before_bucketed_cte_uses_it(self):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
        telemetry_cte_end = sql.index("Bucketed AS (")
        definition = sql[:telemetry_cte_end]
        assert "ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName" in definition

    def test_uses_per_pole_timezone_with_eastern_fallback(self):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
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

    def test_retention_limits_only_defined_for_hour(self):
        """Last48Hours needs no pruning -- it's structurally always
        exactly one row per pole (matched on LocationId+PeriodType alone,
        not PeriodStart -- see _LAST_48_HOURS_MERGE_SQL)."""
        assert pole_vitals_loader._RETENTION_LIMITS == {"Hour": 720}
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
    def test_full_success_flow_executes_both_period_types_in_order(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (77,)
        mock_cursor.rowcount = 5

        pole_vitals_loader.load_pole_vitals()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 8

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleVitals", "Dev", "Leadsun")

        # MERGE calls are at indices 1 (Hour), 3 (Last48Hours)
        merge_indices = {"Hour": 1, "Last48Hours": 3}
        for period_type, idx in merge_indices.items():
            call = calls[idx]
            merge_sql = call.args[0]
            assert len(call.args) == 5  # merge_sql + cutoff, sentinel, source, sp_exec_id
            _, sentinel, source_name, sp_exec_id = call.args[1:]
            assert f"'{period_type}' AS PeriodType" in merge_sql
            assert sentinel == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
            assert source_name == "Leadsun"
            assert sp_exec_id == 77

        # Retention prune call is at index 2 (Hour)
        hour_prune = calls[2].args
        assert hour_prune == (pole_vitals_loader._RETENTION_PRUNE_SQL, "Hour", "Hour", 720)

        # Last48Hours' own stale-row cleanup is at index 4, using the
        # SAME cutoff/sentinel bound into its own MERGE at index 3.
        last48_cleanup = calls[4].args
        assert last48_cleanup[0] == pole_vitals_loader._LAST_48_HOURS_STALE_ROW_PRUNE_SQL
        last48_merge_cutoff = calls[3].args[1]
        assert last48_cleanup[1] == last48_merge_cutoff
        assert last48_cleanup[2] == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL

        # LastKnown48Hours: copy at index 5, fresh-compute-for-offline at
        # index 6 -- both run AFTER Last48Hours' own MERGE+cleanup above.
        copy_call = calls[5].args
        assert copy_call[0] == pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL
        assert copy_call[1:] == ("Leadsun", 77)

        fresh_compute_call = calls[6].args
        assert (
            fresh_compute_call[0]
            == pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        )
        assert fresh_compute_call[1:] == (
            pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL,
            pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL,
            "Leadsun",
            77,
        )

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[7].args
        assert "UPDATE SP_Execution" in update_sql
        # 5 rows x 2 period types (Hour/Last48Hours) + 5 (copy) + 5
        # (fresh-compute) -- mock_cursor.rowcount is a single, shared
        # value applying to every execute() call in this test, including
        # the two LastKnown48Hours statements.
        assert (success, errors, batch_count, sp_exec_id) == (20, 0, 3, 77)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_default_run_uses_small_lookback_not_backfill_window(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=False)

        for idx in (1, 3):  # Hour, Last48Hours MERGE calls
            cutoff = mock_cursor.execute.call_args_list[idx].args[-4]
            assert cutoff > "2025-06-01", idx  # comfortably within ~13 months, not 400 days

    def test_backfill_true_widens_hour_but_not_last_48_hours(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=True)

        hour_cutoff = mock_cursor.execute.call_args_list[1].args[-4]
        last48_cutoff = mock_cursor.execute.call_args_list[3].args[-4]
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
            None,  # Last48Hours MERGE
            None,  # Last48Hours stale-row cleanup
            None,  # LastKnown48Hours copy
            None,  # LastKnown48Hours fresh-compute-for-offline
            None,  # final update
        ]

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 0  # the 01003 "failure" must not count as an error
        # 2 period types x 7 rows each (Hour/Last48Hours), plus 7
        # (copy) + 7 (fresh-compute) -- rowcount is shared across every
        # execute() call in this test.
        assert success == 28

    def test_01003_warning_logs_as_info_not_error(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 3
        mock_cursor.execute.side_effect = [
            None,
            self._make_01003_exception(),
            None, None, None, None, None, None,
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
        # index 0=insert, 1=Hour(ok), 2=Hour prune(ok), 3=Day(FAILS),
        # 4=Last48Hours(ok), 5=Last48Hours cleanup(ok), 6=LastKnown48Hours
        # copy(ok), 7=LastKnown48Hours fresh-compute(ok), 8=final update
        mock_cursor.execute.side_effect = [
            None, None, None, overflow_exc, None, None, None, None, None,
        ]

        pole_vitals_loader.load_pole_vitals()  # must not raise (per-period isolation)

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1


class TestLoadPoleVitalsPerPeriodTypeCommits:
    """
    Confirms load_pole_vitals() commits after EACH period type
    individually (MERGE + retention prune together, where applicable),
    not once at the end for both.

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

        # insert+commit, then (MERGE, prune, commit) for Hour, then
        # (MERGE, stale-row cleanup, commit) for Last48Hours, then
        # (copy, fresh-compute, commit) for LastKnown48Hours, then final
        # update+commit.
        assert call_order == [
            "execute", "commit",             # SP_Execution insert
            "execute", "execute", "commit",  # Hour MERGE + prune
            "execute", "execute", "commit",  # Last48Hours MERGE + stale-row cleanup
            "execute", "execute", "commit",  # LastKnown48Hours copy + fresh-compute
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
        assert call_order.count("commit") == 5  # insert + 2 period types + LastKnown48Hours + final update

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
            "execute", "execute", "commit",  # Last48Hours -- still attempted, succeeds
            "execute", "execute", "commit",  # LastKnown48Hours -- still attempted, succeeds
            "execute", "commit",             # SP_Execution final update
        ]

    def test_an_earlier_period_types_commit_already_happened_before_a_later_failure(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 0=insert, 1=Hour MERGE(ok), 2=Hour prune(ok), 3=Last48Hours MERGE(FAILS)
        call_order = self._track_calls(mock_conn, mock_cursor, {3: RuntimeError("Last48Hours failed")})

        pole_vitals_loader.load_pole_vitals()

        assert call_order == [
            "execute", "commit",             # SP_Execution insert
            "execute", "execute", "commit",  # Hour -- already committed here
            "execute", "rollback",           # Last48Hours MERGE -- fails AFTER Hour's commit above
            "execute", "execute", "commit",  # LastKnown48Hours -- still attempted, succeeds
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
        # Last48Hours (5) + LastKnown48Hours copy + fresh-compute
        # (5 each) = 15; Hour failed, so it contributes 0 and 1 error.
        assert (success, errors) == (15, 1)


class TestLast48HoursStaleRowCleanup:
    """
    Dedicated coverage for the gap this closes: a pole that goes
    completely silent (zero telemetry within the current 48-hour window)
    would otherwise keep its last successfully-computed Last48Hours row
    forever, since _LAST_48_HOURS_MERGE_SQL's own source query only ever
    includes poles that DO still have recent telemetry -- a silent pole
    simply never appears in that source, so the MERGE can neither update
    nor remove its existing row.
    """

    def test_stale_row_prune_sql_targets_last_48_hours_only(self):
        sql = pole_vitals_loader._LAST_48_HOURS_STALE_ROW_PRUNE_SQL
        assert "WHERE pv.PeriodType = 'Last48Hours'" in sql

    def test_stale_row_prune_sql_checks_for_any_recent_telemetry_at_all(self):
        sql = pole_vitals_loader._LAST_48_HOURS_STALE_ROW_PRUNE_SQL
        assert "NOT EXISTS" in sql
        assert "FROM PoleTelemetry t" in sql
        assert "WHERE t.LocationId = pv.LocationId" in sql
        assert "AND t.LastUpload >= ?" in sql
        assert "AND t.LastUpload <> ?" in sql

    def test_cleanup_dispatches_to_retention_prune_for_hour(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 3

        result = pole_vitals_loader._run_cleanup_for_period_type(
            mock_cursor, "Hour", cutoff="2026-08-01 00:00:00.000 -04:00"
        )

        mock_cursor.execute.assert_called_once_with(
            pole_vitals_loader._RETENTION_PRUNE_SQL, "Hour", "Hour", 720
        )
        assert result == 3

    def test_cleanup_dispatches_to_stale_row_prune_for_last_48_hours(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 2
        cutoff = "2026-08-01 00:00:00.000 -04:00"

        result = pole_vitals_loader._run_cleanup_for_period_type(
            mock_cursor, "Last48Hours", cutoff=cutoff
        )

        mock_cursor.execute.assert_called_once_with(
            pole_vitals_loader._LAST_48_HOURS_STALE_ROW_PRUNE_SQL,
            cutoff,
            pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL,
        )
        assert result == 2

    def test_cleanup_returns_zero_not_negative_for_zero_or_missing_rowcount(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        result = pole_vitals_loader._run_cleanup_for_period_type(
            mock_cursor, "Last48Hours", cutoff="2026-08-01 00:00:00.000 -04:00"
        )

        assert result == 0

    def test_stale_pole_removed_end_to_end(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        """A pole with an existing Last48Hours row but zero current
        telemetry: the MERGE's own source is empty for it (nothing to
        update), but the stale-row cleanup step still runs and its
        rowcount is correctly picked up as the "pruned" count for
        logging -- confirmed by checking exactly which execute() call is
        the cleanup step and that its bound SQL/params match."""
        mock_cursor.fetchone.return_value = (1,)
        call_count = {"n": 0}

        def _execute_with_stale_cleanup_rowcount(*args, **kwargs):
            call_count["n"] += 1
            # The Last48Hours stale-row cleanup is the 5th execute() call
            # in a normal, no-exception run (see
            # test_full_success_flow_executes_both_period_types_in_order
            # for the full index layout this mirrors): 1=insert,
            # 2=Hour MERGE, 3=Hour prune, 4=Last48Hours MERGE,
            # 5=Last48Hours stale-row cleanup.
            mock_cursor.rowcount = 1 if call_count["n"] == 5 else 0

        mock_cursor.execute.side_effect = _execute_with_stale_cleanup_rowcount

        pole_vitals_loader.load_pole_vitals()

        cleanup_call = mock_cursor.execute.call_args_list[4]
        assert cleanup_call.args[0] == pole_vitals_loader._LAST_48_HOURS_STALE_ROW_PRUNE_SQL

    def test_one_period_type_failing_does_not_block_the_others(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            RuntimeError("Hour failed"),  # Hour MERGE
            None,  # Last48Hours MERGE
            None,  # Last48Hours stale-row cleanup
            None,  # LastKnown48Hours copy
            None,  # LastKnown48Hours fresh-compute-for-offline
            None,  # final update
        ]
        mock_cursor.rowcount = 3

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1
        # 1 successful period type (Last48Hours) + copy +
        # fresh-compute, x 3 rows each
        assert success == 9

    def test_logs_error_for_failed_period_type(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.execute.side_effect = [
            None,
            RuntimeError("boom"),
            None, None, None, None, None,
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


class TestLoadPoleVitalsFailureRecordingUsesAFreshConnection:
    """
    Regression guard for a real production incident: an 08S01
    "Communication link failure" during the new LastKnown48Hours step
    (cursor.execute()) also broke the connection itself, so the inner
    except block's own conn.rollback() call ALSO failed with the same
    08S01 error -- and since that rollback failure wasn't caught by
    anything, it escaped this loader's per-period-type error isolation
    entirely and reached this top-level except block, which (before this
    fix) reused that SAME, already-dead connection to try to record the
    failure -- failing a THIRD time, uncaught, crashing the whole
    loadLeadsunData timer invocation with SP_Execution's own row left
    half-finished. Same fix, same reasoning, as pole_daylight_flags_loader.py/
    pole_timezones_loader.py's own equivalent, and this module's own
    backfill_*_for_all_poles() functions -- load_pole_vitals() itself was
    the one loader in this project that had never had it applied.

    The ORIGINAL trigger for this class (execute() fails, then
    conn.rollback() ALSO fails, within an inner except block) no longer
    reaches this top-level handler at all -- _safe_rollback() now
    contains that failure locally (see TestSafeRollback and
    TestLoadPoleVitalsRollbackFailureIsContained below), which is a
    genuine improvement, not a regression: one period type's rollback
    failing no longer crashes the entire run. This class's own tests
    below use a DIFFERENT, still-valid trigger instead -- the final,
    step-3 SP_Execution UPDATE itself failing, which isn't preceded by
    any rollback and so still reaches this exact handler unchanged.
    """

    def _make_conn(self):
        conn = MagicMock(name="conn")
        cursor = MagicMock(name="cursor")
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_original_exception_still_raised_when_recording_succeeds(self, mocker):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.rowcount = 5  # a real int -- avoids a TypeError from the rowcount-tracking comparison itself
        # All 7 recompute-phase calls succeed (insert, Hour MERGE+prune,
        # Last48Hours MERGE+cleanup, LastKnown48Hours copy+fresh-compute);
        # the 8th call -- step 3's own final SP_Execution UPDATE -- is
        # what fails here. Not preceded by any rollback, so this reaches
        # the top-level except block directly, unrelated to
        # _safe_rollback() entirely.
        main_cursor.execute.side_effect = [
            None, None, None, None, None, None, None,
            pyodbc.OperationalError("08S01", "Communication link failure (SQLExecDirectW)"),
        ]

        recovery_conn, recovery_cursor = self._make_conn()

        mocker.patch(
            "shared.pole_vitals_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(pyodbc.OperationalError, match="SQLExecDirectW"):
            pole_vitals_loader.load_pole_vitals()

        assert recovery_cursor.execute.called
        update_sql, end_time, error_message, success, errors, sp_exec_id = (
            recovery_cursor.execute.call_args.args
        )
        assert "UPDATE SP_Execution" in update_sql
        assert "SQLExecDirectW" in error_message
        assert sp_exec_id == 55
        recovery_conn.commit.assert_called_once()
        recovery_cursor.close.assert_called_once()
        recovery_conn.close.assert_called_once()
        main_cursor.close.assert_called_once()
        main_conn.close.assert_called_once()

    def test_original_exception_still_raised_when_recording_also_fails(self, mocker, caplog):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.rowcount = 5
        main_cursor.execute.side_effect = [
            None, None, None, None, None, None, None,
            RuntimeError("original communication failure"),
        ]

        recovery_conn, recovery_cursor = self._make_conn()
        recovery_cursor.execute.side_effect = RuntimeError("recovery also failed")

        mocker.patch(
            "shared.pole_vitals_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="original communication failure"):
                pole_vitals_loader.load_pole_vitals()

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("original communication failure" in msg for msg in error_messages)
        assert any(
            "additionally failed to record this run's failure" in msg and "recovery also failed" in msg
            for msg in error_messages
        )


class TestSafeRollback:
    def test_successful_rollback_does_nothing_extra(self, caplog):
        conn = MagicMock()

        with caplog.at_level("WARNING"):
            pole_vitals_loader._safe_rollback(conn, "someContext")

        conn.rollback.assert_called_once()
        assert not any(rec.levelname == "WARNING" for rec in caplog.records)

    def test_failed_rollback_is_caught_and_logged_as_a_warning_not_reraised(self, caplog):
        conn = MagicMock()
        conn.rollback.side_effect = RuntimeError("connection already broken")

        with caplog.at_level("WARNING"):
            pole_vitals_loader._safe_rollback(conn, "someContext")  # must not raise

        warning_messages = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert any("someContext" in msg and "connection already broken" in msg for msg in warning_messages)


class TestLoadPoleVitalsRollbackFailureIsContained:
    """
    Confirms the actual fix for the originally-reported incident: when
    LastKnown48Hours' own execute() fails AND the subsequent rollback
    ALSO fails, load_pole_vitals() no longer crashes the entire run --
    _safe_rollback() contains it, the ORIGINAL error (not the rollback's
    own) gets logged with full context, and the run completes normally,
    recording this as one counted error rather than an uncaught,
    "n/a"-style crash with no useful message anywhere in the logs.
    """

    def test_run_completes_without_raising(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 5 = LastKnown48Hours copy (ok), index 6 = fresh-compute (fails)
        mock_cursor.execute.side_effect = [
            None, None, None, None, None, None,
            pyodbc.OperationalError("08S01", "Communication link failure (SQLExecDirectW)"),
            None,
        ]
        mock_conn.rollback.side_effect = pyodbc.OperationalError(
            "08S01", "Communication link failure (SQLEndTran)"
        )

        with caplog.at_level("ERROR"):
            pole_vitals_loader.load_pole_vitals()  # must NOT raise

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any(
            "failed to recompute LastKnown48Hours" in msg and "SQLExecDirectW" in msg
            for msg in error_messages
        )

    def test_final_update_still_counts_this_as_one_error(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        mock_cursor.execute.side_effect = [
            None, None, None, None, None, None,
            RuntimeError("original failure"),
            None,
        ]
        mock_conn.rollback.side_effect = RuntimeError("rollback also failed")

        pole_vitals_loader.load_pole_vitals()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        # Hour/Last48Hours (5 each = 10) succeeded; LastKnown48Hours
        # counted as exactly 1 error, same as any other period type's
        # own isolated failure.
        assert (success, errors) == (10, 1)


# --------------------------------------------------------------------------
# backfill_latest_hour_for_all_poles() -- a GENUINELY DIFFERENT query shape
# from load_pole_vitals()'s own Hour handling, not a parameter variation:
# ensures every pole's "Hour" row reflects its OWN most recent telemetry,
# no matter how old, rather than only ever looking within a global time
# window relative to "now" (which even backfill=True still does).
# --------------------------------------------------------------------------


class TestBackfillLatestHourPerPoleMergeSqlStructure:
    def test_sunboard_power_and_light_power_defaults_present(self):
        """This copy must stay in sync with _HOUR_MERGE_SQL's own
        ISNULL defaults, same as its fault-flag formulas -- see this
        class's own test_fault_flag_formulas_match_the_normal_hour_merge_exactly."""
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert "NULLIF(ISNULL(pm.SunboardPower, 80), 0)" in sql
        assert "NULLIF(ISNULL(pm.LightPower, 30), 0)" in sql

    def test_has_no_global_last_upload_cutoff_parameter(self):
        """The defining difference from _HOUR_MERGE_SQL: no
        "t.LastUpload >= ?" anywhere -- each pole's own window is
        determined entirely from its own data, not a value passed in
        from "now"."""
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert "LastUpload >= ?" not in sql

    def test_still_excludes_the_sentinel_last_upload_value(self):
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert sql.count("LastUpload <> ?") == 2  # once in each CTE that reads PoleTelemetry directly

    def test_finds_each_poles_own_max_last_upload(self):
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        max_reading_cte = sql.split("MaxReadingPerPole AS (")[1].split("LatestBucketPerPole AS (")[0]
        assert "MAX(t.LastUpload) AS MaxLastUpload" in max_reading_cte
        assert "GROUP BY t.LocationId" in max_reading_cte

    def test_converts_each_poles_max_reading_to_its_own_local_time_zone(self):
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        bucket_cte = sql.split("LatestBucketPerPole AS (")[1].split("TelemetryWithVitals AS (")[0]
        assert "AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time')" in bucket_cte
        assert "LEFT JOIN PoleTimeZones ptz ON mr.LocationId = ptz.LocationId" in bucket_cte

    def test_scopes_each_poles_readings_to_its_own_bucket_range(self):
        """The per-pole equivalent of a WHERE clause -- each pole's own
        readings are filtered against ITS OWN bucket boundaries (joined
        in via LatestBucketPerPole), not a single, shared range."""
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert "JOIN LatestBucketPerPole lb ON t.LocationId = lb.LocationId" in sql
        assert "CAST(t.LastUpload AT TIME ZONE lb.TimeZoneName AS DATETIME2(3)) >= lb.BucketStart" in sql
        assert (
            "CAST(t.LastUpload AT TIME ZONE lb.TimeZoneName AS DATETIME2(3)) < DATEADD(HOUR, 1, lb.BucketStart)"
            in sql
        )

    def test_targets_the_hour_period_type(self):
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert "'Hour' AS PeriodType" in sql

    def test_fault_flag_formulas_match_the_normal_hour_merge_exactly(self):
        """The whole point of this being an intentional copy, not a
        divergent reimplementation: the SAME fault-flag definitions must
        produce the SAME classification, whether computed by the normal
        scheduled loader or this one-off backfill."""
        backfill_sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        hour_sql = pole_vitals_loader._HOUR_MERGE_SQL

        assert (
            "WHEN t.IsDaylightForLedFault = 1 THEN 0" in backfill_sql
            and "WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1" in backfill_sql
        )
        assert (
            "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in backfill_sql
            and "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0"
            in backfill_sql
            and "WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1" in backfill_sql
        )
        assert "(t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag" in backfill_sql
        # And confirm those exact same fragments are genuinely present in
        # the normal Hour merge too, not just independently written the
        # same way by coincidence.
        for fragment in (
            "WHEN t.IsDaylightForLedFault = 1 THEN 0",
            "WHEN t.IsDaylightForPanelFault = 0 THEN 0",
            "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0",
            "WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1",
        ):
            assert fragment in hour_sql

    def test_upsert_column_lists_match_the_normal_hour_merge(self):
        """Same target table, same columns, same match key -- only the
        USING subquery's own scoping differs."""
        backfill_sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert "MERGE PoleVitals AS target" in backfill_sql
        assert (
            "ON target.LocationId = source.LocationId\n"
            "   AND target.PeriodType = source.PeriodType\n"
            "   AND target.PeriodStart = source.PeriodStart" in backfill_sql
        )

    def test_wraps_in_ansi_warnings_off_on_like_the_normal_hour_merge(self):
        sql = pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert sql.strip().startswith("SET ANSI_WARNINGS OFF;")
        assert sql.strip().endswith("SET ANSI_WARNINGS ON;")


class TestBackfillLatestHourForAllPolesSuccessFlow:
    def test_full_success_flow_call_sequence(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.rowcount = 42

        pole_vitals_loader.backfill_latest_hour_for_all_poles()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 3

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("backfillLatestHourPoleVitals", "Dev", "Leadsun")

        merge_sql, sentinel1, sentinel2, source_name, sp_exec_id = calls[1].args
        assert merge_sql == pole_vitals_loader._BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
        assert sentinel1 == sentinel2 == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
        assert source_name == "Leadsun"
        assert sp_exec_id == 99

        update_sql, end_time, success, errors, batch_count, sp_exec_id2 = calls[2].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id2) == (42, 0, 1, 99)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_zero_rowcount_does_not_go_negative(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        pole_vitals_loader.backfill_latest_hour_for_all_poles()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (0, 0)


class TestBackfillLatestHourForAllPolesBenignWarningHandling:
    def test_sqlstate_01003_still_commits_and_reports_success(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        benign_warning = Exception()
        benign_warning.args = ("01003", "Warning: Null value is eliminated")
        mock_cursor.execute.side_effect = [None, benign_warning, None]
        mock_cursor.rowcount = 10

        pole_vitals_loader.backfill_latest_hour_for_all_poles()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (10, 0)
        assert mock_conn.commit.called

    def test_genuine_failure_rolls_back_and_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        mock_cursor.execute.side_effect = [None, RuntimeError("deadlock"), None]

        with pytest.raises(RuntimeError, match="deadlock"):
            pole_vitals_loader.backfill_latest_hour_for_all_poles()

        mock_conn.rollback.assert_called_once()


class TestBackfillLatestHourForAllPolesTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db connection lost")

        with pytest.raises(RuntimeError, match="db connection lost"):
            pole_vitals_loader.backfill_latest_hour_for_all_poles()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestBackfillLatestHourFailureRecordingUsesAFreshConnection:
    """
    Same fix, and same reasoning, as pole_daylight_flags_loader.py/
    pole_timezones_loader.py's own equivalent: recording a run's failure
    in SP_Execution must not reuse a connection that may itself be the
    thing that just failed, or that SECOND failure propagates instead of
    the original, more useful one.
    """

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
            "shared.pole_vitals_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="communication link failure"):
            pole_vitals_loader.backfill_latest_hour_for_all_poles()

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
            "shared.pole_vitals_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="original communication failure"):
                pole_vitals_loader.backfill_latest_hour_for_all_poles()

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("original communication failure" in msg for msg in error_messages)
        assert any(
            "additionally failed to record this run's failure" in msg and "recovery also failed" in msg
            for msg in error_messages
        )


# --------------------------------------------------------------------------
# backfill_last_48_hours_of_hour_for_all_poles() -- a BROADER relative of
# backfill_latest_hour_for_all_poles() above: up to 48 hourly buckets per
# pole (every hour with telemetry in a 48-hour window ending at that
# pole's own latest reading), not just the single newest one.
# --------------------------------------------------------------------------


class TestBackfillLast48HoursOfHourPerPoleMergeSqlStructure:
    def test_has_no_global_last_upload_cutoff_parameter(self):
        """Same defining property as the single-bucket variant: no
        "t.LastUpload >= ?" anywhere -- each pole's own window is
        determined entirely from its own data, not a value passed in
        from "now"."""
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert "LastUpload >= ?" not in sql

    def test_excludes_the_sentinel_last_upload_value_twice(self):
        """Once for MaxReadingPerPole's own MAX(LastUpload) computation,
        once for TelemetryWithVitals' own reading-level filter -- same
        shape as the single-bucket variant's own two sentinel
        exclusions."""
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert sql.count("LastUpload <> ?") == 2

    def test_finds_each_poles_own_max_last_upload(self):
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        max_reading_cte = sql.split("MaxReadingPerPole AS (")[1].split("TelemetryWithVitals AS (")[0]
        assert "MAX(t.LastUpload) AS MaxLastUpload" in max_reading_cte
        assert "GROUP BY t.LocationId" in max_reading_cte

    def test_scopes_each_poles_readings_to_a_48_hour_range_ending_at_its_own_max(self):
        """The defining difference from the single-bucket variant: a
        RANGE of readings (up to 48 hours' worth), not just the ones
        falling into a single hour bucket."""
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert "JOIN MaxReadingPerPole mr ON t.LocationId = mr.LocationId" in sql
        assert "WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)" in sql
        assert "AND t.LastUpload <= mr.MaxLastUpload" in sql

    def test_buckets_by_local_hour_same_as_the_normal_hour_merge(self):
        """Every reading within the 48-hour range still gets bucketed
        into its own local hour -- same DATEADD/DATEDIFF truncation
        expression as _HOUR_MERGE_SQL's own Bucketed CTE, not a
        per-pole single bucket like the OTHER backfill variant."""
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert (
            "DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart" in sql
        )

    def test_targets_the_hour_period_type(self):
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert "'Hour' AS PeriodType" in sql

    def test_fault_flag_and_default_formulas_match_the_normal_hour_merge_exactly(self):
        """Same reasoning as the single-bucket variant's own equivalent
        test: an intentional copy, not a divergent reimplementation."""
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert "WHEN t.IsDaylightForLedFault = 1 THEN 0" in sql
        assert "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in sql
        assert "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0" in sql
        assert "pm.BatteryChargingMin" not in sql  # (comments explaining the change are fine; live code references are not)
        assert "NULLIF(ISNULL(pm.SunboardPower, 80), 0)" in sql
        assert "NULLIF(ISNULL(pm.LightPower, 30), 0)" in sql

    def test_upsert_column_lists_match_the_normal_hour_merge(self):
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert "MERGE PoleVitals AS target" in sql
        assert (
            "ON target.LocationId = source.LocationId\n"
            "   AND target.PeriodType = source.PeriodType\n"
            "   AND target.PeriodStart = source.PeriodStart" in sql
        )

    def test_wraps_in_ansi_warnings_off_on(self):
        sql = pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert sql.strip().startswith("SET ANSI_WARNINGS OFF;")
        assert sql.strip().endswith("SET ANSI_WARNINGS ON;")


class TestBackfillLast48HoursOfHourForAllPolesSuccessFlow:
    def test_full_success_flow_call_sequence(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.rowcount = 420

        pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 3

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("backfillLast48HoursOfHourPoleVitals", "Dev", "Leadsun")

        merge_sql, sentinel1, sentinel2, source_name, sp_exec_id = calls[1].args
        assert merge_sql == pole_vitals_loader._BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
        assert sentinel1 == sentinel2 == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
        assert source_name == "Leadsun"
        assert sp_exec_id == 99

        update_sql, end_time, success, errors, batch_count, sp_exec_id2 = calls[2].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id2) == (420, 0, 1, 99)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_zero_rowcount_does_not_go_negative(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (0, 0)


class TestBackfillLast48HoursOfHourForAllPolesBenignWarningHandling:
    def test_sqlstate_01003_still_commits_and_reports_success(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        benign_warning = Exception()
        benign_warning.args = ("01003", "Warning: Null value is eliminated")
        mock_cursor.execute.side_effect = [None, benign_warning, None]
        mock_cursor.rowcount = 300

        pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (300, 0)
        assert mock_conn.commit.called

    def test_genuine_failure_rolls_back_and_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        mock_cursor.execute.side_effect = [None, RuntimeError("deadlock"), None]

        with pytest.raises(RuntimeError, match="deadlock"):
            pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles()

        mock_conn.rollback.assert_called_once()


class TestBackfillLast48HoursOfHourForAllPolesTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db connection lost")

        with pytest.raises(RuntimeError, match="db connection lost"):
            pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestBackfillLast48HoursOfHourFailureRecordingUsesAFreshConnection:
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
            "shared.pole_vitals_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="communication link failure"):
            pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles()

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


# --------------------------------------------------------------------------
# LastKnown48Hours -- a new period type: identical to Last48Hours for a
# currently-active pole (literally copied), but a fresh per-pole-anchored
# rollup for a pole that's gone completely silent.
# --------------------------------------------------------------------------


class TestLastKnown48HoursCopyFromLast48HoursSqlStructure:
    def test_reads_directly_from_last_48_hours_rows(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL
        assert "FROM PoleVitals" in sql
        assert "WHERE PeriodType = 'Last48Hours'" in sql

    def test_writes_as_last_known_48_hours_period_type(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL
        assert "'LastKnown48Hours' AS PeriodType" in sql

    def test_has_no_sql_aggregate_of_its_own(self):
        """Confirms this really is a plain copy, not a recomputation --
        no AVG()/MAX() anywhere, since it just selects Last48Hours'
        already-computed columns verbatim."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL
        assert "AVG(" not in sql
        assert "MAX(" not in sql

    def test_matches_on_location_id_and_period_type_alone(self):
        """Same structural convention as _LAST_48_HOURS_MERGE_SQL's own
        MERGE -- always exactly one row per pole."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL
        assert (
            "ON target.LocationId = source.LocationId\n"
            "   AND target.PeriodType = source.PeriodType" in sql
        )
        assert "PeriodStart = source.PeriodStart" not in sql.split("ON target.LocationId")[1].split("WHEN MATCHED")[0]

    def test_only_two_bound_parameters(self):
        """Source, SP_ExecId only -- nothing else to parameterize, since
        this reads from PoleVitals itself, not PoleTelemetry."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL
        assert sql.count("?") == 2


class TestLastKnown48HoursFreshComputeForOfflinePolesSqlStructure:
    def test_has_no_global_last_upload_cutoff(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert "LastUpload >= ?" not in sql

    def test_only_targets_poles_without_a_current_last_48_hours_row(self):
        """The defining scoping condition: a pole with SOME telemetry,
        but no Last48Hours row -- i.e. genuinely offline, not just any
        pole."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        # ";WITH CandidateOfflinePoles AS (" -- the leading ";WITH "
        # anchor matters here: plain "OfflinePoles AS (" is also a
        # substring of "CandidateOfflinePoles AS (" itself, so without
        # this more specific anchor this split silently isolates the
        # wrong (much larger) slice instead of raising -- a real false
        # positive this exact test previously had after the CandidateOfflinePoles rename.
        candidate_offline_poles_cte = sql.split(";WITH CandidateOfflinePoles AS (")[1].split(
            "MaxReadingPerCandidatePole AS ("
        )[0]
        assert "NOT EXISTS" in candidate_offline_poles_cte
        assert "PoleVitals pv" in candidate_offline_poles_cte
        assert "pv.PeriodType = 'Last48Hours'" in candidate_offline_poles_cte

    def test_finds_each_candidate_poles_own_max_last_upload(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        cte = sql.split("MaxReadingPerCandidatePole AS (")[1].split("OfflinePolesNeedingRecompute AS (")[0]
        assert "MAX(t.LastUpload) AS MaxLastUpload" in cte
        assert "JOIN CandidateOfflinePoles cop ON t.LocationId = cop.LocationId" in cte

    def test_only_recomputes_poles_whose_last_known_48_hours_is_not_already_up_to_date(self):
        """The real performance fix: a pole whose existing
        LastKnown48Hours.PeriodEnd already matches this SAME
        MaxLastUpload is excluded here -- nothing has changed since the
        last time this ran, so recomputing would just reproduce an
        identical result. Without this, every pole that has ever gone
        silent gets recomputed on every single run, forever."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        cte = sql.split("OfflinePolesNeedingRecompute AS (")[1].split("TelemetryWithVitals AS (")[0]
        assert "NOT EXISTS" in cte
        assert "lk.PeriodType = 'LastKnown48Hours'" in cte
        assert "lk.PeriodEnd = mr.MaxLastUpload" in cte

    def test_scopes_readings_to_a_48_hour_range_ending_at_each_poles_own_max(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert "JOIN OfflinePolesNeedingRecompute mr ON t.LocationId = mr.LocationId" in sql
        assert "WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)" in sql
        assert "AND t.LastUpload <= mr.MaxLastUpload" in sql

    def test_period_start_end_anchored_to_max_last_upload_not_now(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert "SYSDATETIMEOFFSET()" not in sql
        assert (
            "DATEADD(HOUR, -48, MaxLastUpload AT TIME ZONE 'Eastern Standard Time') AS PeriodStart"
            in sql
        )
        assert "MaxLastUpload AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd" in sql

    def test_writes_as_last_known_48_hours_period_type(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert "'LastKnown48Hours' AS PeriodType" in sql

    def test_fault_flag_and_default_formulas_match_the_normal_last_48_hours_merge_exactly(self):
        """Same reasoning as this project's other per-pole-anchored
        variants (e.g. backfill_last_48_hours_of_hour_for_all_poles()'s
        own equivalent test): an intentional copy, not a divergent
        reimplementation."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert "WHEN t.IsDaylightForLedFault = 1 THEN 0" in sql
        assert "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in sql
        assert "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0" in sql
        assert "pm.BatteryChargingMin" not in sql  # (comments explaining the change are fine; live code references are not)
        assert "NULLIF(ISNULL(pm.SunboardPower, 80), 0)" in sql
        assert "NULLIF(ISNULL(pm.LightPower, 30), 0)" in sql

    def test_wraps_in_ansi_warnings_off_on(self):
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert sql.strip().startswith("SET ANSI_WARNINGS OFF;")
        assert sql.strip().endswith("SET ANSI_WARNINGS ON;")

    def test_telemetry_with_vitals_exposes_last_upload_for_aggregated_to_reference(self):
        """Regression guard for a real production bug: Aggregated's own
        MAX(LastUpload) reads from TelemetryWithVitals, but that CTE's
        SELECT list originally (copied from _LAST_48_HOURS_MERGE_SQL,
        which never needed this) only used t.LastUpload INSIDE the
        ROW_NUMBER() window function's own ORDER BY -- never actually
        exposed it as an output column. That's valid SQL for the window
        function itself, but left LastUpload unavailable to any CTE
        downstream, causing SQL Server error 207, "Invalid column name
        'LastUpload'", the first time this actually ran. t.LastUpload
        must appear as its own, plain output column in
        TelemetryWithVitals's SELECT list, not just inside the window
        function expression."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        telemetry_with_vitals_cte = sql.split("TelemetryWithVitals AS (")[1].split(
            "FROM PoleTelemetry t\n    JOIN MaxReadingPerOfflinePole"
        )[0]
        assert "t.LastUpload," in telemetry_with_vitals_cte

    def test_four_bound_parameters(self):
        """2 sentinel exclusions (OfflinePoles, MaxReadingPerOfflinePole)
        + Source + SP_ExecId."""
        sql = pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        assert sql.count("?") == 4


class TestLoadPoleVitalsLastKnown48HoursIntegration:
    def test_runs_after_last_48_hours_merge_and_cleanup(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        """Confirms ordering: both LastKnown48Hours statements must come
        AFTER Last48Hours' own MERGE and stale-row cleanup, since the
        copy reads Last48Hours' just-written rows and the offline-pole
        query depends on seeing that table's latest, post-MERGE state."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 1

        pole_vitals_loader.load_pole_vitals()

        calls = [c.args[0] for c in mock_cursor.execute.call_args_list]
        last48_merge_idx = calls.index(pole_vitals_loader._LAST_48_HOURS_MERGE_SQL)
        last48_cleanup_idx = calls.index(pole_vitals_loader._LAST_48_HOURS_STALE_ROW_PRUNE_SQL)
        copy_idx = calls.index(pole_vitals_loader._LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL)
        fresh_compute_idx = calls.index(
            pole_vitals_loader._LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
        )
        assert last48_merge_idx < last48_cleanup_idx < copy_idx < fresh_compute_idx

    def test_benign_warning_on_fresh_compute_only_counts_that_statements_rows(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        """The copy statement has no aggregate of its own and so can
        never itself raise SQLSTATE 01003 -- only the fresh-compute
        statement can. Confirms the benign-warning branch still counts
        the copy's own (already-committed-by-the-time-of-the-exception)
        rowcount correctly, not just the fresh-compute's."""
        mock_cursor.fetchone.return_value = (1,)
        benign_exc = Exception("01003", "Warning: Null value is eliminated by an aggregate...")
        # Indices: 0=insert, 1=Hour, 2=Hour prune, 3=Day, 4=Day prune,
        # 5=Last48Hours, 6=Last48Hours cleanup, 7=copy(ok),
        # 8=fresh-compute(benign warning), 9=final update
        mock_cursor.execute.side_effect = [
            None, None, None, None, None, None, None, None, benign_exc, None,
        ]
        mock_cursor.rowcount = 4

        pole_vitals_loader.load_pole_vitals()  # must not raise

        assert mock_conn.rollback.called is False
        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 0

    def test_genuine_failure_on_copy_statement_rolls_back_and_is_isolated(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        # index 5 = LastKnown48Hours copy statement
        mock_cursor.execute.side_effect = [
            None, None, None, None, None,
            RuntimeError("copy failed"),
            None, None,
        ]

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        # Hour/Last48Hours (5 each = 10) all succeeded; LastKnown48Hours failed.
        assert (success, errors) == (10, 1)
        mock_conn.rollback.assert_called_once()


# --------------------------------------------------------------------------
# backfill_last_known_48_hours_for_offline_poles_after_formula_change() --
# a one-off backfill needed specifically because IsPanelFaultFlag's own
# formula changed (average BatteryVoltage1/BatteryVoltage2 vs
# BatteryChargingMin -> total BatteryElecCurrent1 + BatteryElecCurrent2
# equal to exactly 200), and the normal "skip if this pole's own data
# hasn't changed" optimization would otherwise leave every already-
# offline pole's LastKnown48Hours row silently stuck on the OLD
# formula's result forever.
# --------------------------------------------------------------------------


class TestBackfillLastKnown48HoursForceRecomputeSqlStructure:
    def test_no_offline_poles_needing_recompute_filter(self):
        """The defining difference from the normal, scheduled
        _LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL: this
        backfill deliberately has NO "skip if already up to date" filter
        -- it recomputes every genuinely offline pole unconditionally."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert "OfflinePolesNeedingRecompute AS (" not in sql
        assert "JOIN OfflinePolesNeedingRecompute" not in sql

    def test_joins_max_reading_per_candidate_pole_directly(self):
        """Without the normal filter CTE in between, TelemetryWithVitals
        must join straight onto MaxReadingPerCandidatePole -- the
        unfiltered set of every genuinely offline pole."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert "JOIN MaxReadingPerCandidatePole mr ON t.LocationId = mr.LocationId" in sql

    def test_still_finds_only_genuinely_offline_poles(self):
        """The offline-detection logic itself (a pole with real
        telemetry but no current Last48Hours row) is unchanged -- only
        the "already up to date" narrowing is removed, not the
        fundamental "is this pole actually offline" check."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert "CandidateOfflinePoles AS (" in sql
        candidate_cte = sql.split("CandidateOfflinePoles AS (")[1].split(
            "MaxReadingPerCandidatePole AS ("
        )[0]
        assert "NOT EXISTS" in candidate_cte
        assert "pv.PeriodType = 'Last48Hours'" in candidate_cte

    def test_uses_the_current_panel_fault_formula(self):
        """The whole point of this backfill existing: recomputes using
        whatever IsPanelFaultFlag formula is CURRENTLY in the code, not
        whatever formula was in effect the last time a given offline
        pole's row was computed."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert "WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0" in sql
        assert "pm.BatteryChargingMin" not in sql

    def test_writes_as_last_known_48_hours_period_type(self):
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert "'LastKnown48Hours' AS PeriodType" in sql

    def test_wraps_in_ansi_warnings_off_on(self):
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert sql.strip().startswith("SET ANSI_WARNINGS OFF;")
        assert sql.strip().endswith("SET ANSI_WARNINGS ON;")

    def test_six_bound_parameters(self):
        """CHANGED from 4 to 6, for batching support: TOP (?) batch
        size, sentinel (CandidateOfflinePoles), SP_ExecId exclusion
        (skip poles an earlier batch in this same run already
        processed), sentinel (MaxReadingPerCandidatePole), Source,
        SP_ExecId (for the MERGE's own INSERT/UPDATE values -- the SAME
        value as the exclusion parameter, but bound separately since
        it's used in a different part of the query)."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        assert sql.count("?") == 6

    def test_batch_size_parameter_appears_before_the_first_sentinel(self):
        """TOP (?) sits textually before CandidateOfflinePoles' own WHERE
        clause, so batch_size must be bound first, not last."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        top_pos = sql.index("TOP (?) t.LocationId")
        first_sentinel_pos = sql.index("t.LastUpload <> ?")
        assert top_pos < first_sentinel_pos

    def test_sp_exec_id_exclusion_appears_in_candidate_offline_poles_cte(self):
        """The batching-progress mechanism itself -- must be scoped to
        CandidateOfflinePoles (excluding a pole this same run's own
        SP_ExecId already touched), not accidentally applied somewhere
        else in the query."""
        sql = pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        candidate_cte = sql.split("CandidateOfflinePoles AS (")[1].split(
            "MaxReadingPerCandidatePole AS ("
        )[0]
        assert "lk.SP_ExecId = ?" in candidate_cte
        assert "lk.PeriodType = 'LastKnown48Hours'" in candidate_cte


class TestBackfillLastKnown48HoursForOfflinePolesSuccessFlow:
    def test_full_success_flow_call_sequence(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        """Simulates a realistic two-batch run: the first batch
        processes 12 poles, the second finds none left (rowcount 0),
        which is what naturally ends the loop."""
        mock_cursor.fetchone.return_value = (77,)
        merge_rowcounts = iter([12, 0])

        def execute_side_effect(sql, *params):
            if sql == pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL:
                mock_cursor.rowcount = next(merge_rowcounts)
            return None

        mock_cursor.execute.side_effect = execute_side_effect

        pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 4  # INSERT + 2 MERGE batches + final UPDATE

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == (
            "backfillLastKnown48HoursOfflinePolesAfterFormulaChange",
            "Dev",
            "Leadsun",
        )

        for call in (calls[1], calls[2]):
            merge_sql, batch_size, sentinel1, sp_exec_id_excl, sentinel2, source_name, sp_exec_id = (
                call.args
            )
            assert (
                merge_sql
                == pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
            )
            assert batch_size == 500  # the default
            assert sentinel1 == sentinel2 == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
            assert sp_exec_id_excl == sp_exec_id == 77
            assert source_name == "Leadsun"

        update_sql, end_time, success, errors, batch_count, sp_exec_id2 = calls[3].args
        assert "UPDATE SP_Execution" in update_sql
        # 12 from batch 1 + 0 from batch 2 = 12 total; 2 batches attempted.
        assert (success, errors, batch_count, sp_exec_id2) == (12, 0, 2, 77)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_custom_batch_size_is_respected(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0  # immediately nothing left -- one batch, then done

        pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change(
            batch_size=50
        )

        merge_call = mock_cursor.execute.call_args_list[1]
        assert merge_call.args[1] == 50

    def test_zero_rowcount_does_not_go_negative(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (0, 0)


class TestBackfillLastKnown48HoursForOfflinePolesBenignWarningHandling:
    def test_sqlstate_01003_still_commits_and_reports_success(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        benign_warning = Exception()
        benign_warning.args = ("01003", "Warning: Null value is eliminated")
        merge_call_count = [0]

        def execute_side_effect(sql, *params):
            if sql == pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL:
                merge_call_count[0] += 1
                if merge_call_count[0] == 1:
                    mock_cursor.rowcount = 9
                    raise benign_warning
                mock_cursor.rowcount = 0  # second batch: nothing left, loop ends
            return None

        mock_cursor.execute.side_effect = execute_side_effect

        pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (9, 0)
        assert mock_conn.commit.called

    def test_genuine_failure_rolls_back_and_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        mock_cursor.execute.side_effect = [None, RuntimeError("deadlock"), None]

        with pytest.raises(RuntimeError, match="deadlock"):
            pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        mock_conn.rollback.assert_called_once()


class TestBackfillLastKnown48HoursForOfflinePolesTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db connection lost")

        with pytest.raises(RuntimeError, match="db connection lost"):
            pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestBackfillLastKnown48HoursBatchingPreservesEarlierProgress:
    """
    Regression guard for the actual reported production incident: a
    connection timeout (SQLSTATE 08S01, WSAETIMEDOUT/10060) partway
    through what used to be a single, unbounded MERGE covering every
    offline pole at once. The fix batches that MERGE and commits after
    EVERY batch -- these tests confirm a LATER batch's own failure
    genuinely does not undo an EARLIER batch's already-committed work.
    """

    def test_second_batch_failing_still_commits_the_first_batchs_progress(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        merge_call_count = [0]

        def execute_side_effect(sql, *params):
            if sql == pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL:
                merge_call_count[0] += 1
                if merge_call_count[0] == 1:
                    mock_cursor.rowcount = 500  # first batch: a full batch, succeeds
                    return None
                raise pyodbc.OperationalError(
                    "08S01",
                    "[08S01] [Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: "
                    "Error code 0x274C (10060) (SQLExecDirectW)",
                )
            return None

        mock_cursor.execute.side_effect = execute_side_effect

        with pytest.raises(pyodbc.OperationalError, match="10060"):
            pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        # The first batch's own commit already happened, independently
        # of the second batch's later failure -- confirmed directly via
        # the mock's own call count, not inferred from the exception
        # alone.
        assert mock_conn.commit.call_count >= 2  # SP_Execution insert + first batch's own commit
        mock_conn.rollback.assert_called_once()  # only the FAILED (second) batch rolled back

    def test_each_batch_binds_the_same_sp_exec_id_so_progress_is_trackable(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        """Every batch within one run shares the same SP_ExecId -- this
        is the exact value the NEXT batch's own exclusion filter checks
        against, so a pole updated by batch 1 is correctly excluded from
        batch 2's own candidate set."""
        mock_cursor.fetchone.return_value = (42,)
        merge_rowcounts = iter([200, 0])

        def execute_side_effect(sql, *params):
            if sql == pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL:
                mock_cursor.rowcount = next(merge_rowcounts)
            return None

        mock_cursor.execute.side_effect = execute_side_effect

        pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        merge_calls = [
            c
            for c in mock_cursor.execute.call_args_list
            if c.args[0]
            == pole_vitals_loader._BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
        ]
        assert len(merge_calls) == 2
        for call in merge_calls:
            # (sql, batch_size, sentinel1, sp_exec_id_exclusion, sentinel2, source, sp_exec_id)
            assert call.args[3] == call.args[6] == 42


class TestBackfillLastKnown48HoursFailureRecordingUsesAFreshConnection:
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
            "shared.pole_vitals_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="communication link failure"):
            pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()

        assert recovery_cursor.execute.called
        update_sql, end_time, error_message, success, errors, batch_count, sp_exec_id = (
            recovery_cursor.execute.call_args.args
        )
        assert "UPDATE SP_Execution" in update_sql
        assert "communication link failure" in error_message
        assert batch_count == 1  # the single, failed batch
        assert sp_exec_id == 55
        recovery_conn.commit.assert_called_once()
        recovery_cursor.close.assert_called_once()
        recovery_conn.close.assert_called_once()
        main_cursor.close.assert_called_once()
        main_conn.close.assert_called_once()


# --------------------------------------------------------------------------
# AvgPanelPercentage/AvgLightPercentage conditional averaging -- a
# CHANGE, by explicit request, scoped ONLY to Last48Hours and
# LastKnown48Hours (both the normal, scheduled offline-pole computation
# and this project's own force-recompute backfill of it) -- Hour and Day
# deliberately keep the plain, unconditional AVG() from before.
# --------------------------------------------------------------------------


class TestLast48HoursConditionalPanelAndLightAverages:
    @pytest.mark.parametrize(
        "sql_constant_name",
        [
            "_LAST_48_HOURS_MERGE_SQL",
            "_LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL",
            "_BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL",
        ],
    )
    def test_avg_panel_percentage_only_considers_daylight_and_non_200_current(
        self, sql_constant_name
    ):
        """CHANGED by explicit request: AvgPanelPercentage now only
        considers readings taken during daylight AND where the total of
        BatteryElecCurrent1 + BatteryElecCurrent2 is NOT exactly 200 --
        readings outside that window no longer contribute to the
        average at all, via a conditional CASE that AVG() then ignores
        the NULLs from."""
        sql = getattr(pole_vitals_loader, sql_constant_name)
        assert (
            "AVG(CASE WHEN ISNULL(IsDaylightForPanelFault, 1) = 1 "
            "AND BatteryElecCurrentTotal <> 200 THEN PanelPercentage END) AS AvgPanelPercentage"
            in sql
        )

    @pytest.mark.parametrize(
        "sql_constant_name",
        [
            "_LAST_48_HOURS_MERGE_SQL",
            "_LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL",
            "_BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL",
        ],
    )
    def test_avg_light_percentage_only_considers_night_time(self, sql_constant_name):
        """CHANGED by explicit request: AvgLightPercentage now only
        considers readings taken at night -- daytime readings no longer
        contribute to the average at all."""
        sql = getattr(pole_vitals_loader, sql_constant_name)
        assert (
            "AVG(CASE WHEN ISNULL(IsDaylightForLedFault, 0) = 0 THEN LightPercentage END) "
            "AS AvgLightPercentage" in sql
        )

    @pytest.mark.parametrize(
        "sql_constant_name",
        [
            "_LAST_48_HOURS_MERGE_SQL",
            "_LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL",
            "_BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL",
        ],
    )
    def test_new_columns_exposed_for_the_conditional_averages_to_reference(
        self, sql_constant_name
    ):
        """Aggregated reads from TelemetryWithVitals, not directly from
        PoleTelemetry -- these three columns must be exposed as
        TelemetryWithVitals' own output columns, not just referenced
        inline elsewhere, or the conditional AVG()s above would fail
        with an invalid-column-name error (the same class of bug fixed
        once already for a different column earlier in this project)."""
        sql = getattr(pole_vitals_loader, sql_constant_name)
        telemetry_with_vitals_cte = sql.split("TelemetryWithVitals AS (")[1].split(
            "FROM PoleTelemetry t"
        )[0]
        assert "t.IsDaylightForPanelFault," in telemetry_with_vitals_cte
        assert "t.IsDaylightForLedFault," in telemetry_with_vitals_cte
        assert (
            "(t.BatteryElecCurrent1 + t.BatteryElecCurrent2) AS BatteryElecCurrentTotal,"
            in telemetry_with_vitals_cte
        )

    def test_null_handling_matches_each_flags_own_fault_check_precedent(self):
        """Not an arbitrary choice -- ISNULL(IsDaylightForPanelFault, 1)
        mirrors IsPanelFaultFlag's own "NULL falls through, treated as
        past warmup" behavior in this SAME query; ISNULL(
        IsDaylightForLedFault, 0) mirrors IsLedFaultFlag's own "NULL
        falls through, treated as confirmed dark" behavior. Checked
        against Last48Hours specifically since that's this project's
        one hand-written source of truth for this logic (the offline-
        pole variants are built by copying it programmatically)."""
        sql = pole_vitals_loader._LAST_48_HOURS_MERGE_SQL
        assert "WHEN t.IsDaylightForPanelFault = 0 THEN 0" in sql  # IsPanelFaultFlag's own NULL->daylight precedent
        assert "WHEN t.IsDaylightForLedFault = 1 THEN 0" in sql  # IsLedFaultFlag's own NULL->night precedent
        assert "ISNULL(IsDaylightForPanelFault, 1)" in sql
        assert "ISNULL(IsDaylightForLedFault, 0)" in sql

    def test_hour_is_unaffected_still_plain_unconditional_avg(self):
        """Explicit scope confirmation: this change was requested ONLY
        for Last48Hours/LastKnown48Hours -- Hour must keep the exact
        same plain, unconditional AVG() as before, with no new
        BatteryElecCurrentTotal/daylight columns introduced at all."""
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE["Hour"]
        assert "AVG(PanelPercentage)   AS AvgPanelPercentage," in sql
        assert "AVG(LightPercentage)   AS AvgLightPercentage," in sql
        assert "BatteryElecCurrentTotal" not in sql
