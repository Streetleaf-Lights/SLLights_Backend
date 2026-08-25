import os
import logging
import time
from datetime import timedelta

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.pole_telemetry_loader import _MISSING_LAST_UPLOAD_SENTINEL

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

PERIOD_TYPES = ("Hour", "Last48Hours")

# How many rows to KEEP per LocationId for each period type -- this table
# had no retention/pruning at all before this; it grew one row per pole
# per Hour forever. Hour is a genuinely historical, discrete bucket
# sequence, so pruning means "delete anything beyond the newest N,
# ORDER BY PeriodStart DESC" (see _RETENTION_PRUNE_SQL below).
# Last48Hours isn't in this dict at all -- it's a single, continuously
# upserted row per pole (its own MERGE matches on LocationId+PeriodType
# alone, not PeriodStart -- see _LAST_48_HOURS_MERGE_SQL's own comment),
# so there's structurally never more than one row per pole to prune.
_RETENTION_LIMITS = {
    # 720 = 30 days of hourly buckets, no gaps -- raised from an earlier
    # 168 (7 days) specifically so getPoleVitalsByPeriod(period_type=
    # 'Hour', limit=...) can actually serve up to 30 days back (see
    # pole_vitals_api.py's own _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE,
    # whose window bound is now derived from the caller's own limit
    # rather than a fixed 48). Raising this cap is NOT retroactive --
    # any row already pruned under the OLD, smaller cap is gone for
    # good (a genuine DELETE, not an archive/soft-delete -- see
    # _RETENTION_PRUNE_SQL below), so the full 30-day window won't
    # actually be AVAILABLE until that much new data has accumulated
    # under this new cap going forward.
    "Hour": 720,
}

# How far back each period type recomputes on a normal (non-backfill) run.
# For Hour, wide enough to cover "the current bucket + the previous
# bucket" (so late-arriving telemetry near a boundary still lands in the
# right bucket) without rescanning PoleTelemetry's full 6-month retention
# window every 10 minutes -- the same round-trip-count trap already hit
# (and fixed) for Poles/PoleTelemetry itself. Bounded by
# IX_PoleTelemetry_LastUpload_Covering.
#
# Last48Hours is different in kind, not just degree: it's not an
# incremental "current + previous bucket" window at all -- every run
# recomputes the ENTIRE rolling 48-hour window fresh (there's no
# "previous Last48Hours bucket" the way Hour has previous buckets),
# so its own lookback IS the full window, always exactly 48 hours,
# regardless of when this loader last ran.
_DEFAULT_LOOKBACK = {
    "Hour": timedelta(hours=3),
    "Last48Hours": timedelta(hours=48),
}

# Wide enough to cover PoleTelemetry's entire 6-month retention window --
# for a one-off historical backfill via load_pole_vitals(backfill=True).
# Doesn't apply to Last48Hours at all (see above -- there's no
# "backfill history" concept for a single rolling-window row; backfill=True
# only widens Hour's lookback).
_BACKFILL_LOOKBACK = timedelta(days=400)


def _compute_cutoff(now, period_type: str, backfill: bool):
    """
    Returns the DTO-formatted cutoff string for the WHERE t.LastUpload >= ?
    AND t.LastUpload <> ? parameters -- pure function, kept separate from
    load_pole_vitals() so the lookback-window math is unit-testable
    without a database. backfill is ignored for Last48Hours -- see
    _DEFAULT_LOOKBACK's own comment for why that period type has no
    backfill-history concept to widen.
    """
    if period_type == "Last48Hours":
        return _to_dto_string(now - _DEFAULT_LOOKBACK["Last48Hours"])
    lookback = _BACKFILL_LOOKBACK if backfill else _DEFAULT_LOOKBACK[period_type]
    return _to_dto_string(now - lookback)


# ----------------------------------------------------------------------
# Fault-flag design (replaces the earlier Daylight-based LightStatus
# classification -- LightStatus itself no longer exists anywhere in this
# schema; see the README for that history). IsDaylight, however, IS back
# -- restored specifically to drive IsLedFault below with real
# per-day/per-location sunrise/sunset math (shared/daylight_utils.py,
# via shared/pole_daylight_flags_loader.py), after a fixed 7:00AM-8:00PM
# clock window was tried first and found to have a real, unavoidable
# flaw: whichever bucket straddles the actual sunrise/sunset moment for a
# given day/location gets misclassified in one direction or the other.
#
# Four independent fault signals, computed per PoleTelemetry reading:
#   IsLedFault      = (LampPower1 + LampPower2) = 0, EXCEPT while
#                      IsDaylightForLedFault = 1 -- a solar-powered
#                      light is SUPPOSED to be off while the sun is
#                      actually up, so zero lamp power during real
#                      daylight is expected, correct behavior, never a
#                      fault; only once it's genuinely dark
#                      (IsDaylightForLedFault = 0, or NULL if not yet
#                      computed for this reading -- treated the same as
#                      confirmed-dark, not exempted) does zero lamp
#                      power indicate a real problem.
#                      IsDaylightForLedFault (a PoleTelemetry column,
#                      computed by pole_daylight_flags_loader.py) is
#                      DELIBERATELY not the same value as IsPanelFault's
#                      own IsDaylight below -- it's true at the exact
#                      moment, OR within a 1-hour grace period before OR
#                      after that moment, confirmed necessary in practice
#                      since a real lamp doesn't always turn on the
#                      instant the sun crosses the sunset threshold (nor
#                      off exactly at sunrise -- some lamps sense
#                      approaching dawn light and turn off slightly
#                      early). IsPanelFault has no equivalent lag (solar
#                      output tracks the sun
#                      closely), so it keeps using the strict column.
#   IsBatteryFault  = (BatteryElecCurrent1 + BatteryElecCurrent2) / 2 < 10
#   IsPanelFault    = (SolarBoardVoltage * SolarBoardElecCurrent) = 0,
#                      EXCEPT while IsDaylightForPanelFault = 0 (a solar
#                      panel only charges once it's been daylight for at
#                      least an hour -- IsDaylightForPanelFault, a
#                      PoleTelemetry column computed by
#                      pole_daylight_flags_loader.py, gives the panel
#                      time to physically warm up right after sunrise --
#                      so zero panel output at night OR during that
#                      first hour is expected, correct behavior, never a
#                      fault; only once it's past the warmup period
#                      (IsDaylightForPanelFault = 1, or NULL if not yet
#                      computed for this reading -- treated the same as
#                      confirmed-past-warmup, not exempted) does zero
#                      panel output indicate a real problem -- mirror
#                      image of IsLedFault above: each flag's own
#                      condition only applies during the OPPOSITE time
#                      of day), AND EXCEPT while the total of
#                      BatteryElecCurrent1 + BatteryElecCurrent2 is
#                      exactly 200 (replaces an earlier version of this
#                      check -- average BatteryVoltage1/BatteryVoltage2
#                      against a per-model BatteryChargingMin threshold
#                      from PoleModels -- by explicit request;
#                      BatteryChargingMin has been removed from
#                      PoleModels entirely).
#   IsOpenIssueFault = t.IsOpenIssueFault (already computed and stored per
#                      reading by pole_telemetry_loader.py -- joining
#                      PoleOpenIssues at read time here would be
#                      redundant work already done at write time)
# IsPoleFault = IsLedFault OR IsBatteryFault OR IsPanelFault OR
# IsOpenIssueFault -- never stored as its own per-reading column, only
# computed once per bucket below (and again, identically, at the
# getPoleVitals API layer for anything reading PoleVitals directly).
#
# A reading with NULL inputs (e.g. LampPower1/2 both NULL) does NOT get
# treated as faulted -- SQL's NULL propagation means
# "(NULL + NULL) = 0" evaluates to UNKNOWN, not TRUE, so these CASE
# expressions naturally fall through to "not a fault" rather than
# guessing. Missing data is a different state from confirmed-zero
# output, even though this project has no separate "unknown" tri-state
# for these specific flags (unlike LightStatus's old NULL-vs-DayLight
# distinction) -- treating "we don't know" as "not faulted" is the more
# conservative failure mode here (a real fault only ever produces actual
# zero/low readings, not missing ones, in practice).
#
# Bucket-level aggregation (Hour/Last48Hours both share this same
# logic, just over different windows of readings):
#   IsLedFault/IsBatteryFault/IsPanelFault: ANY reading in the window
#     faulted -> the whole bucket is faulted. A single confirmed fault
#     within the window shouldn't get averaged away by several
#     otherwise-fine readings.
#   IsOpenIssueFault: NOT an aggregate across the window at all -- takes
#     the single MOST RECENT reading's own IsOpenIssueFault value
#     (identified via ROW_NUMBER() ... ORDER BY LastUpload DESC, then
#     MAX(CASE WHEN rn = 1 THEN ...) to extract that one row's value --
#     a standard "pick a column from the row with max/min of another
#     column, within a group" pattern). This differs from the other
#     three specifically because open-issue status is a current fact
#     about the POLE (from a completely different data source,
#     PoleOpenIssues), not something that should be judged by whether it
#     was EVER true across a whole window of otherwise-unrelated
#     telemetry readings.
#   IsPoleFault: OR of all four, computed once in the final SELECT feeding
#     each MERGE below.
#
# IsOnline bucket-level aggregation is unchanged from the prior design:
# "was ANY reading in the window online" -- both Hour and Last48Hours use
# this same definition, just over their own respective windows.
# ----------------------------------------------------------------------

# A GENUINELY DIFFERENT query shape from _HOUR_MERGE_SQL below, not a
# parameter variation of it -- built for a specific, one-off need:
# ensure EVERY pole has an up-to-date "Hour" PoleVitals row reflecting
# its own most recent known telemetry, even for a pole that's gone
# completely silent (its latest reading is older than
# _DEFAULT_LOOKBACK["Hour"]'s normal 3-hour window, and even older than
# Last48Hours' own 48-hour window) -- such a pole's "Hour" row would
# otherwise never get touched again by the normal, scheduled run, no
# matter how long this loader keeps running, since it simply never
# appears in that run's own global time-cutoff filter.
#
# load_pole_vitals(backfill=True) does NOT solve this the way it might
# look like it should: it widens Hour's lookback to
# _BACKFILL_LOOKBACK (400 days), but that's a GLOBAL cutoff still --
# it recomputes EVERY historical hourly bucket within that huge window,
# for every pole, which is both enormously more expensive than what's
# needed here and produces the wrong shape of result (many old buckets
# per pole, not "just make sure the ONE latest bucket per pole is
# current").
#
# Scoping mechanism: for each pole independently, find its own
# MAX(LastUpload), convert that to the pole's own local time, truncate
# to the start of that local hour, then include only THAT pole's own
# readings that fall within that same local hour -- no global time
# cutoff parameter at all, since each pole's own window floats
# independently based on its own data, not "now".
#
# The fault-flag/aggregation logic below (TelemetryWithVitals's own CASE
# expressions and comments, Bucketed, Aggregated, and the final MERGE)
# is IDENTICAL to _HOUR_MERGE_SQL's own -- deliberately kept as an exact
# copy rather than shared/factored out, matching this project's existing
# convention of Hour/Last48Hours each carrying their own full copy
# rather than a shared SQL view or stored procedure. This does mean a
# FOURTH copy to keep in sync if these fault-flag definitions change
# again -- a real, known cost, not an oversight.
_BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH MaxReadingPerPole AS (
    SELECT
        t.LocationId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
    GROUP BY t.LocationId
),
LatestBucketPerPole AS (
    SELECT
        mr.LocationId,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        DATEADD(
            HOUR,
            DATEDIFF(
                HOUR, '19000101',
                CAST(mr.MaxLastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3))
            ),
            '19000101'
        ) AS BucketStart
    FROM MaxReadingPerPole mr
    LEFT JOIN PoleTimeZones ptz ON mr.LocationId = ptz.LocationId
),
TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        lb.TimeZoneName,
        lb.BucketStart,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        -- ISNULL(pm.SunboardPower, 80)/ISNULL(pm.LightPower, 30):
        -- a ModelId with no PoleModels match at all (the LEFT JOIN
        -- above produces NULL for both) now defaults to a representative
        -- rated capacity instead of leaving PanelPercentage/
        -- LightPercentage NULL for that reading -- an unmatched model is
        -- treated the same as a
        -- matched one with a sensible default, not "unknown", so it
        -- still contributes to this bucket's AVG() instead of silently
        -- dropping out of it. NULLIF(..., 0) still wraps the OUTSIDE of
        -- that default, unchanged from before -- it now only guards
        -- against a genuinely-matched PoleModels row that explicitly
        -- has SunboardPower/LightPower = 0 (a real, if unusual, model
        -- record), since the default values themselves (80, 30) are
        -- never 0.
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- See _HOUR_MERGE_SQL's own comment on this exact CASE
        -- expression for the full reasoning -- copied verbatim here,
        -- not re-explained.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- See _HOUR_MERGE_SQL's own comment on this exact CASE
        -- expression for the full reasoning -- copied verbatim here,
        -- not re-explained.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.LastUpload
    FROM PoleTelemetry t
    JOIN LatestBucketPerPole lb ON t.LocationId = lb.LocationId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
      AND CAST(t.LastUpload AT TIME ZONE lb.TimeZoneName AS DATETIME2(3)) >= lb.BucketStart
      AND CAST(t.LastUpload AT TIME ZONE lb.TimeZoneName AS DATETIME2(3)) < DATEADD(HOUR, 1, lb.BucketStart)
),
Bucketed AS (
    SELECT
        LocationId,
        TimeZoneName,
        BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        ROW_NUMBER() OVER (
            PARTITION BY LocationId, BucketStart
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        LocationId,
        TimeZoneName,
        BucketStart,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestInBucket = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Hour' AS PeriodType,
        BucketStart AT TIME ZONE TimeZoneName AS PeriodStart,
        DATEADD(HOUR, 1, BucketStart) AT TIME ZONE TimeZoneName AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage,
        IsOnlineAgg AS IsOnline,
        CAST(IsLedFaultAgg AS BIT) AS IsLedFault,
        CAST(IsBatteryFaultAgg AS BIT) AS IsBatteryFault,
        CAST(IsPanelFaultAgg AS BIT) AS IsPanelFault,
        CAST(ISNULL(IsOpenIssueFaultAgg, 0) AS BIT) AS IsOpenIssueFault,
        CAST(
            CASE WHEN IsLedFaultAgg = 1 OR IsBatteryFaultAgg = 1 OR IsPanelFaultAgg = 1
                      OR ISNULL(IsOpenIssueFaultAgg, 0) = 1
                 THEN 1 ELSE 0 END
        AS BIT) AS IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM Aggregated
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
   AND target.PeriodStart = source.PeriodStart
WHEN MATCHED THEN UPDATE SET
    PeriodEnd            = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# A GENUINELY DIFFERENT scope from _BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
# above: that one produces exactly ONE Hour bucket per pole (its own
# single latest hour of telemetry). This one produces UP TO 48 hourly
# buckets per pole -- every hour that has telemetry within a 48-hour
# window ending at that SAME pole's own latest reading. Built for a
# specific, related need: pole_vitals_api.py's GetPoleVitalsByPeriod now
# anchors its own 48-hour display window to each pole's latest telemetry
# too (see _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE there) -- but that
# anchor change only actually shows something useful for an offline pole
# if PoleVitals rows genuinely exist across that pole's own last 48
# hours of activity in the first place. A pole that was already offline
# before this project's Hour-vitals logic existed, or one whose Hour
# rows from that window were never successfully computed for some other
# reason, needs this fuller backfill -- the single-bucket variant above
# only ever fixes the newest hour, not the 47 behind it.
#
# Structurally, this is _HOUR_MERGE_SQL's own TelemetryWithVitals/
# Bucketed/Aggregated/MERGE logic, copied verbatim (same reasoning as
# _BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL's own copy -- see that
# constant's comment) -- the ONLY structural difference is a new
# MaxReadingPerPole CTE feeding a per-pole, relative-to-its-own-data
# WHERE clause, replacing _HOUR_MERGE_SQL's own global
# "t.LastUpload >= ?" cutoff relative to "now".
_BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH MaxReadingPerPole AS (
    SELECT
        t.LocationId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
    GROUP BY t.LocationId
),
TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        -- ISNULL(pm.SunboardPower, 80)/ISNULL(pm.LightPower, 30):
        -- a ModelId with no PoleModels match at all (the LEFT JOIN
        -- above produces NULL for both) now defaults to a representative
        -- rated capacity instead of leaving PanelPercentage/
        -- LightPercentage NULL for that reading -- an unmatched model is
        -- treated the same as a
        -- matched one with a sensible default, not "unknown", so it
        -- still contributes to this bucket's AVG() instead of silently
        -- dropping out of it. NULLIF(..., 0) still wraps the OUTSIDE of
        -- that default, unchanged from before -- it now only guards
        -- against a genuinely-matched PoleModels row that explicitly
        -- has SunboardPower/LightPower = 0 (a real, if unusual, model
        -- record), since the default values themselves (80, 30) are
        -- never 0.
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- Solar-powered lights are SUPPOSED to be off during daylight --
        -- LampPower1+LampPower2=0 while the sun is actually up
        -- (t.IsDaylightForLedFault, computed via real per-day/
        -- per-location sunrise/sunset math in
        -- pole_daylight_flags_loader.py -- NOT a fixed clock window,
        -- which was tried first and had a real, unavoidable flaw:
        -- whichever bucket straddles the actual sunrise/sunset moment
        -- for a given day/location gets misclassified in one direction
        -- or the other) is expected, correct behavior, not a fault.
        -- Only when it's actually dark does zero lamp power indicate a
        -- real problem. See this CASE's own ordering: the daylight
        -- check comes first and unconditionally returns 0, regardless
        -- of LampPower -- only falls through to the actual LampPower
        -- check once it's established this reading is genuinely at
        -- night.
        --
        -- t.IsDaylightForLedFault is DELIBERATELY NOT the same as
        -- IsPanelFaultFlag's own t.IsDaylight below -- it's a more
        -- forgiving definition (true at the exact moment, OR within 1
        -- hour before OR after -- see pole_daylight_flags_loader.py's
        -- own _LED_FAULT_GRACE_PERIOD), confirmed necessary in
        -- practice: a real lamp doesn't always turn on the INSTANT the
        -- sun crosses the sunset threshold (the "before" side of the
        -- grace period), nor does one always turn off exactly at
        -- sunrise -- some lamps sense approaching dawn light and turn
        -- off slightly early (the "after" side). IsPanelFault has no
        -- equivalent lag (solar output genuinely does track the sun
        -- closely), so it keeps using the strict column unmodified --
        -- these two flags need different daylight definitions, which
        -- is exactly why this is two separate PoleTelemetry columns,
        -- not one shared value.
        --
        -- t.IsDaylightForLedFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through to the LampPower check below, same as "confirmed
        -- dark" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- Solar panels only need to charge when BOTH (a) it's been
        -- daylight for at least an hour (t.IsDaylightForPanelFault = 1
        -- -- see pole_daylight_flags_loader.py's own
        -- _PANEL_FAULT_SUNRISE_WARMUP_PERIOD; gives a panel time to
        -- physically warm up right after sunrise) AND (b) the battery
        -- actually needs it -- the TOTAL (not average) of
        -- BatteryElecCurrent1 + BatteryElecCurrent2 is NOT exactly 200.
        -- Replaces an earlier version of this same check (average
        -- BatteryVoltage1/BatteryVoltage2 against a per-model
        -- BatteryChargingMin threshold from PoleModels) by explicit
        -- request -- BatteryChargingMin itself has been removed from
        -- PoleModels entirely (see "sql/PoleModels/Drop
        -- BatteryChargingMin column.sql"), so PoleModels is no longer
        -- involved in this specific check at all (it's still joined
        -- below for SunboardPower/LightPower, used elsewhere in this
        -- same CTE -- just no longer for this). When the total current
        -- IS exactly 200, zero panel output is expected, correct
        -- behavior (nothing left to charge), not a fault, even during
        -- daylight. Only once it's past the sunrise warmup AND this
        -- condition does NOT hold does zero panel output indicate a
        -- real problem. See this CASE's own ordering:
        -- t.IsDaylightForPanelFault = 0 is checked first and
        -- unconditionally returns 0 regardless of anything else -- it's
        -- False both at night (no daylight at all) AND during the first
        -- hour after sunrise (daylight, but not yet past warmup), so
        -- this single check covers both cases without needing a
        -- separate plain-nighttime condition; the total-current check
        -- is checked second and ALSO unconditionally returns 0
        -- regardless of panel output -- only once both of those are
        -- ruled out does this fall through to the actual panel-output
        -- check.
        --
        -- t.IsDaylightForPanelFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through past that first check, same as "confirmed past
        -- warmup" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet. This is the mirror
        -- image of IsLedFaultFlag's own NULL handling above (which
        -- treats unknown as "confirmed dark" instead) -- each flag's
        -- fault condition only applies during the OPPOSITE time of day,
        -- so "unknown" always falls through to that flag's own check,
        -- just via a different comparison (= 1 there, = 0 here).
        --
        -- A NULL BatteryElecCurrent1/BatteryElecCurrent2 (missing
        -- readings) means the total is also NULL, and "NULL = 200" is
        -- UNKNOWN (not TRUE) in T-SQL -- so a missing reading falls
        -- through past this check too, same "unknown is treated as
        -- subject to the normal check" reasoning as the daylight-NULL
        -- case above, not silently exempted from fault detection.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.LastUpload
    FROM PoleTelemetry t
    JOIN MaxReadingPerPole mr ON t.LocationId = mr.LocationId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
    -- Bounded to THIS POLE'S OWN last 48 hours of real activity,
    -- ending at its own most recent reading -- NOT a global cutoff
    -- relative to "now" like _HOUR_MERGE_SQL's own WHERE clause
    -- uses. Deliberately > / <= (not >= / <) on the lower/upper
    -- bounds respectively: a half-open interval, so a reading
    -- exactly 48 hours before MaxLastUpload is excluded (giving a
    -- clean, unambiguous 48-hour span) while MaxLastUpload itself
    -- is always included.
    WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)
      AND t.LastUpload <= mr.MaxLastUpload
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Bucketed AS (
    SELECT
        LocationId,
        TimeZoneName,
        DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        -- Identifies each bucket's own most-recent reading, for
        -- IsOpenIssueFault's "take the last telemetry" rule below --
        -- NOT used for anything else (the other three fault flags and
        -- IsOnline are ANY-in-window, not last-in-window).
        ROW_NUMBER() OVER (
            PARTITION BY LocationId, DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        LocationId,
        TimeZoneName,
        BucketStart,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestInBucket = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Hour' AS PeriodType,
        BucketStart AT TIME ZONE TimeZoneName AS PeriodStart,
        DATEADD(HOUR, 1, BucketStart) AT TIME ZONE TimeZoneName AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage,
        IsOnlineAgg AS IsOnline,
        CAST(IsLedFaultAgg AS BIT) AS IsLedFault,
        CAST(IsBatteryFaultAgg AS BIT) AS IsBatteryFault,
        CAST(IsPanelFaultAgg AS BIT) AS IsPanelFault,
        CAST(ISNULL(IsOpenIssueFaultAgg, 0) AS BIT) AS IsOpenIssueFault,
        CAST(
            CASE WHEN IsLedFaultAgg = 1 OR IsBatteryFaultAgg = 1 OR IsPanelFaultAgg = 1
                      OR ISNULL(IsOpenIssueFaultAgg, 0) = 1
                 THEN 1 ELSE 0 END
        AS BIT) AS IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM Aggregated
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
   AND target.PeriodStart = source.PeriodStart
WHEN MATCHED THEN UPDATE SET
    PeriodEnd            = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# LastKnown48Hours -- a NEW period type, and a genuinely different beast
# from Hour/Last48Hours above: not one MERGE, but TWO, run as a pair
# (both below), because its own definition is conditional on whether a
# pole currently has a Last48Hours row at all:
#
#   - Pole currently has telemetry (a Last48Hours row exists) ->
#     LastKnown48Hours is IDENTICAL to that ALREADY-COMPUTED Last48Hours
#     row -- literally copied, not independently recomputed from
#     PoleTelemetry a second time. Cheaper, and guarantees the two
#     genuinely agree rather than risking two independent computations
#     of "the same thing" silently drifting apart at the edges (e.g. a
#     reading landing just inside one window's cutoff but not the
#     other's, computed a few seconds apart).
#   - Pole has gone completely silent (NO Last48Hours row -- see
#     _LAST_48_HOURS_STALE_ROW_PRUNE_SQL above for why that row
#     disappears once a pole stops reporting) -> LastKnown48Hours instead
#     rolls up THAT POLE'S OWN last 48 hours of telemetry it actually
#     HAS, ending at its own most recent reading, no matter how long ago
#     that was. This is the entire point of this period type existing at
#     all: Last48Hours is deliberately "what's happening right now" (and
#     disappears for a silent pole on purpose); LastKnown48Hours is "the
#     last thing we actually know about this pole", and persists.
#
# _LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL (this one) must run
# AFTER _LAST_48_HOURS_MERGE_SQL has already committed for this same run
# -- it reads Last48Hours' own just-written rows directly, not
# PoleTelemetry. _LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
# (below this one) must ALSO run after that same commit -- its own
# OfflinePoles CTE specifically looks for poles WITHOUT a current
# Last48Hours row, so it needs to see that table's latest, post-MERGE
# state to correctly identify who's actually offline right now.
#
# Matches on LocationId+PeriodType alone (no PeriodStart), same
# structural reasoning as _LAST_48_HOURS_MERGE_SQL's own MERGE -- always
# exactly one LastKnown48Hours row per pole, PeriodStart/PeriodEnd simply
# overwritten to the fresh window's bounds each run.
_LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL = """
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'LastKnown48Hours' AS PeriodType,
        PeriodStart,
        PeriodEnd,
        AvgBatteryPercentage,
        AvgPanelPercentage,
        AvgLightPercentage,
        IsOnline,
        IsLedFault,
        IsBatteryFault,
        IsPanelFault,
        IsOpenIssueFault,
        IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM PoleVitals
    WHERE PeriodType = 'Last48Hours'
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
WHEN MATCHED THEN UPDATE SET
    PeriodStart           = source.PeriodStart,
    PeriodEnd             = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
"""

# _LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL: structurally
# _LAST_48_HOURS_MERGE_SQL's own TelemetryWithVitals/Aggregated/MERGE
# logic, copied verbatim (same reasoning, same convention, as this
# project's other per-pole-anchored backfills -- see this file's own
# backfill_last_48_hours_of_hour_for_all_poles() and
# pole_telemetry_loader.py's backfill_is_open_issue_fault_for_all_poles()
# for the established precedent) -- the structural differences are new
# CandidateOfflinePoles/MaxReadingPerCandidatePole/
# OfflinePolesNeedingRecompute CTEs feeding a per-pole,
# relative-to-its-own-data WHERE clause and PeriodStart/PeriodEnd,
# replacing _LAST_48_HOURS_MERGE_SQL's own global cutoff relative to
# "now", plus the PeriodType literal itself.
#
# OfflinePolesNeedingRecompute is a real, load-bearing performance fix,
# not just naming -- see its own comment further down for the full
# reasoning: without it, this recomputes EVERY pole that has EVER gone
# silent across the whole retention window, on EVERY single run,
# forever, confirmed in practice as the cause of loadPoleVitals slowing
# down substantially once LastKnown48Hours was introduced. With it, a
# pole whose LastKnown48Hours row already correctly reflects its own
# (unchanging, since it's dead) latest reading is skipped entirely on
# every subsequent run.
_LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL = """
SET ANSI_WARNINGS OFF;
;WITH CandidateOfflinePoles AS (
    -- Every LocationId with real telemetry that DOESN'T currently have a
    -- Last48Hours row -- i.e. genuinely offline (no telemetry within the
    -- last 48 hours from now at all), since _LAST_48_HOURS_MERGE_SQL's
    -- own MERGE only ever produces/keeps a row for a pole that DOES.
    -- "Candidate" -- NOT yet the final set this actually recomputes for;
    -- see OfflinePolesNeedingRecompute below for the second, narrowing
    -- filter that matters for performance.
    SELECT DISTINCT t.LocationId
    FROM PoleTelemetry t
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
      AND NOT EXISTS (
          SELECT 1 FROM PoleVitals pv
          WHERE pv.LocationId = t.LocationId AND pv.PeriodType = 'Last48Hours'
      )
),
MaxReadingPerCandidatePole AS (
    SELECT
        t.LocationId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    JOIN CandidateOfflinePoles cop ON t.LocationId = cop.LocationId
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
    GROUP BY t.LocationId
),
OfflinePolesNeedingRecompute AS (
    -- A REAL performance fix, not a cosmetic rename: without this
    -- second filter, EVERY pole that has EVER gone silent -- across the
    -- entire retention window, potentially many months of history --
    -- gets its full 48-hour rollup recomputed from scratch on EVERY
    -- single loadPoleVitals run, forever, even though a truly dead
    -- pole's own telemetry never changes again once it's stopped
    -- reporting. Confirmed in practice as the actual cause of
    -- loadPoleVitals slowing down substantially after LastKnown48Hours
    -- was introduced.
    --
    -- This filters that candidate set down to only poles whose EXISTING
    -- LastKnown48Hours row (if any) does NOT already reflect this exact
    -- same MaxLastUpload -- i.e. either no LastKnown48Hours row exists
    -- yet at all (newly silent, or never computed before), or this
    -- pole's own latest reading has actually advanced since the last
    -- time this ran (it came back online briefly, or simply got one
    -- more reading before going quiet again). A pole that's been dead
    -- for months, whose LastKnown48Hours row was already computed
    -- correctly once, matches neither of those conditions on every
    -- subsequent run and is correctly skipped from here on -- reducing
    -- the ongoing, steady-state cost of this query to roughly "how many
    -- poles went newly silent since last time", not "how many poles
    -- have EVER been silent".
    --
    -- lk.PeriodEnd = mr.MaxLastUpload compares two DATETIMEOFFSET values
    -- directly -- valid and correct despite PeriodEnd being stored
    -- AT TIME ZONE 'Eastern Standard Time' for display, since
    -- DATETIMEOFFSET equality compares the underlying UTC instant, not
    -- the display offset.
    SELECT mr.LocationId, mr.MaxLastUpload
    FROM MaxReadingPerCandidatePole mr
    WHERE NOT EXISTS (
        SELECT 1 FROM PoleVitals lk
        WHERE lk.LocationId = mr.LocationId
          AND lk.PeriodType = 'LastKnown48Hours'
          AND lk.PeriodEnd = mr.MaxLastUpload
    )
),
TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        -- ISNULL(pm.SunboardPower, 80)/ISNULL(pm.LightPower, 30):
        -- a ModelId with no PoleModels match at all (the LEFT JOIN
        -- above produces NULL for both) now defaults to a representative
        -- rated capacity instead of leaving PanelPercentage/
        -- LightPercentage NULL for that reading -- an unmatched model is
        -- treated the same as a
        -- matched one with a sensible default, not "unknown", so it
        -- still contributes to this bucket's AVG() instead of silently
        -- dropping out of it. NULLIF(..., 0) still wraps the OUTSIDE of
        -- that default, unchanged from before -- it now only guards
        -- against a genuinely-matched PoleModels row that explicitly
        -- has SunboardPower/LightPower = 0 (a real, if unusual, model
        -- record), since the default values themselves (80, 30) are
        -- never 0.
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        -- Exposed as their own output columns (not just used inline
        -- within other expressions) specifically so the Aggregated CTE
        -- downstream can reference them for AvgPanelPercentage's/
        -- AvgLightPercentage's own new conditional-AVG() filters below
        -- -- see that CTE's own comment for the full reasoning. NULL
        -- handling for each mirrors this SAME query's own
        -- IsPanelFaultFlag/IsLedFaultFlag precedent exactly, for
        -- consistency: a NULL IsDaylightForPanelFault falls through
        -- IsPanelFaultFlag's own daylight check the same way "confirmed
        -- past warmup" would, so it's treated as daylight here too; a
        -- NULL IsDaylightForLedFault falls through IsLedFaultFlag's own
        -- daylight check the same way "confirmed dark" would, so it's
        -- treated as night here too -- both are applied via ISNULL() at
        -- the point of use in Aggregated, not baked into these raw
        -- columns themselves.
        t.IsDaylightForPanelFault,
        t.IsDaylightForLedFault,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) AS BatteryElecCurrentTotal,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- Solar-powered lights are SUPPOSED to be off during daylight --
        -- LampPower1+LampPower2=0 while the sun is actually up
        -- (t.IsDaylightForLedFault, computed via real per-day/
        -- per-location sunrise/sunset math in
        -- pole_daylight_flags_loader.py -- NOT a fixed clock window,
        -- which was tried first and had a real, unavoidable flaw:
        -- whichever bucket straddles the actual sunrise/sunset moment
        -- for a given day/location gets misclassified in one direction
        -- or the other) is expected, correct behavior, not a fault.
        -- Only when it's actually dark does zero lamp power indicate a
        -- real problem. See this CASE's own ordering: the daylight
        -- check comes first and unconditionally returns 0, regardless
        -- of LampPower -- only falls through to the actual LampPower
        -- check once it's established this reading is genuinely at
        -- night.
        --
        -- t.IsDaylightForLedFault is DELIBERATELY NOT the same as
        -- IsPanelFaultFlag's own t.IsDaylight below -- it's a more
        -- forgiving definition (true at the exact moment, OR within 1
        -- hour before OR after -- see pole_daylight_flags_loader.py's
        -- own _LED_FAULT_GRACE_PERIOD), confirmed necessary in
        -- practice: a real lamp doesn't always turn on the INSTANT the
        -- sun crosses the sunset threshold (the "before" side of the
        -- grace period), nor does one always turn off exactly at
        -- sunrise -- some lamps sense approaching dawn light and turn
        -- off slightly early (the "after" side). IsPanelFault has no
        -- equivalent lag (solar output genuinely does track the sun
        -- closely), so it keeps using the strict column unmodified --
        -- these two flags need different daylight definitions, which
        -- is exactly why this is two separate PoleTelemetry columns,
        -- not one shared value.
        --
        -- t.IsDaylightForLedFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through to the LampPower check below, same as "confirmed
        -- dark" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- Solar panels only need to charge when BOTH (a) it's been
        -- daylight for at least an hour (t.IsDaylightForPanelFault = 1
        -- -- see pole_daylight_flags_loader.py's own
        -- _PANEL_FAULT_SUNRISE_WARMUP_PERIOD; gives a panel time to
        -- physically warm up right after sunrise) AND (b) the battery
        -- actually needs it -- the TOTAL (not average) of
        -- BatteryElecCurrent1 + BatteryElecCurrent2 is NOT exactly 200.
        -- Replaces an earlier version of this same check (average
        -- BatteryVoltage1/BatteryVoltage2 against a per-model
        -- BatteryChargingMin threshold from PoleModels) by explicit
        -- request -- BatteryChargingMin itself has been removed from
        -- PoleModels entirely (see "sql/PoleModels/Drop
        -- BatteryChargingMin column.sql"), so PoleModels is no longer
        -- involved in this specific check at all (it's still joined
        -- below for SunboardPower/LightPower, used elsewhere in this
        -- same CTE -- just no longer for this). When the total current
        -- IS exactly 200, zero panel output is expected, correct
        -- behavior (nothing left to charge), not a fault, even during
        -- daylight. Only once it's past the sunrise warmup AND this
        -- condition does NOT hold does zero panel output indicate a
        -- real problem. See this CASE's own ordering:
        -- t.IsDaylightForPanelFault = 0 is checked first and
        -- unconditionally returns 0 regardless of anything else -- it's
        -- False both at night (no daylight at all) AND during the first
        -- hour after sunrise (daylight, but not yet past warmup), so
        -- this single check covers both cases without needing a
        -- separate plain-nighttime condition; the total-current check
        -- is checked second and ALSO unconditionally returns 0
        -- regardless of panel output -- only once both of those are
        -- ruled out does this fall through to the actual panel-output
        -- check.
        --
        -- t.IsDaylightForPanelFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through past that first check, same as "confirmed past
        -- warmup" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet. This is the mirror
        -- image of IsLedFaultFlag's own NULL handling above (which
        -- treats unknown as "confirmed dark" instead) -- each flag's
        -- fault condition only applies during the OPPOSITE time of day,
        -- so "unknown" always falls through to that flag's own check,
        -- just via a different comparison (= 1 there, = 0 here).
        --
        -- A NULL BatteryElecCurrent1/BatteryElecCurrent2 (missing
        -- readings) means the total is also NULL, and "NULL = 200" is
        -- UNKNOWN (not TRUE) in T-SQL -- so a missing reading falls
        -- through past this check too, same "unknown is treated as
        -- subject to the normal check" reasoning as the daylight-NULL
        -- case above, not silently exempted from fault detection.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        -- Exposed as an actual output column (not just used inside the
        -- window function's own ORDER BY below) specifically so the
        -- Aggregated CTE downstream can compute MAX(LastUpload) from
        -- it -- _LAST_48_HOURS_MERGE_SQL's own TelemetryWithVitals never
        -- needed this (it anchors PeriodStart/PeriodEnd to the current
        -- moment instead), so this is a genuine addition here, not
        -- copied from that query.
        t.LastUpload,
        -- Identifies each pole's own single most-recent reading in the
        -- window, for IsOpenIssueFault's "take the last telemetry" rule.
        ROW_NUMBER() OVER (PARTITION BY t.LocationId ORDER BY t.LastUpload DESC) AS LatestOverall
    FROM PoleTelemetry t
    JOIN OfflinePolesNeedingRecompute mr ON t.LocationId = mr.LocationId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    -- Bounded to THIS OFFLINE POLE'S OWN last 48 hours of real
    -- activity, ending at its own most recent reading -- NOT the
    -- global cutoff relative to "now" that _LAST_48_HOURS_MERGE_SQL
    -- itself uses (that cutoff is exactly why this pole has no
    -- Last48Hours row to begin with). Deliberately > / <= (not
    -- >= / <) on the lower/upper bounds respectively -- a half-open
    -- interval, so a reading exactly 48 hours before MaxLastUpload
    -- is excluded (a clean, unambiguous 48-hour span) while
    -- MaxLastUpload itself is always included.
    WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)
      AND t.LastUpload <= mr.MaxLastUpload
),
Aggregated AS (
    SELECT
        LocationId,
        -- Carried through so the final MERGE below can anchor THIS
        -- pole's own PeriodStart/PeriodEnd to it, instead of
        -- reading the current moment like _LAST_48_HOURS_MERGE_SQL
        -- itself does.
        MAX(LastUpload) AS MaxLastUpload,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        -- CHANGED by explicit request: only readings taken (a) during
        -- daylight (ISNULL(IsDaylightForPanelFault, 1) = 1 -- NULL
        -- treated as daylight, matching IsPanelFaultFlag's own NULL
        -- handling above) AND (b) where the battery genuinely needs
        -- charging (BatteryElecCurrentTotal <> 200 -- a NULL total
        -- makes this UNKNOWN, not TRUE, so a reading with missing
        -- current data is excluded from this average rather than
        -- assumed to qualify) now contribute to AvgPanelPercentage.
        -- SQL's own AVG() already ignores NULL inputs, so wrapping the
        -- column itself in a CASE that evaluates to NULL for a
        -- non-qualifying reading -- rather than filtering rows out of
        -- the CTE entirely -- correctly excludes it from JUST this one
        -- average while leaving every other aggregate in this same
        -- Aggregated CTE (AvgBatteryPercentage, the fault-flag MAX()es,
        -- RecordCount) computed over the full, unfiltered reading set,
        -- exactly as before.
        AVG(CASE WHEN ISNULL(IsDaylightForPanelFault, 1) = 1 AND BatteryElecCurrentTotal <> 200 THEN PanelPercentage END) AS AvgPanelPercentage,
        -- CHANGED by explicit request: only readings taken at night
        -- (ISNULL(IsDaylightForLedFault, 0) = 0 -- NULL treated as
        -- night, matching IsLedFaultFlag's own NULL handling above) now
        -- contribute to AvgLightPercentage -- same "wrap the column in
        -- a CASE, let AVG() ignore the NULLs" mechanism as
        -- AvgPanelPercentage's own change just above; see that
        -- constant's own comment for the full reasoning.
        AVG(CASE WHEN ISNULL(IsDaylightForLedFault, 0) = 0 THEN LightPercentage END) AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestOverall = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM TelemetryWithVitals
    GROUP BY LocationId
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'LastKnown48Hours' AS PeriodType,
        -- Anchored to THIS OFFLINE POLE'S OWN latest reading
        -- (MaxLastUpload, carried through from Aggregated above) --
        -- the entire reason this variant exists: for an offline
        -- pole, the current moment could be days or weeks past its
        -- actual last activity, which would make PeriodStart/
        -- PeriodEnd describe a window this pole never actually
        -- reported anything in at all. Still converted to Eastern
        -- for the same display-consistency reason as
        -- _LAST_48_HOURS_MERGE_SQL's own PeriodStart/PeriodEnd --
        -- see that constant's own comment for the full reasoning.
        DATEADD(HOUR, -48, MaxLastUpload AT TIME ZONE 'Eastern Standard Time') AS PeriodStart,
        MaxLastUpload AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage,
        IsOnlineAgg AS IsOnline,
        CAST(IsLedFaultAgg AS BIT) AS IsLedFault,
        CAST(IsBatteryFaultAgg AS BIT) AS IsBatteryFault,
        CAST(IsPanelFaultAgg AS BIT) AS IsPanelFault,
        CAST(ISNULL(IsOpenIssueFaultAgg, 0) AS BIT) AS IsOpenIssueFault,
        CAST(
            CASE WHEN IsLedFaultAgg = 1 OR IsBatteryFaultAgg = 1 OR IsPanelFaultAgg = 1
                      OR ISNULL(IsOpenIssueFaultAgg, 0) = 1
                 THEN 1 ELSE 0 END
        AS BIT) AS IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM Aggregated
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
WHEN MATCHED THEN UPDATE SET
    PeriodStart           = source.PeriodStart,
    PeriodEnd             = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# One-off backfill variant of _LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL
# above -- structurally identical (same CandidateOfflinePoles/
# MaxReadingPerCandidatePole CTEs, same TelemetryWithVitals/Aggregated/
# MERGE logic, same fault-flag formulas), but WITHOUT that query's own
# OfflinePolesNeedingRecompute filter -- this recomputes EVERY
# genuinely offline pole's LastKnown48Hours row unconditionally, not
# just the ones whose own telemetry has changed since last time.
#
# Exists specifically because that "skip if unchanged" optimization
# (a real, deliberate performance fix in its own right -- see that
# query's own comment) has an edge case it was never designed to
# handle: it assumes the COMPUTATION LOGIC itself never changes, only
# the underlying DATA does. When IsPanelFaultFlag's own formula changes
# (as happened when the average-BatteryVoltage-vs-BatteryChargingMin
# check was replaced by the total-BatteryElecCurrent-equals-200 check),
# an offline pole's DATA hasn't changed at all, but the RESULT computed
# from that same data has -- and the normal path would silently keep
# every offline pole's LastKnown48Hours row stuck on the OLD formula's
# result forever, since nothing about that pole's own MaxLastUpload
# ever prompts a recompute.
#
# NOT needed for Last48Hours itself, or for LastKnown48Hours on any
# CURRENTLY ACTIVE pole -- both fully recompute from scratch on every
# single loadPoleVitals run regardless of this kind of formula change,
# so the very next scheduled run already reflects any new fault-flag
# logic for those poles with no backfill required at all. This backfill
# exists ONLY for the offline-pole gap described above.
#
# Intended to be run manually, once, as a one-off correction after
# deploying a change to the fault-flag computation logic itself (not
# routinely, and not as part of the normal, scheduled loadLeadsunData
# cycle) -- see scripts/backfill_last_known_48_hours_offline_poles.py
# for how to invoke it.
_BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL = """
SET ANSI_WARNINGS OFF;
;WITH CandidateOfflinePoles AS (
    -- Every LocationId with real telemetry that DOESN'T currently have a
    -- Last48Hours row -- i.e. genuinely offline (no telemetry within the
    -- last 48 hours from now at all), since _LAST_48_HOURS_MERGE_SQL's
    -- own MERGE only ever produces/keeps a row for a pole that DOES.
    -- Unlike the normal, scheduled path (which additionally filters
    -- this candidate set down via OfflinePolesNeedingRecompute, to skip
    -- a pole whose LastKnown48Hours row is already up to date -- see
    -- _LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL's own
    -- comment), THIS backfill deliberately recomputes EVERY genuinely
    -- offline pole unconditionally -- the whole reason this separate
    -- one-off backfill exists: an offline pole's own DATA hasn't
    -- changed, but the FORMULA computing IsPanelFault/IsPoleFault FROM
    -- that data has, so the normal "skip if unchanged" optimization
    -- would otherwise leave every offline pole's LastKnown48Hours row
    -- silently stuck on the OLD formula's result forever.
    --
    -- Batched, with ORDER BY LocationId for a deterministic, repeatable
    -- selection -- BATCHED per explicit request, after a real production
    -- incident: with potentially many months' worth of offline poles
    -- accumulated (this backfill's WHOLE point is that the normal path
    -- never revisits them), doing every single one in ONE query
    -- execution took long enough to hit a TCP-level connection timeout
    -- (SQLSTATE 08S01, WSAETIMEDOUT/10060) partway through -- losing ALL
    -- progress, since nothing had committed yet. Bounding each execution
    -- to a fixed batch size, committed independently (see
    -- backfill_last_known_48_hours_for_offline_poles_after_formula_change()'s
    -- own Python-side loop), keeps any single execution short enough to
    -- avoid that risk, and means a transient failure partway through
    -- only loses the CURRENT batch's own progress, not everything.
    --
    -- The SP_ExecId exclusion below is what makes repeated calls WITHIN
    -- THE SAME RUN make forward progress rather than re-selecting the
    -- exact same first-N poles every single batch: once a pole's
    -- LastKnown48Hours row is written by an EARLIER batch in this same
    -- run, its own SP_ExecId column (already present on every
    -- PoleVitals row, for exactly this kind of audit/tracking purpose)
    -- now matches THIS run's own sp_exec_id, so this batch's own
    -- candidate set naturally excludes it. Deliberately NOT the same
    -- kind of check as OfflinePolesNeedingRecompute's own PeriodEnd
    -- comparison above -- that one can't distinguish "recomputed under
    -- the OLD formula" from "recomputed under the NEW one" (both leave
    -- an identical PeriodEnd), which is exactly the gap this whole
    -- backfill exists to fix; SP_ExecId sidesteps that entirely by
    -- tracking "processed by THIS SPECIFIC RUN", a distinction PeriodEnd
    -- alone can never make.
    SELECT DISTINCT TOP (?) t.LocationId
    FROM PoleTelemetry t
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
      AND NOT EXISTS (
          SELECT 1 FROM PoleVitals pv
          WHERE pv.LocationId = t.LocationId AND pv.PeriodType = 'Last48Hours'
      )
      AND NOT EXISTS (
          SELECT 1 FROM PoleVitals lk
          WHERE lk.LocationId = t.LocationId
            AND lk.PeriodType = 'LastKnown48Hours'
            AND lk.SP_ExecId = ?
      )
    ORDER BY t.LocationId
),
MaxReadingPerCandidatePole AS (
    SELECT
        t.LocationId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    JOIN CandidateOfflinePoles cop ON t.LocationId = cop.LocationId
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
    GROUP BY t.LocationId
),
TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        -- ISNULL(pm.SunboardPower, 80)/ISNULL(pm.LightPower, 30):
        -- a ModelId with no PoleModels match at all (the LEFT JOIN
        -- above produces NULL for both) now defaults to a representative
        -- rated capacity instead of leaving PanelPercentage/
        -- LightPercentage NULL for that reading -- an unmatched model is
        -- treated the same as a
        -- matched one with a sensible default, not "unknown", so it
        -- still contributes to this bucket's AVG() instead of silently
        -- dropping out of it. NULLIF(..., 0) still wraps the OUTSIDE of
        -- that default, unchanged from before -- it now only guards
        -- against a genuinely-matched PoleModels row that explicitly
        -- has SunboardPower/LightPower = 0 (a real, if unusual, model
        -- record), since the default values themselves (80, 30) are
        -- never 0.
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        -- Exposed as their own output columns (not just used inline
        -- within other expressions) specifically so the Aggregated CTE
        -- downstream can reference them for AvgPanelPercentage's/
        -- AvgLightPercentage's own new conditional-AVG() filters below
        -- -- see that CTE's own comment for the full reasoning. NULL
        -- handling for each mirrors this SAME query's own
        -- IsPanelFaultFlag/IsLedFaultFlag precedent exactly, for
        -- consistency: a NULL IsDaylightForPanelFault falls through
        -- IsPanelFaultFlag's own daylight check the same way "confirmed
        -- past warmup" would, so it's treated as daylight here too; a
        -- NULL IsDaylightForLedFault falls through IsLedFaultFlag's own
        -- daylight check the same way "confirmed dark" would, so it's
        -- treated as night here too -- both are applied via ISNULL() at
        -- the point of use in Aggregated, not baked into these raw
        -- columns themselves.
        t.IsDaylightForPanelFault,
        t.IsDaylightForLedFault,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) AS BatteryElecCurrentTotal,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- Solar-powered lights are SUPPOSED to be off during daylight --
        -- LampPower1+LampPower2=0 while the sun is actually up
        -- (t.IsDaylightForLedFault, computed via real per-day/
        -- per-location sunrise/sunset math in
        -- pole_daylight_flags_loader.py -- NOT a fixed clock window,
        -- which was tried first and had a real, unavoidable flaw:
        -- whichever bucket straddles the actual sunrise/sunset moment
        -- for a given day/location gets misclassified in one direction
        -- or the other) is expected, correct behavior, not a fault.
        -- Only when it's actually dark does zero lamp power indicate a
        -- real problem. See this CASE's own ordering: the daylight
        -- check comes first and unconditionally returns 0, regardless
        -- of LampPower -- only falls through to the actual LampPower
        -- check once it's established this reading is genuinely at
        -- night.
        --
        -- t.IsDaylightForLedFault is DELIBERATELY NOT the same as
        -- IsPanelFaultFlag's own t.IsDaylight below -- it's a more
        -- forgiving definition (true at the exact moment, OR within 1
        -- hour before OR after -- see pole_daylight_flags_loader.py's
        -- own _LED_FAULT_GRACE_PERIOD), confirmed necessary in
        -- practice: a real lamp doesn't always turn on the INSTANT the
        -- sun crosses the sunset threshold (the "before" side of the
        -- grace period), nor does one always turn off exactly at
        -- sunrise -- some lamps sense approaching dawn light and turn
        -- off slightly early (the "after" side). IsPanelFault has no
        -- equivalent lag (solar output genuinely does track the sun
        -- closely), so it keeps using the strict column unmodified --
        -- these two flags need different daylight definitions, which
        -- is exactly why this is two separate PoleTelemetry columns,
        -- not one shared value.
        --
        -- t.IsDaylightForLedFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through to the LampPower check below, same as "confirmed
        -- dark" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- Solar panels only need to charge when BOTH (a) it's been
        -- daylight for at least an hour (t.IsDaylightForPanelFault = 1
        -- -- see pole_daylight_flags_loader.py's own
        -- _PANEL_FAULT_SUNRISE_WARMUP_PERIOD; gives a panel time to
        -- physically warm up right after sunrise) AND (b) the battery
        -- actually needs it -- the TOTAL (not average) of
        -- BatteryElecCurrent1 + BatteryElecCurrent2 is NOT exactly 200.
        -- Replaces an earlier version of this same check (average
        -- BatteryVoltage1/BatteryVoltage2 against a per-model
        -- BatteryChargingMin threshold from PoleModels) by explicit
        -- request -- BatteryChargingMin itself has been removed from
        -- PoleModels entirely (see "sql/PoleModels/Drop
        -- BatteryChargingMin column.sql"), so PoleModels is no longer
        -- involved in this specific check at all (it's still joined
        -- below for SunboardPower/LightPower, used elsewhere in this
        -- same CTE -- just no longer for this). When the total current
        -- IS exactly 200, zero panel output is expected, correct
        -- behavior (nothing left to charge), not a fault, even during
        -- daylight. Only once it's past the sunrise warmup AND this
        -- condition does NOT hold does zero panel output indicate a
        -- real problem. See this CASE's own ordering:
        -- t.IsDaylightForPanelFault = 0 is checked first and
        -- unconditionally returns 0 regardless of anything else -- it's
        -- False both at night (no daylight at all) AND during the first
        -- hour after sunrise (daylight, but not yet past warmup), so
        -- this single check covers both cases without needing a
        -- separate plain-nighttime condition; the total-current check
        -- is checked second and ALSO unconditionally returns 0
        -- regardless of panel output -- only once both of those are
        -- ruled out does this fall through to the actual panel-output
        -- check.
        --
        -- t.IsDaylightForPanelFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through past that first check, same as "confirmed past
        -- warmup" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet. This is the mirror
        -- image of IsLedFaultFlag's own NULL handling above (which
        -- treats unknown as "confirmed dark" instead) -- each flag's
        -- fault condition only applies during the OPPOSITE time of day,
        -- so "unknown" always falls through to that flag's own check,
        -- just via a different comparison (= 1 there, = 0 here).
        --
        -- A NULL BatteryElecCurrent1/BatteryElecCurrent2 (missing
        -- readings) means the total is also NULL, and "NULL = 200" is
        -- UNKNOWN (not TRUE) in T-SQL -- so a missing reading falls
        -- through past this check too, same "unknown is treated as
        -- subject to the normal check" reasoning as the daylight-NULL
        -- case above, not silently exempted from fault detection.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        -- Exposed as an actual output column (not just used inside the
        -- window function's own ORDER BY below) specifically so the
        -- Aggregated CTE downstream can compute MAX(LastUpload) from
        -- it -- _LAST_48_HOURS_MERGE_SQL's own TelemetryWithVitals never
        -- needed this (it anchors PeriodStart/PeriodEnd to the current
        -- moment instead), so this is a genuine addition here, not
        -- copied from that query.
        t.LastUpload,
        -- Identifies each pole's own single most-recent reading in the
        -- window, for IsOpenIssueFault's "take the last telemetry" rule.
        ROW_NUMBER() OVER (PARTITION BY t.LocationId ORDER BY t.LastUpload DESC) AS LatestOverall
    FROM PoleTelemetry t
    JOIN MaxReadingPerCandidatePole mr ON t.LocationId = mr.LocationId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    -- Bounded to THIS OFFLINE POLE'S OWN last 48 hours of real
    -- activity, ending at its own most recent reading -- NOT the
    -- global cutoff relative to "now" that _LAST_48_HOURS_MERGE_SQL
    -- itself uses (that cutoff is exactly why this pole has no
    -- Last48Hours row to begin with). Deliberately > / <= (not
    -- >= / <) on the lower/upper bounds respectively -- a half-open
    -- interval, so a reading exactly 48 hours before MaxLastUpload
    -- is excluded (a clean, unambiguous 48-hour span) while
    -- MaxLastUpload itself is always included.
    WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)
      AND t.LastUpload <= mr.MaxLastUpload
),
Aggregated AS (
    SELECT
        LocationId,
        -- Carried through so the final MERGE below can anchor THIS
        -- pole's own PeriodStart/PeriodEnd to it, instead of
        -- reading the current moment like _LAST_48_HOURS_MERGE_SQL
        -- itself does.
        MAX(LastUpload) AS MaxLastUpload,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        -- CHANGED by explicit request: only readings taken (a) during
        -- daylight (ISNULL(IsDaylightForPanelFault, 1) = 1 -- NULL
        -- treated as daylight, matching IsPanelFaultFlag's own NULL
        -- handling above) AND (b) where the battery genuinely needs
        -- charging (BatteryElecCurrentTotal <> 200 -- a NULL total
        -- makes this UNKNOWN, not TRUE, so a reading with missing
        -- current data is excluded from this average rather than
        -- assumed to qualify) now contribute to AvgPanelPercentage.
        -- SQL's own AVG() already ignores NULL inputs, so wrapping the
        -- column itself in a CASE that evaluates to NULL for a
        -- non-qualifying reading -- rather than filtering rows out of
        -- the CTE entirely -- correctly excludes it from JUST this one
        -- average while leaving every other aggregate in this same
        -- Aggregated CTE (AvgBatteryPercentage, the fault-flag MAX()es,
        -- RecordCount) computed over the full, unfiltered reading set,
        -- exactly as before.
        AVG(CASE WHEN ISNULL(IsDaylightForPanelFault, 1) = 1 AND BatteryElecCurrentTotal <> 200 THEN PanelPercentage END) AS AvgPanelPercentage,
        -- CHANGED by explicit request: only readings taken at night
        -- (ISNULL(IsDaylightForLedFault, 0) = 0 -- NULL treated as
        -- night, matching IsLedFaultFlag's own NULL handling above) now
        -- contribute to AvgLightPercentage -- same "wrap the column in
        -- a CASE, let AVG() ignore the NULLs" mechanism as
        -- AvgPanelPercentage's own change just above; see that
        -- constant's own comment for the full reasoning.
        AVG(CASE WHEN ISNULL(IsDaylightForLedFault, 0) = 0 THEN LightPercentage END) AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestOverall = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM TelemetryWithVitals
    GROUP BY LocationId
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'LastKnown48Hours' AS PeriodType,
        -- Anchored to THIS OFFLINE POLE'S OWN latest reading
        -- (MaxLastUpload, carried through from Aggregated above) --
        -- the entire reason this variant exists: for an offline
        -- pole, the current moment could be days or weeks past its
        -- actual last activity, which would make PeriodStart/
        -- PeriodEnd describe a window this pole never actually
        -- reported anything in at all. Still converted to Eastern
        -- for the same display-consistency reason as
        -- _LAST_48_HOURS_MERGE_SQL's own PeriodStart/PeriodEnd --
        -- see that constant's own comment for the full reasoning.
        DATEADD(HOUR, -48, MaxLastUpload AT TIME ZONE 'Eastern Standard Time') AS PeriodStart,
        MaxLastUpload AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage,
        IsOnlineAgg AS IsOnline,
        CAST(IsLedFaultAgg AS BIT) AS IsLedFault,
        CAST(IsBatteryFaultAgg AS BIT) AS IsBatteryFault,
        CAST(IsPanelFaultAgg AS BIT) AS IsPanelFault,
        CAST(ISNULL(IsOpenIssueFaultAgg, 0) AS BIT) AS IsOpenIssueFault,
        CAST(
            CASE WHEN IsLedFaultAgg = 1 OR IsBatteryFaultAgg = 1 OR IsPanelFaultAgg = 1
                      OR ISNULL(IsOpenIssueFaultAgg, 0) = 1
                 THEN 1 ELSE 0 END
        AS BIT) AS IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM Aggregated
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
WHEN MATCHED THEN UPDATE SET
    PeriodStart           = source.PeriodStart,
    PeriodEnd             = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

_HOUR_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        -- ISNULL(pm.SunboardPower, 80)/ISNULL(pm.LightPower, 30):
        -- a ModelId with no PoleModels match at all (the LEFT JOIN
        -- above produces NULL for both) now defaults to a representative
        -- rated capacity instead of leaving PanelPercentage/
        -- LightPercentage NULL for that reading -- an unmatched model is
        -- treated the same as a
        -- matched one with a sensible default, not "unknown", so it
        -- still contributes to this bucket's AVG() instead of silently
        -- dropping out of it. NULLIF(..., 0) still wraps the OUTSIDE of
        -- that default, unchanged from before -- it now only guards
        -- against a genuinely-matched PoleModels row that explicitly
        -- has SunboardPower/LightPower = 0 (a real, if unusual, model
        -- record), since the default values themselves (80, 30) are
        -- never 0.
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- Solar-powered lights are SUPPOSED to be off during daylight --
        -- LampPower1+LampPower2=0 while the sun is actually up
        -- (t.IsDaylightForLedFault, computed via real per-day/
        -- per-location sunrise/sunset math in
        -- pole_daylight_flags_loader.py -- NOT a fixed clock window,
        -- which was tried first and had a real, unavoidable flaw:
        -- whichever bucket straddles the actual sunrise/sunset moment
        -- for a given day/location gets misclassified in one direction
        -- or the other) is expected, correct behavior, not a fault.
        -- Only when it's actually dark does zero lamp power indicate a
        -- real problem. See this CASE's own ordering: the daylight
        -- check comes first and unconditionally returns 0, regardless
        -- of LampPower -- only falls through to the actual LampPower
        -- check once it's established this reading is genuinely at
        -- night.
        --
        -- t.IsDaylightForLedFault is DELIBERATELY NOT the same as
        -- IsPanelFaultFlag's own t.IsDaylight below -- it's a more
        -- forgiving definition (true at the exact moment, OR within 1
        -- hour before OR after -- see pole_daylight_flags_loader.py's
        -- own _LED_FAULT_GRACE_PERIOD), confirmed necessary in
        -- practice: a real lamp doesn't always turn on the INSTANT the
        -- sun crosses the sunset threshold (the "before" side of the
        -- grace period), nor does one always turn off exactly at
        -- sunrise -- some lamps sense approaching dawn light and turn
        -- off slightly early (the "after" side). IsPanelFault has no
        -- equivalent lag (solar output genuinely does track the sun
        -- closely), so it keeps using the strict column unmodified --
        -- these two flags need different daylight definitions, which
        -- is exactly why this is two separate PoleTelemetry columns,
        -- not one shared value.
        --
        -- t.IsDaylightForLedFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through to the LampPower check below, same as "confirmed
        -- dark" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- Solar panels only need to charge when BOTH (a) it's been
        -- daylight for at least an hour (t.IsDaylightForPanelFault = 1
        -- -- see pole_daylight_flags_loader.py's own
        -- _PANEL_FAULT_SUNRISE_WARMUP_PERIOD; gives a panel time to
        -- physically warm up right after sunrise) AND (b) the battery
        -- actually needs it -- the TOTAL (not average) of
        -- BatteryElecCurrent1 + BatteryElecCurrent2 is NOT exactly 200.
        -- Replaces an earlier version of this same check (average
        -- BatteryVoltage1/BatteryVoltage2 against a per-model
        -- BatteryChargingMin threshold from PoleModels) by explicit
        -- request -- BatteryChargingMin itself has been removed from
        -- PoleModels entirely (see "sql/PoleModels/Drop
        -- BatteryChargingMin column.sql"), so PoleModels is no longer
        -- involved in this specific check at all (it's still joined
        -- below for SunboardPower/LightPower, used elsewhere in this
        -- same CTE -- just no longer for this). When the total current
        -- IS exactly 200, zero panel output is expected, correct
        -- behavior (nothing left to charge), not a fault, even during
        -- daylight. Only once it's past the sunrise warmup AND this
        -- condition does NOT hold does zero panel output indicate a
        -- real problem. See this CASE's own ordering:
        -- t.IsDaylightForPanelFault = 0 is checked first and
        -- unconditionally returns 0 regardless of anything else -- it's
        -- False both at night (no daylight at all) AND during the first
        -- hour after sunrise (daylight, but not yet past warmup), so
        -- this single check covers both cases without needing a
        -- separate plain-nighttime condition; the total-current check
        -- is checked second and ALSO unconditionally returns 0
        -- regardless of panel output -- only once both of those are
        -- ruled out does this fall through to the actual panel-output
        -- check.
        --
        -- t.IsDaylightForPanelFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through past that first check, same as "confirmed past
        -- warmup" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet. This is the mirror
        -- image of IsLedFaultFlag's own NULL handling above (which
        -- treats unknown as "confirmed dark" instead) -- each flag's
        -- fault condition only applies during the OPPOSITE time of day,
        -- so "unknown" always falls through to that flag's own check,
        -- just via a different comparison (= 1 there, = 0 here).
        --
        -- A NULL BatteryElecCurrent1/BatteryElecCurrent2 (missing
        -- readings) means the total is also NULL, and "NULL = 200" is
        -- UNKNOWN (not TRUE) in T-SQL -- so a missing reading falls
        -- through past this check too, same "unknown is treated as
        -- subject to the normal check" reasoning as the daylight-NULL
        -- case above, not silently exempted from fault detection.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.LastUpload
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
    WHERE t.LastUpload >= ?
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Bucketed AS (
    SELECT
        LocationId,
        TimeZoneName,
        DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        -- Identifies each bucket's own most-recent reading, for
        -- IsOpenIssueFault's "take the last telemetry" rule below --
        -- NOT used for anything else (the other three fault flags and
        -- IsOnline are ANY-in-window, not last-in-window).
        ROW_NUMBER() OVER (
            PARTITION BY LocationId, DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        LocationId,
        TimeZoneName,
        BucketStart,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestInBucket = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Hour' AS PeriodType,
        BucketStart AT TIME ZONE TimeZoneName AS PeriodStart,
        DATEADD(HOUR, 1, BucketStart) AT TIME ZONE TimeZoneName AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage,
        IsOnlineAgg AS IsOnline,
        CAST(IsLedFaultAgg AS BIT) AS IsLedFault,
        CAST(IsBatteryFaultAgg AS BIT) AS IsBatteryFault,
        CAST(IsPanelFaultAgg AS BIT) AS IsPanelFault,
        CAST(ISNULL(IsOpenIssueFaultAgg, 0) AS BIT) AS IsOpenIssueFault,
        CAST(
            CASE WHEN IsLedFaultAgg = 1 OR IsBatteryFaultAgg = 1 OR IsPanelFaultAgg = 1
                      OR ISNULL(IsOpenIssueFaultAgg, 0) = 1
                 THEN 1 ELSE 0 END
        AS BIT) AS IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM Aggregated
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
   AND target.PeriodStart = source.PeriodStart
WHEN MATCHED THEN UPDATE SET
    PeriodEnd            = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# Last48Hours -- a genuinely different kind of "period" from Hour: a
# single, continuously-updated ROLLING window per pole ("the last 48
# hours as of whenever this loader last ran"), not one of a sequence of
# discrete, non-overlapping historical buckets. There's no per-pole
# "history" of Last48Hours rows the way Hour has 720 of them -- only ever
# one, matching the explicit "only 1 of Last48Hours period" retention
# rule (see load_pole_vitals()'s own docstring).
#
# No PoleTimeZones join, no local-time bucketing at all -- unlike
# Hour, this window is a pure 48-hour DURATION ("however long ago",
# not "the last 2 calendar days" in any particular timezone), and
# DATETIMEOFFSET comparisons are already timezone-aware (comparing actual
# UTC instants), so there's nothing for a timezone conversion to add here.
#
# The MERGE's ON clause matches on LocationId + PeriodType alone --
# deliberately NOT including PeriodStart, unlike Hour. PeriodStart
# shifts forward by definition on every run (it's always "now - 48h"),
# so matching on it would mean this could only ever INSERT a new row,
# never UPDATE the existing one -- exactly the "only 1 row" guarantee
# this needs would be violated without the retention-pruning step Hour
# relies on (which doesn't apply here -- see _RETENTION_LIMITS' own
# comment). Matching on LocationId+PeriodType alone means PeriodStart/
# PeriodEnd are simply overwritten to the fresh window's bounds on every
# run, keeping exactly one row per pole updated in place.
_LAST_48_HOURS_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        -- ISNULL(pm.SunboardPower, 80)/ISNULL(pm.LightPower, 30):
        -- a ModelId with no PoleModels match at all (the LEFT JOIN
        -- above produces NULL for both) now defaults to a representative
        -- rated capacity instead of leaving PanelPercentage/
        -- LightPercentage NULL for that reading -- an unmatched model is
        -- treated the same as a
        -- matched one with a sensible default, not "unknown", so it
        -- still contributes to this bucket's AVG() instead of silently
        -- dropping out of it. NULLIF(..., 0) still wraps the OUTSIDE of
        -- that default, unchanged from before -- it now only guards
        -- against a genuinely-matched PoleModels row that explicitly
        -- has SunboardPower/LightPower = 0 (a real, if unusual, model
        -- record), since the default values themselves (80, 30) are
        -- never 0.
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        -- Exposed as their own output columns (not just used inline
        -- within other expressions) specifically so the Aggregated CTE
        -- downstream can reference them for AvgPanelPercentage's/
        -- AvgLightPercentage's own new conditional-AVG() filters below
        -- -- see that CTE's own comment for the full reasoning. NULL
        -- handling for each mirrors this SAME query's own
        -- IsPanelFaultFlag/IsLedFaultFlag precedent exactly, for
        -- consistency: a NULL IsDaylightForPanelFault falls through
        -- IsPanelFaultFlag's own daylight check the same way "confirmed
        -- past warmup" would, so it's treated as daylight here too; a
        -- NULL IsDaylightForLedFault falls through IsLedFaultFlag's own
        -- daylight check the same way "confirmed dark" would, so it's
        -- treated as night here too -- both are applied via ISNULL() at
        -- the point of use in Aggregated, not baked into these raw
        -- columns themselves.
        t.IsDaylightForPanelFault,
        t.IsDaylightForLedFault,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) AS BatteryElecCurrentTotal,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- Solar-powered lights are SUPPOSED to be off during daylight --
        -- LampPower1+LampPower2=0 while the sun is actually up
        -- (t.IsDaylightForLedFault, computed via real per-day/
        -- per-location sunrise/sunset math in
        -- pole_daylight_flags_loader.py -- NOT a fixed clock window,
        -- which was tried first and had a real, unavoidable flaw:
        -- whichever bucket straddles the actual sunrise/sunset moment
        -- for a given day/location gets misclassified in one direction
        -- or the other) is expected, correct behavior, not a fault.
        -- Only when it's actually dark does zero lamp power indicate a
        -- real problem. See this CASE's own ordering: the daylight
        -- check comes first and unconditionally returns 0, regardless
        -- of LampPower -- only falls through to the actual LampPower
        -- check once it's established this reading is genuinely at
        -- night.
        --
        -- t.IsDaylightForLedFault is DELIBERATELY NOT the same as
        -- IsPanelFaultFlag's own t.IsDaylight below -- it's a more
        -- forgiving definition (true at the exact moment, OR within 1
        -- hour before OR after -- see pole_daylight_flags_loader.py's
        -- own _LED_FAULT_GRACE_PERIOD), confirmed necessary in
        -- practice: a real lamp doesn't always turn on the INSTANT the
        -- sun crosses the sunset threshold (the "before" side of the
        -- grace period), nor does one always turn off exactly at
        -- sunrise -- some lamps sense approaching dawn light and turn
        -- off slightly early (the "after" side). IsPanelFault has no
        -- equivalent lag (solar output genuinely does track the sun
        -- closely), so it keeps using the strict column unmodified --
        -- these two flags need different daylight definitions, which
        -- is exactly why this is two separate PoleTelemetry columns,
        -- not one shared value.
        --
        -- t.IsDaylightForLedFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through to the LampPower check below, same as "confirmed
        -- dark" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- Solar panels only need to charge when BOTH (a) it's been
        -- daylight for at least an hour (t.IsDaylightForPanelFault = 1
        -- -- see pole_daylight_flags_loader.py's own
        -- _PANEL_FAULT_SUNRISE_WARMUP_PERIOD; gives a panel time to
        -- physically warm up right after sunrise) AND (b) the battery
        -- actually needs it -- the TOTAL (not average) of
        -- BatteryElecCurrent1 + BatteryElecCurrent2 is NOT exactly 200.
        -- Replaces an earlier version of this same check (average
        -- BatteryVoltage1/BatteryVoltage2 against a per-model
        -- BatteryChargingMin threshold from PoleModels) by explicit
        -- request -- BatteryChargingMin itself has been removed from
        -- PoleModels entirely (see "sql/PoleModels/Drop
        -- BatteryChargingMin column.sql"), so PoleModels is no longer
        -- involved in this specific check at all (it's still joined
        -- below for SunboardPower/LightPower, used elsewhere in this
        -- same CTE -- just no longer for this). When the total current
        -- IS exactly 200, zero panel output is expected, correct
        -- behavior (nothing left to charge), not a fault, even during
        -- daylight. Only once it's past the sunrise warmup AND this
        -- condition does NOT hold does zero panel output indicate a
        -- real problem. See this CASE's own ordering:
        -- t.IsDaylightForPanelFault = 0 is checked first and
        -- unconditionally returns 0 regardless of anything else -- it's
        -- False both at night (no daylight at all) AND during the first
        -- hour after sunrise (daylight, but not yet past warmup), so
        -- this single check covers both cases without needing a
        -- separate plain-nighttime condition; the total-current check
        -- is checked second and ALSO unconditionally returns 0
        -- regardless of panel output -- only once both of those are
        -- ruled out does this fall through to the actual panel-output
        -- check.
        --
        -- t.IsDaylightForPanelFault NULL (not yet computed by
        -- pole_daylight_flags_loader.py for this specific reading) falls
        -- through past that first check, same as "confirmed past
        -- warmup" -- treating "we don't know yet" as "subject to the
        -- normal check" is the safer default, rather than silently
        -- exempting a reading from fault detection just because its
        -- daylight status hasn't been computed yet. This is the mirror
        -- image of IsLedFaultFlag's own NULL handling above (which
        -- treats unknown as "confirmed dark" instead) -- each flag's
        -- fault condition only applies during the OPPOSITE time of day,
        -- so "unknown" always falls through to that flag's own check,
        -- just via a different comparison (= 1 there, = 0 here).
        --
        -- A NULL BatteryElecCurrent1/BatteryElecCurrent2 (missing
        -- readings) means the total is also NULL, and "NULL = 200" is
        -- UNKNOWN (not TRUE) in T-SQL -- so a missing reading falls
        -- through past this check too, same "unknown is treated as
        -- subject to the normal check" reasoning as the daylight-NULL
        -- case above, not silently exempted from fault detection.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) = 200 THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        -- Identifies each pole's own single most-recent reading in the
        -- window, for IsOpenIssueFault's "take the last telemetry" rule.
        ROW_NUMBER() OVER (PARTITION BY t.LocationId ORDER BY t.LastUpload DESC) AS LatestOverall
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.LastUpload >= ?
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Aggregated AS (
    SELECT
        LocationId,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        -- CHANGED by explicit request: only readings taken (a) during
        -- daylight (ISNULL(IsDaylightForPanelFault, 1) = 1 -- NULL
        -- treated as daylight, matching IsPanelFaultFlag's own NULL
        -- handling above) AND (b) where the battery genuinely needs
        -- charging (BatteryElecCurrentTotal <> 200 -- a NULL total
        -- makes this UNKNOWN, not TRUE, so a reading with missing
        -- current data is excluded from this average rather than
        -- assumed to qualify) now contribute to AvgPanelPercentage.
        -- SQL's own AVG() already ignores NULL inputs, so wrapping the
        -- column itself in a CASE that evaluates to NULL for a
        -- non-qualifying reading -- rather than filtering rows out of
        -- the CTE entirely -- correctly excludes it from JUST this one
        -- average while leaving every other aggregate in this same
        -- Aggregated CTE (AvgBatteryPercentage, the fault-flag MAX()es,
        -- RecordCount) computed over the full, unfiltered reading set,
        -- exactly as before.
        AVG(CASE WHEN ISNULL(IsDaylightForPanelFault, 1) = 1 AND BatteryElecCurrentTotal <> 200 THEN PanelPercentage END) AS AvgPanelPercentage,
        -- CHANGED by explicit request: only readings taken at night
        -- (ISNULL(IsDaylightForLedFault, 0) = 0 -- NULL treated as
        -- night, matching IsLedFaultFlag's own NULL handling above) now
        -- contribute to AvgLightPercentage -- same "wrap the column in
        -- a CASE, let AVG() ignore the NULLs" mechanism as
        -- AvgPanelPercentage's own change just above; see that
        -- constant's own comment for the full reasoning.
        AVG(CASE WHEN ISNULL(IsDaylightForLedFault, 0) = 0 THEN LightPercentage END) AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestOverall = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM TelemetryWithVitals
    GROUP BY LocationId
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Last48Hours' AS PeriodType,
        -- Converted to Eastern -- SYSDATETIMEOFFSET() alone reflects the
        -- SERVER's own time zone (Azure SQL Database runs in UTC
        -- regardless of physical region), which would otherwise show
        -- PeriodStart/PeriodEnd as +00:00 while every other "now"-style
        -- timestamp in this project (e.g. SP_Execution's StartDateTime/
        -- EndDateTime, via shared/datetime_utils.py's now_eastern())
        -- is Eastern -- confusing side-by-side, and inconsistent with
        -- the rest of this codebase for no real reason. Applying AT TIME
        -- ZONE before subtracting 48 hours doesn't change WHICH absolute
        -- instant PeriodStart lands on (DATEADD operates on the
        -- underlying instant, not the display offset, so this is safe
        -- even across a DST transition) -- it only changes how that
        -- same instant is displayed.
        DATEADD(HOUR, -48, SYSDATETIMEOFFSET() AT TIME ZONE 'Eastern Standard Time') AS PeriodStart,
        SYSDATETIMEOFFSET() AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage,
        IsOnlineAgg AS IsOnline,
        CAST(IsLedFaultAgg AS BIT) AS IsLedFault,
        CAST(IsBatteryFaultAgg AS BIT) AS IsBatteryFault,
        CAST(IsPanelFaultAgg AS BIT) AS IsPanelFault,
        CAST(ISNULL(IsOpenIssueFaultAgg, 0) AS BIT) AS IsOpenIssueFault,
        CAST(
            CASE WHEN IsLedFaultAgg = 1 OR IsBatteryFaultAgg = 1 OR IsPanelFaultAgg = 1
                      OR ISNULL(IsOpenIssueFaultAgg, 0) = 1
                 THEN 1 ELSE 0 END
        AS BIT) AS IsPoleFault,
        RecordCount,
        ? AS Source,
        ? AS SP_ExecId
    FROM Aggregated
) AS source
ON target.LocationId = source.LocationId
   AND target.PeriodType = source.PeriodType
WHEN MATCHED THEN UPDATE SET
    PeriodStart           = source.PeriodStart,
    PeriodEnd             = source.PeriodEnd,
    AvgBatteryPercentage  = source.AvgBatteryPercentage,
    AvgPanelPercentage    = source.AvgPanelPercentage,
    AvgLightPercentage    = source.AvgLightPercentage,
    IsOnline              = source.IsOnline,
    IsLedFault            = source.IsLedFault,
    IsBatteryFault        = source.IsBatteryFault,
    IsPanelFault          = source.IsPanelFault,
    IsOpenIssueFault      = source.IsOpenIssueFault,
    IsPoleFault           = source.IsPoleFault,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

_MERGE_SQL_BY_PERIOD_TYPE = {
    "Hour": _HOUR_MERGE_SQL,
    "Last48Hours": _LAST_48_HOURS_MERGE_SQL,
}

# Deletes anything beyond the newest N rows per LocationId, ordered by
# PeriodStart DESC -- run once per period type, right after that period
# type's own MERGE commits. Only Hour is in _RETENTION_LIMITS --
# count-based retention doesn't apply to Last48Hours, which is always
# exactly one row per pole by construction, not a growing history to cap.
# Last48Hours has its own, different cleanup need instead -- see
# _LAST_48_HOURS_STALE_ROW_PRUNE_SQL below.
_RETENTION_PRUNE_SQL = """
;WITH Ranked AS (
    SELECT LocationId, PeriodStart,
           ROW_NUMBER() OVER (PARTITION BY LocationId ORDER BY PeriodStart DESC) AS rn
    FROM PoleVitals
    WHERE PeriodType = ?
)
DELETE pv
FROM PoleVitals pv
JOIN Ranked r ON pv.LocationId = r.LocationId AND pv.PeriodStart = r.PeriodStart
WHERE pv.PeriodType = ? AND r.rn > ?
"""

# Removes any existing Last48Hours row for a pole that no longer has ANY
# telemetry within the current 48-hour window -- without this, a pole
# that goes completely silent (zero readings at all, not even one) keeps
# whatever it last successfully computed FOREVER, since
# _LAST_48_HOURS_MERGE_SQL's own source query only ever includes poles
# that DO still have recent telemetry -- a silent pole simply never
# appears in that source at all, so the MERGE can neither update nor
# remove its existing row. A stale "IsOnline=1" (or whatever it last
# was) for a pole that hasn't reported in days would keep silently
# counting toward getPoleVitals' connectedLights/totalLights, which is
# actively misleading, not just imprecise -- this is a correctness gap,
# not a nice-to-have.
#
# cutoff/sentinel here must be the SAME two values bound into
# _LAST_48_HOURS_MERGE_SQL's own WHERE clause for this same run -- this
# has to describe exactly the same "no recent telemetry" condition the
# MERGE itself used to decide who's IN its source, or this could delete
# (or fail to delete) the wrong set of rows relative to what the MERGE
# just did.
_LAST_48_HOURS_STALE_ROW_PRUNE_SQL = """
DELETE pv
FROM PoleVitals pv
WHERE pv.PeriodType = 'Last48Hours'
  AND NOT EXISTS (
      SELECT 1 FROM PoleTelemetry t
      WHERE t.LocationId = pv.LocationId
        AND t.LastUpload >= ?
        AND t.LastUpload <> ?
  )
"""


def _run_cleanup_for_period_type(cursor, period_type: str, cutoff: str) -> int:
    """
    Runs whatever "remove now-irrelevant rows" step applies to this
    period type, right after its own MERGE succeeds. Hour gets the
    keep-newest-N retention prune; Last48Hours gets its own stale-row
    removal instead (see _LAST_48_HOURS_STALE_ROW_PRUNE_SQL's own
    comment for why retention-by-count doesn't apply to it). A single,
    shared call site for both success paths in load_pole_vitals() (the
    normal one and the benign-01003-warning one) -- rather than
    duplicating this same period-type dispatch logic in both places,
    which had already happened once before this function existed and
    was exactly the kind of near-identical duplication worth collapsing.

    Returns the number of rows removed, for logging.
    """
    retention_limit = _RETENTION_LIMITS.get(period_type)
    if retention_limit is not None:
        cursor.execute(_RETENTION_PRUNE_SQL, period_type, period_type, retention_limit)
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    if period_type == "Last48Hours":
        cursor.execute(
            _LAST_48_HOURS_STALE_ROW_PRUNE_SQL,
            cutoff,
            _MISSING_LAST_UPLOAD_SENTINEL,
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    return 0



def _is_benign_null_aggregate_warning(exc: Exception) -> bool:
    """
    SQLSTATE 01003 ("Warning: Null value is eliminated by an aggregate or
    other SET operation") is SQL Server's informational notice that
    AVG()/etc. skipped over a NULL -- not a real failure. It's the
    designed, expected consequence of this loader's NULLIF-guarded
    PanelPercentage/LightPercentage formulas: a reading whose PoleModels
    row explicitly has SunboardPower/LightPower = 0 is *supposed* to drop
    out of that specific average -- a genuinely unmatched ModelId no
    longer reaches this path at all (ISNULL(pm.SunboardPower, 80)/
    ISNULL(pm.LightPower, 30) give it a default instead), but a real
    PoleModels row explicitly recorded as 0 still hits the same NULLIF
    guard, same as before. pyodbc still raises this as a Python exception
    though (SQLSTATE class "01" is warning, not error, but pyodbc doesn't
    distinguish for the purposes of cursor.execute() raising), so without
    this check a MERGE that actually completed successfully gets logged
    and counted as a failure.
    """
    args = getattr(exc, "args", ())
    return bool(args) and args[0] == "01003"


def _safe_rollback(conn, context: str) -> None:
    """
    Attempts conn.rollback(), but never lets a FAILED rollback itself
    replace/mask whatever original exception the caller is already
    handling. Confirmed in practice as a real production incident: if
    the connection is already broken (e.g. the same class of 08S01
    "Communication link failure" this project has already hit once, in
    load_pole_vitals()'s own top-level except block, before that one got
    its own fresh-connection fix), conn.rollback() can ITSELF raise --
    and since every caller of this helper originally called
    conn.rollback() BEFORE its own logging.error() describing the REAL
    problem, that logging.error() call never ran at all. The original,
    useful error message (which period type, which step, what actually
    failed) was silently lost, replaced by an uninformative
    connection-level exception with no context -- exactly the shape of
    the "n/a" / no-descriptive-message failures this was built to
    prevent going forward.

    A failed rollback here isn't a NEW problem needing its own separate
    handling or its own error-level log entry -- the connection is
    already on its way to being closed in the caller's own top-level
    `finally` block regardless (nothing further to protect by rolling
    back successfully), so this is logged as a warning, not re-raised.
    context is purely for that log line, so it's traceable to which
    caller's own rollback attempt this was.
    """
    try:
        conn.rollback()
    except Exception as rollback_error:
        logging.warning(
            "%s: rollback itself failed (connection likely already broken, same underlying "
            "cause as whatever the ORIGINAL failure being handled was) -- %s",
            context,
            rollback_error,
        )


def load_pole_vitals(backfill: bool = False) -> None:
    """
    Recomputes PoleVitals from PoleTelemetry + PoleModels + PoleTimeZones.
    Each period type is its own MERGE (plus, for Hour, a retention
    prune immediately after) -- no per-row Python loop or staging table
    needed here, unlike the other loaders, since the SQL aggregation
    itself produces a modest number of output rows (bounded by distinct
    LocationIds x a couple of buckets), not thousands of individually-
    bound parameter rows.

    Three period types: Hour, Last48Hours, LastKnown48Hours. ('Day',
    'Week', and 'Month' were all removed entirely by explicit request --
    'Week'/'Month' had already been dropped from active computation
    earlier, leaving only the PeriodType CHECK constraint still
    permitting them; 'Day' was still actively computed until this same
    removal. Existing historical rows with these PeriodType values are
    deliberately left in the table untouched -- only the CHECK
    constraint was tightened and this loader's own computation stopped,
    nothing was deleted.) See this module's own header comment for the
    full fault-flag design (IsLedFault/IsBatteryFault/IsPanelFault/
    IsOpenIssueFault/IsPoleFault) all three now compute, replacing the
    earlier Daylight-based LightStatus classification entirely.
    LastKnown48Hours is handled as its own step (2b), separate from the
    uniform PERIOD_TYPES loop below -- see
    _LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL's own comment for
    why: for a pole currently reporting, it's a direct copy of that same
    pole's just-computed Last48Hours row; for a pole that's gone
    completely silent, it's a fresh rollup of that pole's own last 48
    hours of telemetry it actually has, ending at its own most recent
    reading -- deliberately the one period type that PERSISTS for an
    offline pole rather than disappearing, unlike Last48Hours itself.

    Retention: Hour keeps the newest 720 rows per pole (30 days, no
    gaps) -- this table had no pruning at all before this was
    introduced, so it shrank (once) the first time this ran against an
    existing, unpruned table. Last48Hours doesn't need count-based
    retention (it's structurally always exactly one row per pole -- see
    _LAST_48_HOURS_MERGE_SQL's own comment for why its MERGE is built
    that way), but DOES get its own different cleanup: any existing row
    for a pole that's gone completely silent (no telemetry at all within
    the current 48-hour window) is removed, since the MERGE itself can
    never touch such a pole -- it simply never appears in the MERGE's
    own source query, so without this cleanup its last-known values
    would persist forever, misleadingly counting toward getPoleVitals'
    connectedLights/totalLights long after the pole stopped reporting.
    LastKnown48Hours gets NO equivalent stale-row removal -- persisting
    is the entire point of it existing.

    Commits after EACH period type's MERGE (and its own cleanup step --
    retention prune for Hour, stale-row removal for Last48Hours)
    individually, not once at the end for all of them -- a slow or
    failing period type can no longer roll back an earlier period
    type's already-computed, already-succeeded results. Each period
    type's fate -- commit on success, rollback on genuine failure -- is
    independent of what happens to the others.

    Set backfill=True for a one-off historical recompute covering
    PoleTelemetry's entire 6-month retention window for Hour, instead
    of the small "current + previous bucket" window used on every normal
    run. Ignored for Last48Hours, which always uses the full 48-hour
    window regardless (see _compute_cutoff()'s own docstring).
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()

    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        # 1. Open an SP_Execution row for this run
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "loadPoleVitals",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Recompute each period type, committing immediately after
        # each one succeeds (MERGE, then retention prune if applicable)
        # -- NOT once at the end for all three. See load_pole_vitals()'s
        # own docstring for why this matters in practice, not just in
        # theory.
        upsert_start = time.perf_counter()
        now = _now_eastern()
        for period_type in PERIOD_TYPES:
            merge_sql = _MERGE_SQL_BY_PERIOD_TYPE[period_type]
            cutoff = _compute_cutoff(now, period_type, backfill)
            params = (cutoff, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id)

            try:
                cursor.execute(merge_sql, *params)
                affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

                pruned = _run_cleanup_for_period_type(cursor, period_type, cutoff)

                conn.commit()
                total_success += affected
                logging.info(
                    "loadPoleVitals: %s period recomputed and committed, %d row(s) affected, "
                    "%d stale row(s) pruned (since %s).",
                    period_type,
                    affected,
                    pruned,
                    cutoff,
                )
            except Exception as period_error:
                if _is_benign_null_aggregate_warning(period_error):
                    # SQLSTATE 01003 -- see _is_benign_null_aggregate_warning's
                    # docstring. The MERGE itself completed, so this still
                    # commits (including the retention prune, run the same
                    # way as the success path above) -- only pyodbc's
                    # exception-raising made it look like a failure.
                    affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    pruned = _run_cleanup_for_period_type(cursor, period_type, cutoff)
                    conn.commit()
                    total_success += affected
                    logging.info(
                        "loadPoleVitals: %s period recomputed and committed, %d row(s) affected, "
                        "%d stale row(s) pruned (since %s) -- some reading(s) had a PoleModels row "
                        "explicitly recording zero SunboardPower or LightPower and were excluded "
                        "from that specific average, which is expected, not an error.",
                        period_type,
                        affected,
                        pruned,
                        cutoff,
                    )
                else:
                    # A genuine failure -- explicitly roll back THIS
                    # period type's attempt (rather than leaving it in
                    # whatever ambiguous uncommitted state the failed
                    # statement left behind) before moving on to the
                    # next period type in the same connection/transaction.
                    # _safe_rollback (not conn.rollback() directly) --
                    # see that helper's own docstring for why: a FAILED
                    # rollback here must not prevent the logging.error()
                    # right below from actually running.
                    _safe_rollback(conn, f"loadPoleVitals ({period_type})")
                    total_errors += 1
                    logging.error(
                        "loadPoleVitals: failed to recompute %s period (rolled back, other "
                        "period types unaffected): %s",
                        period_type,
                        period_error,
                    )

        # 2b. LastKnown48Hours -- a pair of statements, run AFTER the
        # loop above so Last48Hours' own MERGE (and its stale-row prune)
        # have already committed for this run. See
        # _LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL's own comment
        # for the full reasoning -- not part of the uniform
        # PERIOD_TYPES loop above since its own definition is
        # conditional (copy vs. fresh-compute), not a single MERGE like
        # the other three period types.
        last_known_48_hours_params = (SOURCE_NAME, sp_exec_id)
        try:
            cursor.execute(_LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL, *last_known_48_hours_params)
            copied = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

            offline_params = (
                _MISSING_LAST_UPLOAD_SENTINEL,
                _MISSING_LAST_UPLOAD_SENTINEL,
                SOURCE_NAME,
                sp_exec_id,
            )
            cursor.execute(_LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL, *offline_params)
            freshly_computed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

            conn.commit()
            total_success += copied + freshly_computed
            logging.info(
                "loadPoleVitals: LastKnown48Hours recomputed and committed -- %d row(s) copied "
                "from currently-active poles' own Last48Hours, %d row(s) freshly computed for "
                "offline poles.",
                copied,
                freshly_computed,
            )
        except Exception as last_known_error:
            if _is_benign_null_aggregate_warning(last_known_error):
                # SQLSTATE 01003 -- see _is_benign_null_aggregate_warning's
                # own docstring. Only the second statement (the fresh
                # compute for offline poles) can actually raise this --
                # the copy statement has no AVG()/aggregate of its own,
                # it just selects Last48Hours' already-computed values
                # verbatim.
                copied = 0  # can't distinguish which statement had already run/committed here
                freshly_computed = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
                total_success += freshly_computed
                logging.info(
                    "loadPoleVitals: LastKnown48Hours recomputed and committed -- %d row(s) "
                    "freshly computed for offline poles (some reading(s) had a PoleModels row "
                    "explicitly recording zero SunboardPower or LightPower and were excluded "
                    "from that specific average, which is expected, not an error).",
                    freshly_computed,
                )
            else:
                # _safe_rollback (not conn.rollback() directly) -- see
                # that helper's own docstring for why: a FAILED rollback
                # here must not prevent the logging.error() right below
                # from actually running. Confirmed in practice as the
                # likely cause of a real production incident: a
                # generic, undescriptive "n/a" failure with NEITHER this
                # step's own success message NOR this specific error
                # message appearing anywhere in the logs -- exactly what
                # happens when conn.rollback() itself fails first
                # (because the connection was already broken, e.g. by
                # the same class of 08S01 failure this project has
                # already hit once elsewhere) and its own exception
                # silently replaces last_known_error before this
                # logging.error() call ever gets a chance to run.
                _safe_rollback(conn, "loadPoleVitals (LastKnown48Hours)")
                total_errors += 1
                logging.error(
                    "loadPoleVitals: failed to recompute LastKnown48Hours (rolled back, other "
                    "period types unaffected): %s",
                    last_known_error,
                )

        logging.info(
            "loadPoleVitals: recompute phase took %.1fs.",
            time.perf_counter() - upsert_start,
        )

        # 3. Close out the SP_Execution row with final counts
        cursor.execute(
            """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = ?,
                IsFinalBatch = 1
            WHERE Id = ?
            """,
            _to_dto_string(_now_eastern()),
            total_success,
            total_errors,
            # len(PERIOD_TYPES) + 1: the main loop's 3 period types, plus
            # the one additional LastKnown48Hours step (2b above) that
            # runs outside that loop -- BatchCount is purely a diagnostic
            # count of "how many distinct recompute steps this run did",
            # not something anything else depends on for correctness.
            len(PERIOD_TYPES) + 1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("loadPoleVitals: run failed: %s", ex)
        if sp_exec_id:
            # Fresh connection for recording the failure -- a real
            # production gap this loader had, unlike every other loader
            # in this project (pole_daylight_flags_loader.py/
            # pole_timezones_loader.py/this same module's own
            # backfill_*_for_all_poles() functions): the exception that
            # got us here might BE a connection-level failure (confirmed
            # in practice -- an 08S01 "Communication link failure"
            # during the new LastKnown48Hours step), in which case
            # reusing the SAME connection/cursor to record it just fails
            # a SECOND time (conn.rollback() above, then this same
            # UPDATE), and that second failure was going completely
            # uncaught -- propagating past this except block entirely
            # and crashing the whole loadLeadsunData timer invocation,
            # with SP_Execution's own row left half-finished (no
            # EndDateTime, no ErrorMessage) instead of recording
            # anything useful about what actually happened.
            try:
                recovery_conn = get_connection()
                recovery_cursor = recovery_conn.cursor()
                try:
                    recovery_cursor.execute(
                        """
                        UPDATE SP_Execution
                        SET EndDateTime = ?, ErrorMessage = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?
                        WHERE Id = ?
                        """,
                        _to_dto_string(_now_eastern()),
                        str(ex),
                        total_success,
                        total_errors,
                        sp_exec_id,
                    )
                    recovery_conn.commit()
                finally:
                    recovery_cursor.close()
                    recovery_conn.close()
            except Exception as recording_error:
                logging.error(
                    "loadPoleVitals: additionally failed to record this run's failure in "
                    "SP_Execution (Id=%s): %s -- that row will be left with EndDateTime still "
                    "NULL. The ORIGINAL failure (%s) is what's actually raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()


def backfill_latest_hour_for_all_poles() -> None:
    """
    One-off operation: ensures EVERY pole has an up-to-date "Hour"
    PoleVitals row reflecting its own most recent known telemetry,
    REGARDLESS of how old that telemetry is -- unlike load_pole_vitals()
    (even with backfill=True), which only ever looks within a GLOBAL
    time window relative to "now". A pole that's gone completely silent
    (its latest reading older than even Last48Hours' own 48-hour window)
    would otherwise never get its "Hour" row touched again, no matter
    how many times the normal, scheduled loader runs -- it simply never
    appears in that run's own global time-cutoff filter.

    See _BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL's own comment for the
    full reasoning and exactly how each pole's own scope is determined
    (its own MAX(LastUpload), converted to ITS OWN local time zone,
    truncated to the start of that local hour).

    A single set-based MERGE covering every pole at once, not a per-pole
    Python loop -- SQL Server resolves each pole's own MAX(LastUpload)
    and its own bucket boundaries together, in one pass.

    Intended to be run manually, as a one-off catch-up (e.g. after
    correcting a batch of poles' data, or after noticing a specific pole
    stuck on stale Hour vitals) -- NOT part of the normal, scheduled
    loadLeadsunData cycle. See
    scripts/backfill_latest_hour_pole_vitals.py for how to invoke it.

    Only touches the "Hour" period type -- Day and Last48Hours are
    unaffected. If a matching need arises for those too, each would need
    its own analogous query (see this function's own module-level SQL
    constant's comment on why this is a full, separate copy rather than
    something parameterizable across period types).
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()

    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        # 1. Open an SP_Execution row for this run
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "backfillLatestHourPoleVitals",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. The single, set-based MERGE covering every pole's own
        # latest-hour bucket at once.
        params = (_MISSING_LAST_UPLOAD_SENTINEL, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id)
        try:
            cursor.execute(_BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL, *params)
            total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
            logging.info(
                "backfillLatestHourPoleVitals: %d pole(s)' latest Hour vitals recomputed and committed.",
                total_success,
            )
        except Exception as merge_error:
            if _is_benign_null_aggregate_warning(merge_error):
                # SQLSTATE 01003 -- see _is_benign_null_aggregate_warning's
                # own docstring. The MERGE itself completed, so this
                # still commits -- only pyodbc's exception-raising made
                # it look like a failure.
                total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
                logging.info(
                    "backfillLatestHourPoleVitals: %d pole(s)' latest Hour vitals recomputed and "
                    "committed -- some reading(s) had a PoleModels row explicitly recording zero "
                    "SunboardPower or LightPower and were excluded from that specific average, "
                    "which is expected, not an error.",
                    total_success,
                )
            else:
                _safe_rollback(conn, "backfillLatestHourPoleVitals")
                total_errors = 1
                logging.error(
                    "backfillLatestHourPoleVitals: MERGE failed (rolled back): %s", merge_error
                )
                raise

        # 3. Close out the SP_Execution row with final counts
        cursor.execute(
            """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = ?,
                IsFinalBatch = 1
            WHERE Id = ?
            """,
            _to_dto_string(_now_eastern()),
            total_success,
            total_errors,
            1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("backfillLatestHourPoleVitals: run failed: %s", ex)
        if sp_exec_id:
            # Fresh connection for recording the failure -- the
            # exception that got us here might BE a connection-level
            # failure (e.g. a genuine "Communication link failure"), in
            # which case reusing the same connection/cursor to record it
            # would just raise a SECOND time, masking the original,
            # more useful error with a less useful one about recording
            # it. Same fix, same reasoning, as
            # pole_daylight_flags_loader.py/pole_timezones_loader.py's
            # own equivalent.
            try:
                recovery_conn = get_connection()
                recovery_cursor = recovery_conn.cursor()
                try:
                    recovery_cursor.execute(
                        """
                        UPDATE SP_Execution
                        SET EndDateTime = ?, ErrorMessage = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?
                        WHERE Id = ?
                        """,
                        _to_dto_string(_now_eastern()),
                        str(ex),
                        total_success,
                        total_errors,
                        sp_exec_id,
                    )
                    recovery_conn.commit()
                finally:
                    recovery_cursor.close()
                    recovery_conn.close()
            except Exception as recording_error:
                logging.error(
                    "backfillLatestHourPoleVitals: additionally failed to record this run's "
                    "failure in SP_Execution (Id=%s): %s -- that row will be left with "
                    "EndDateTime still NULL. The ORIGINAL failure (%s) is what's actually "
                    "raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()


def backfill_last_48_hours_of_hour_for_all_poles() -> None:
    """
    One-off operation: ensures EVERY pole has up to 48 hourly "Hour"
    PoleVitals rows -- every hour that has telemetry within a 48-hour
    window ending at THAT POLE'S OWN latest reading, REGARDLESS of how
    old that reading is.

    A broader relative of backfill_latest_hour_for_all_poles() above,
    not a replacement for it: that one only ever touches a pole's single
    newest hour. This one exists for a specific, related need:
    pole_vitals_api.py's GetPoleVitalsByPeriod now anchors its own
    48-hour display window to each pole's latest telemetry too (see
    _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE there) -- but that only shows
    something useful for an offline pole if PoleVitals rows genuinely
    exist across that pole's own last 48 hours of activity in the first
    place. A pole that went offline before this project's Hour-vitals
    logic existed, or whose Hour rows from that window were never
    successfully computed for some other reason, needs this fuller
    backfill.

    See _BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL's own comment
    for the full reasoning and exactly how each pole's own scope is
    determined (its own MAX(LastUpload), then every reading within 48
    hours before that moment, each bucketed into its own local hour --
    same bucketing logic as the normal, scheduled _HOUR_MERGE_SQL, just
    scoped per-pole instead of by a global cutoff relative to "now").

    A single set-based MERGE covering every pole's entire 48-hour window
    at once, not a per-pole Python loop -- SQL Server resolves each
    pole's own MAX(LastUpload), its own 48-hour range, and every bucket
    within it together, in one pass.

    Intended to be run manually, as a one-off catch-up (e.g. after
    correcting a batch of poles' data, after deploying the
    GetPoleVitalsByPeriod anchor change, or after noticing a specific
    offline pole showing an incomplete history) -- NOT part of the
    normal, scheduled loadLeadsunData cycle. See
    scripts/backfill_last_48_hours_hour_pole_vitals.py for how to invoke
    it.

    Only touches the "Hour" period type -- Day and Last48Hours are
    unaffected, same as backfill_latest_hour_for_all_poles() above.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()

    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        # 1. Open an SP_Execution row for this run
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "backfillLast48HoursOfHourPoleVitals",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. The single, set-based MERGE covering every pole's entire
        # 48-hour window at once.
        params = (_MISSING_LAST_UPLOAD_SENTINEL, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id)
        try:
            cursor.execute(_BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL, *params)
            total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
            logging.info(
                "backfillLast48HoursOfHourPoleVitals: %d Hour vitals row(s) recomputed and committed "
                "across every pole's own last 48 hours of activity.",
                total_success,
            )
        except Exception as merge_error:
            if _is_benign_null_aggregate_warning(merge_error):
                # SQLSTATE 01003 -- see _is_benign_null_aggregate_warning's
                # own docstring. The MERGE itself completed, so this
                # still commits -- only pyodbc's exception-raising made
                # it look like a failure.
                total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
                logging.info(
                    "backfillLast48HoursOfHourPoleVitals: %d Hour vitals row(s) recomputed and "
                    "committed -- some reading(s) had a PoleModels row explicitly recording zero "
                    "SunboardPower or LightPower and were excluded from that specific average, "
                    "which is expected, not an error.",
                    total_success,
                )
            else:
                _safe_rollback(conn, "backfillLast48HoursOfHourPoleVitals")
                total_errors = 1
                logging.error(
                    "backfillLast48HoursOfHourPoleVitals: MERGE failed (rolled back): %s", merge_error
                )
                raise

        # 3. Close out the SP_Execution row with final counts
        cursor.execute(
            """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = ?,
                IsFinalBatch = 1
            WHERE Id = ?
            """,
            _to_dto_string(_now_eastern()),
            total_success,
            total_errors,
            1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("backfillLast48HoursOfHourPoleVitals: run failed: %s", ex)
        if sp_exec_id:
            # Fresh connection for recording the failure -- same fix,
            # same reasoning, as backfill_latest_hour_for_all_poles()'s
            # own equivalent above.
            try:
                recovery_conn = get_connection()
                recovery_cursor = recovery_conn.cursor()
                try:
                    recovery_cursor.execute(
                        """
                        UPDATE SP_Execution
                        SET EndDateTime = ?, ErrorMessage = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?
                        WHERE Id = ?
                        """,
                        _to_dto_string(_now_eastern()),
                        str(ex),
                        total_success,
                        total_errors,
                        sp_exec_id,
                    )
                    recovery_conn.commit()
                finally:
                    recovery_cursor.close()
                    recovery_conn.close()
            except Exception as recording_error:
                logging.error(
                    "backfillLast48HoursOfHourPoleVitals: additionally failed to record this run's "
                    "failure in SP_Execution (Id=%s): %s -- that row will be left with "
                    "EndDateTime still NULL. The ORIGINAL failure (%s) is what's actually "
                    "raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()


def backfill_last_known_48_hours_for_offline_poles_after_formula_change(
    batch_size: int = 500,
) -> None:
    """
    One-off operation: force-recomputes EVERY genuinely offline pole's
    LastKnown48Hours row, bypassing the normal "skip if this pole's own
    data hasn't changed" optimization -- see
    _BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL's own
    comment for the full reasoning behind why that normal optimization
    needs bypassing here specifically (in short: it correctly assumes a
    pole's own DATA not changing means nothing needs recomputing, but
    that assumption breaks the one time the computation LOGIC itself
    changes instead).

    Genuinely GENERIC, not tied to any one specific formula change --
    this always runs whatever the CURRENT
    TelemetryWithVitals/Aggregated logic in
    _BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
    happens to be at the time it's run, so the SAME function (and the
    same script invoking it) is the right one to re-run after ANY future
    change to how Last48Hours/LastKnown48Hours compute their own fault
    flags or averages -- first used when IsPanelFaultFlag's own formula
    was replaced, used again when AvgPanelPercentage/AvgLightPercentage
    became conditional on daylight/night-time -- no code changes needed
    here for either.

    NOT needed for Last48Hours, or for LastKnown48Hours on any CURRENTLY
    ACTIVE pole -- both fully recompute from scratch on every single
    loadPoleVitals run regardless, so the very next scheduled run
    already reflects any new computation logic for those poles with no
    action needed here at all. Run this ONLY to fix already-offline
    poles' existing LastKnown48Hours rows, which the normal scheduled
    path will otherwise never revisit.

    BATCHED per explicit request, after a real production incident: with
    potentially many months' worth of offline poles accumulated (this
    backfill's whole point is that the normal path never revisits them),
    a single query execution covering every one of them at once took
    long enough to hit a TCP-level connection timeout partway through
    (SQLSTATE 08S01, "TCP Provider: Error code 0x274C (10060)" --
    WSAETIMEDOUT, a genuine network-level timeout, not a client-side
    pyodbc query-timeout setting) -- losing ALL progress, since nothing
    had committed yet. Now loops, calling
    _BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL
    repeatedly with a bounded batch_size each time, committing after
    EVERY batch, until a batch affects zero rows (meaning every
    genuinely offline pole has been processed). See that SQL constant's
    own comment for exactly how a batch avoids re-selecting the same
    poles a previous batch in this SAME run already handled (its own
    SP_ExecId exclusion -- not the PeriodEnd-based check the normal,
    unbatched, scheduled path uses, which can't tell "recomputed under
    the OLD formula" apart from "recomputed under the NEW one"). If a
    later batch fails (e.g. another transient network error), every
    EARLIER batch's own progress is preserved regardless, since each one
    already committed independently -- simply re-running this same
    function again picks up wherever it left off... well, not quite:
    since this is a brand NEW run with its own, different SP_ExecId, a
    re-run actually starts over and reprocesses every offline pole from
    scratch again -- harmless (this operation is idempotent; recomputing
    an already-correct row just produces the same, correct result again)
    but not truly resumable across separate invocations. Each
    INDIVIDUAL batch is now small enough to complete well within a
    typical connection's timeout window, which is the actual guarantee
    that matters here, not perfect cross-run resumability.

    batch_size defaults to 500 poles per execution -- large enough to
    make meaningful progress per round trip, small enough that even a
    slow, distant network connection should comfortably complete one
    batch's own full 48-hour-per-pole aggregation within typical
    connection/network timeout windows. Override only if you have a
    specific reason to (e.g. a particularly unreliable connection
    warranting an even smaller batch, or a confirmed-stable one where a
    larger batch would finish the whole backfill in fewer round trips).

    Intended to be run manually, once, right after deploying a change to
    Last48Hours/LastKnown48Hours' own computation logic -- NOT part of
    the normal, scheduled loadLeadsunData cycle, and not something that
    needs running again afterward for THAT SAME change (the normal
    "skip if unchanged" path is correct and sufficient once this one-off
    catch-up has run) -- only re-run it again the next time the
    computation logic itself changes once more. See
    scripts/backfill_last_known_48_hours_offline_poles.py for how to
    invoke it.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()

    sp_exec_id = None
    total_success = 0
    total_errors = 0
    batch_count = 0
    # A generous, defensive upper bound -- not expected to ever actually
    # be reached in practice (it would take 5,000,000 offline poles at
    # the default batch_size to get there), just a guard against an
    # unforeseen bug causing an infinite loop rather than a natural,
    # zero-rowcount termination.
    max_batches = 10000

    try:
        # 1. Open an SP_Execution row for this run
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "backfillLastKnown48HoursOfflinePolesAfterFormulaChange",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Loop, each iteration a single, bounded-size MERGE covering
        # up to batch_size genuinely offline poles this run hasn't
        # already processed -- committed independently, so a later
        # batch's own failure never undoes an earlier one's progress.
        while batch_count < max_batches:
            batch_count += 1
            params = (
                batch_size,
                _MISSING_LAST_UPLOAD_SENTINEL,
                sp_exec_id,
                _MISSING_LAST_UPLOAD_SENTINEL,
                SOURCE_NAME,
                sp_exec_id,
            )
            try:
                cursor.execute(
                    _BACKFILL_LAST_KNOWN_48_HOURS_FORCE_RECOMPUTE_OFFLINE_POLES_SQL, *params
                )
                batch_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
            except Exception as merge_error:
                if _is_benign_null_aggregate_warning(merge_error):
                    # SQLSTATE 01003 -- see _is_benign_null_aggregate_warning's
                    # own docstring. The MERGE itself completed, so this
                    # batch still commits -- only pyodbc's
                    # exception-raising made it look like a failure.
                    batch_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    conn.commit()
                    logging.info(
                        "backfillLastKnown48HoursOfflinePolesAfterFormulaChange: batch %d: "
                        "%d row(s) force-recomputed and committed -- some reading(s) had a "
                        "PoleModels row explicitly recording zero SunboardPower or LightPower "
                        "and were excluded from that specific average, which is expected, not "
                        "an error.",
                        batch_count,
                        batch_success,
                    )
                else:
                    _safe_rollback(conn, "backfillLastKnown48HoursOfflinePolesAfterFormulaChange")
                    total_errors += 1
                    logging.error(
                        "backfillLastKnown48HoursOfflinePolesAfterFormulaChange: batch %d "
                        "failed (rolled back), %d row(s) from EARLIER batches already "
                        "committed and unaffected by this failure: %s",
                        batch_count,
                        total_success,
                        merge_error,
                    )
                    raise

            total_success += batch_success
            logging.info(
                "backfillLastKnown48HoursOfflinePolesAfterFormulaChange: batch %d: %d "
                "offline pole(s)' LastKnown48Hours row(s) force-recomputed and committed "
                "under the current formula (%d total so far).",
                batch_count,
                batch_success,
                total_success,
            )

            if batch_success == 0:
                # Nothing left to process -- every genuinely offline
                # pole has now been recomputed under the current formula.
                break
        else:
            logging.warning(
                "backfillLastKnown48HoursOfflinePolesAfterFormulaChange: stopped after "
                "reaching the defensive max_batches=%d limit, not because there was nothing "
                "left to process -- this almost certainly indicates a bug (e.g. the SP_ExecId "
                "exclusion not actually preventing a batch from re-selecting poles an earlier "
                "batch in this same run already handled) rather than a genuinely enormous "
                "offline-pole backlog. %d row(s) recomputed so far.",
                max_batches,
                total_success,
            )

        # 3. Close out the SP_Execution row with final, accumulated counts
        cursor.execute(
            """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = ?,
                IsFinalBatch = 1
            WHERE Id = ?
            """,
            _to_dto_string(_now_eastern()),
            total_success,
            total_errors,
            batch_count,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error(
            "backfillLastKnown48HoursOfflinePolesAfterFormulaChange: run failed: %s", ex
        )
        if sp_exec_id:
            # Fresh connection for recording the failure -- same fix,
            # same reasoning, as this module's other backfill functions.
            try:
                recovery_conn = get_connection()
                recovery_cursor = recovery_conn.cursor()
                try:
                    recovery_cursor.execute(
                        """
                        UPDATE SP_Execution
                        SET EndDateTime = ?, ErrorMessage = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?, BatchCount = ?
                        WHERE Id = ?
                        """,
                        _to_dto_string(_now_eastern()),
                        str(ex),
                        total_success,
                        total_errors,
                        batch_count,
                        sp_exec_id,
                    )
                    recovery_conn.commit()
                finally:
                    recovery_cursor.close()
                    recovery_conn.close()
            except Exception as recording_error:
                logging.error(
                    "backfillLastKnown48HoursOfflinePolesAfterFormulaChange: additionally failed "
                    "to record this run's failure in SP_Execution (Id=%s): %s -- that row will be "
                    "left with EndDateTime still NULL. The ORIGINAL failure (%s) is what's "
                    "actually raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()
