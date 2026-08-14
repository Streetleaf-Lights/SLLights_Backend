import os
import logging
import time
from datetime import timedelta

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.pole_telemetry_loader import _MISSING_LAST_UPLOAD_SENTINEL

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

PERIOD_TYPES = ("Hour", "Day", "Last48Hours")

# How many rows to KEEP per LocationId for each period type -- this table
# had no retention/pruning at all before this; it grew one row per pole
# per Hour (or Day) forever. Hour/Day are genuinely historical, discrete
# buckets, so pruning means "delete anything beyond the newest N,
# ORDER BY PeriodStart DESC" (see _RETENTION_PRUNE_SQL below).
# Last48Hours isn't in this dict at all -- it's a single, continuously
# upserted row per pole (its own MERGE matches on LocationId+PeriodType
# alone, not PeriodStart -- see _LAST_48_HOURS_MERGE_SQL's own comment),
# so there's structurally never more than one row per pole to prune.
_RETENTION_LIMITS = {
    "Hour": 168,
    "Day": 7,
}

# How far back each period type recomputes on a normal (non-backfill) run.
# For Hour/Day, wide enough to cover "the current bucket + the previous
# bucket" (so late-arriving telemetry near a boundary still lands in the
# right bucket) without rescanning PoleTelemetry's full 6-month retention
# window every 10 minutes -- the same round-trip-count trap already hit
# (and fixed) for Poles/PoleTelemetry itself. Bounded by
# IX_PoleTelemetry_LastUpload_Covering.
#
# Last48Hours is different in kind, not just degree: it's not an
# incremental "current + previous bucket" window at all -- every run
# recomputes the ENTIRE rolling 48-hour window fresh (there's no
# "previous Last48Hours bucket" the way Hour/Day have previous buckets),
# so its own lookback IS the full window, always exactly 48 hours,
# regardless of when this loader last ran.
_DEFAULT_LOOKBACK = {
    "Hour": timedelta(hours=3),
    "Day": timedelta(days=2),
    "Last48Hours": timedelta(hours=48),
}

# Wide enough to cover PoleTelemetry's entire 6-month retention window --
# for a one-off historical backfill via load_pole_vitals(backfill=True).
# Doesn't apply to Last48Hours at all (see above -- there's no
# "backfill history" concept for a single rolling-window row; backfill=True
# only widens Hour/Day's lookback).
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
#                      of day), AND EXCEPT while the average of
#                      BatteryVoltage1/BatteryVoltage2 is already >=
#                      PoleModels' BatteryChargingMin for that pole's
#                      ModelId, defaulting to 13.5 if that ModelId has no
#                      PoleModels match at all (a fully (or sufficiently)
#                      charged battery has nothing left to charge, so
#                      zero panel output is expected there too, even
#                      during daylight).
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
# Bucket-level aggregation (Hour/Day/Last48Hours all share this same
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
# Hour/Last48Hours use "was ANY reading in the window online"; Day uses
# the same but restricted to the last 6 hours OF THAT BUCKET'S OWN END
# (not "now", and not the whole day) -- see _DAY_MERGE_SQL's own comment
# for why that narrower window exists.
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
# look like it should: it widens Hour/Day's lookback to
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
# convention of Hour/Day/Last48Hours each carrying their own full copy
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
        -- LightPercentage NULL for that reading -- same reasoning, same
        -- shape, as IsPanelFaultFlag's own ISNULL(pm.BatteryChargingMin,
        -- 13.5) below: an unmatched model is treated the same as a
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
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
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
        -- LightPercentage NULL for that reading -- same reasoning, same
        -- shape, as IsPanelFaultFlag's own ISNULL(pm.BatteryChargingMin,
        -- 13.5) below: an unmatched model is treated the same as a
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
        -- actually needs it -- the average of BatteryVoltage1/
        -- BatteryVoltage2 is below pm.BatteryChargingMin (a per-model
        -- threshold, currently a fixed 13.5 for every model -- see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). Once the
        -- battery is already at or above that threshold, zero panel
        -- output is expected, correct behavior (nothing left to
        -- charge), not a fault, even during daylight. Only once it's
        -- past the sunrise warmup AND the battery genuinely needs
        -- charging does zero panel output indicate a real problem. See
        -- this CASE's own ordering: t.IsDaylightForPanelFault = 0 is
        -- checked first and unconditionally returns 0 regardless of
        -- anything else -- it's False both at night (no daylight at
        -- all) AND during the first hour after sunrise (daylight, but
        -- not yet past warmup), so this single check covers both cases
        -- without needing a separate plain-nighttime condition;
        -- "battery already charged enough" is checked second and ALSO
        -- unconditionally returns 0 regardless of panel output -- only
        -- once both of those are ruled out does this fall through to
        -- the actual panel-output check.
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
        -- A NULL average BatteryVoltage (missing readings) still falls
        -- through past the battery-already-charged check -- "unknown
        -- whether the battery needs charging" is treated as "assume it
        -- might", not silently exempted, since NULL >= anything is
        -- still UNKNOWN in T-SQL regardless of what the threshold
        -- itself is.
        --
        -- A ModelId with no PoleModels match AT ALL (the LEFT JOIN
        -- below produces a NULL pm.BatteryChargingMin) is DIFFERENT --
        -- rather than falling through the same way, ISNULL() below
        -- defaults the threshold itself to 13.5, the same value every
        -- model in PoleModels currently has anyway (see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). An
        -- unmatched model is deliberately treated the same as a
        -- matched one with today's default value, not as "unknown,
        -- assume it might still need charging" regardless of how
        -- charged the battery actually is.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
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
        -- LightPercentage NULL for that reading -- same reasoning, same
        -- shape, as IsPanelFaultFlag's own ISNULL(pm.BatteryChargingMin,
        -- 13.5) below: an unmatched model is treated the same as a
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
        -- actually needs it -- the average of BatteryVoltage1/
        -- BatteryVoltage2 is below pm.BatteryChargingMin (a per-model
        -- threshold, currently a fixed 13.5 for every model -- see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). Once the
        -- battery is already at or above that threshold, zero panel
        -- output is expected, correct behavior (nothing left to
        -- charge), not a fault, even during daylight. Only once it's
        -- past the sunrise warmup AND the battery genuinely needs
        -- charging does zero panel output indicate a real problem. See
        -- this CASE's own ordering: t.IsDaylightForPanelFault = 0 is
        -- checked first and unconditionally returns 0 regardless of
        -- anything else -- it's False both at night (no daylight at
        -- all) AND during the first hour after sunrise (daylight, but
        -- not yet past warmup), so this single check covers both cases
        -- without needing a separate plain-nighttime condition;
        -- "battery already charged enough" is checked second and ALSO
        -- unconditionally returns 0 regardless of panel output -- only
        -- once both of those are ruled out does this fall through to
        -- the actual panel-output check.
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
        -- A NULL average BatteryVoltage (missing readings) still falls
        -- through past the battery-already-charged check -- "unknown
        -- whether the battery needs charging" is treated as "assume it
        -- might", not silently exempted, since NULL >= anything is
        -- still UNKNOWN in T-SQL regardless of what the threshold
        -- itself is.
        --
        -- A ModelId with no PoleModels match AT ALL (the LEFT JOIN
        -- below produces a NULL pm.BatteryChargingMin) is DIFFERENT --
        -- rather than falling through the same way, ISNULL() below
        -- defaults the threshold itself to 13.5, the same value every
        -- model in PoleModels currently has anyway (see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). An
        -- unmatched model is deliberately treated the same as a
        -- matched one with today's default value, not as "unknown,
        -- assume it might still need charging" regardless of how
        -- charged the battery actually is.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
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

_DAY_MERGE_SQL = """
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
        -- LightPercentage NULL for that reading -- same reasoning, same
        -- shape, as IsPanelFaultFlag's own ISNULL(pm.BatteryChargingMin,
        -- 13.5) below: an unmatched model is treated the same as a
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
        t.IsOnline,
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
        -- actually needs it -- the average of BatteryVoltage1/
        -- BatteryVoltage2 is below pm.BatteryChargingMin (a per-model
        -- threshold, currently a fixed 13.5 for every model -- see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). Once the
        -- battery is already at or above that threshold, zero panel
        -- output is expected, correct behavior (nothing left to
        -- charge), not a fault, even during daylight. Only once it's
        -- past the sunrise warmup AND the battery genuinely needs
        -- charging does zero panel output indicate a real problem. See
        -- this CASE's own ordering: t.IsDaylightForPanelFault = 0 is
        -- checked first and unconditionally returns 0 regardless of
        -- anything else -- it's False both at night (no daylight at
        -- all) AND during the first hour after sunrise (daylight, but
        -- not yet past warmup), so this single check covers both cases
        -- without needing a separate plain-nighttime condition;
        -- "battery already charged enough" is checked second and ALSO
        -- unconditionally returns 0 regardless of panel output -- only
        -- once both of those are ruled out does this fall through to
        -- the actual panel-output check.
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
        -- A NULL average BatteryVoltage (missing readings) still falls
        -- through past the battery-already-charged check -- "unknown
        -- whether the battery needs charging" is treated as "assume it
        -- might", not silently exempted, since NULL >= anything is
        -- still UNKNOWN in T-SQL regardless of what the threshold
        -- itself is.
        --
        -- A ModelId with no PoleModels match AT ALL (the LEFT JOIN
        -- below produces a NULL pm.BatteryChargingMin) is DIFFERENT --
        -- rather than falling through the same way, ISNULL() below
        -- defaults the threshold itself to 13.5, the same value every
        -- model in PoleModels currently has anyway (see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). An
        -- unmatched model is deliberately treated the same as a
        -- matched one with today's default value, not as "unknown,
        -- assume it might still need charging" regardless of how
        -- charged the battery actually is.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
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
        CAST(LocalTime AS DATE) AS BucketStart,
        IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        BatteryPercentage, PanelPercentage, LightPercentage,
        -- "Last 6 hours" is relative to THIS bucket's own end (the day's
        -- own midnight), not to when this query happens to run -- a
        -- historical Day recomputed later always checks the same 18:00-
        -- midnight window of that specific day. Unlike the fault flags
        -- below, ONLY IsOnline uses this narrower window -- an explicit
        -- requirement, not carried over by accident.
        CASE
            WHEN LocalTime >= DATEADD(HOUR, -6, DATEADD(DAY, 1, CAST(CAST(LocalTime AS DATE) AS DATETIME2(3))))
                 AND IsOnline = 1
            THEN 1 ELSE 0
        END AS IsOnlineRecentFlag,
        ROW_NUMBER() OVER (
            PARTITION BY LocationId, CAST(LocalTime AS DATE)
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
        MAX(IsOnlineRecentFlag) AS IsOnlineAgg,
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
        'Day' AS PeriodType,
        CAST(BucketStart AS DATETIME2(3)) AT TIME ZONE TimeZoneName AS PeriodStart,
        CAST(DATEADD(DAY, 1, BucketStart) AS DATETIME2(3)) AT TIME ZONE TimeZoneName AS PeriodEnd,
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

# Last48Hours -- a genuinely different kind of "period" from Hour/Day: a
# single, continuously-updated ROLLING window per pole ("the last 48
# hours as of whenever this loader last ran"), not one of a sequence of
# discrete, non-overlapping historical buckets. There's no per-pole
# "history" of Last48Hours rows the way Hour has 168 of them -- only ever
# one, matching the explicit "only 1 of Last48Hours period" retention
# rule (see load_pole_vitals()'s own docstring).
#
# No PoleTimeZones join, no local-time bucketing at all -- unlike
# Hour/Day, this window is a pure 48-hour DURATION ("however long ago",
# not "the last 2 calendar days" in any particular timezone), and
# DATETIMEOFFSET comparisons are already timezone-aware (comparing actual
# UTC instants), so there's nothing for a timezone conversion to add here.
#
# The MERGE's ON clause matches on LocationId + PeriodType alone --
# deliberately NOT including PeriodStart, unlike Hour/Day. PeriodStart
# shifts forward by definition on every run (it's always "now - 48h"),
# so matching on it would mean this could only ever INSERT a new row,
# never UPDATE the existing one -- exactly the "only 1 row" guarantee
# this needs would be violated without the retention-pruning step Hour/
# Day rely on (which doesn't apply here -- see _RETENTION_LIMITS' own
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
        -- LightPercentage NULL for that reading -- same reasoning, same
        -- shape, as IsPanelFaultFlag's own ISNULL(pm.BatteryChargingMin,
        -- 13.5) below: an unmatched model is treated the same as a
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
        -- actually needs it -- the average of BatteryVoltage1/
        -- BatteryVoltage2 is below pm.BatteryChargingMin (a per-model
        -- threshold, currently a fixed 13.5 for every model -- see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). Once the
        -- battery is already at or above that threshold, zero panel
        -- output is expected, correct behavior (nothing left to
        -- charge), not a fault, even during daylight. Only once it's
        -- past the sunrise warmup AND the battery genuinely needs
        -- charging does zero panel output indicate a real problem. See
        -- this CASE's own ordering: t.IsDaylightForPanelFault = 0 is
        -- checked first and unconditionally returns 0 regardless of
        -- anything else -- it's False both at night (no daylight at
        -- all) AND during the first hour after sunrise (daylight, but
        -- not yet past warmup), so this single check covers both cases
        -- without needing a separate plain-nighttime condition;
        -- "battery already charged enough" is checked second and ALSO
        -- unconditionally returns 0 regardless of panel output -- only
        -- once both of those are ruled out does this fall through to
        -- the actual panel-output check.
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
        -- A NULL average BatteryVoltage (missing readings) still falls
        -- through past the battery-already-charged check -- "unknown
        -- whether the battery needs charging" is treated as "assume it
        -- might", not silently exempted, since NULL >= anything is
        -- still UNKNOWN in T-SQL regardless of what the threshold
        -- itself is.
        --
        -- A ModelId with no PoleModels match AT ALL (the LEFT JOIN
        -- below produces a NULL pm.BatteryChargingMin) is DIFFERENT --
        -- rather than falling through the same way, ISNULL() below
        -- defaults the threshold itself to 13.5, the same value every
        -- model in PoleModels currently has anyway (see
        -- "sql/PoleModels/Add BatteryChargingMin column.sql"). An
        -- unmatched model is deliberately treated the same as a
        -- matched one with today's default value, not as "unknown,
        -- assume it might still need charging" regardless of how
        -- charged the battery actually is.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
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
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
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
    "Day": _DAY_MERGE_SQL,
    "Last48Hours": _LAST_48_HOURS_MERGE_SQL,
}

# Deletes anything beyond the newest N rows per LocationId, ordered by
# PeriodStart DESC -- run once per period type, right after that period
# type's own MERGE commits. Only Hour/Day are in _RETENTION_LIMITS --
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
    period type, right after its own MERGE succeeds. Hour/Day get the
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


def load_pole_vitals(backfill: bool = False) -> None:
    """
    Recomputes PoleVitals from PoleTelemetry + PoleModels + PoleTimeZones.
    Each period type is its own MERGE (plus, for Hour/Day, a retention
    prune immediately after) -- no per-row Python loop or staging table
    needed here, unlike the other loaders, since the SQL aggregation
    itself produces a modest number of output rows (bounded by distinct
    LocationIds x a couple of buckets), not thousands of individually-
    bound parameter rows.

    Three period types: Hour, Day, Last48Hours. See this module's own
    header comment for the full fault-flag design (IsLedFault/
    IsBatteryFault/IsPanelFault/IsOpenIssueFault/IsPoleFault) all three
    now compute, replacing the earlier Daylight-based LightStatus
    classification entirely.

    Retention: Hour keeps the newest 168 rows per pole, Day keeps 7 --
    this table had no pruning at all before this change, so it will
    shrink (once) the first time this runs against an existing,
    unpruned table. Last48Hours doesn't need count-based retention (it's
    structurally always exactly one row per pole -- see
    _LAST_48_HOURS_MERGE_SQL's own comment for why its MERGE is built
    that way), but DOES get its own different cleanup: any existing row
    for a pole that's gone completely silent (no telemetry at all within
    the current 48-hour window) is removed, since the MERGE itself can
    never touch such a pole -- it simply never appears in the MERGE's
    own source query, so without this cleanup its last-known values
    would persist forever, misleadingly counting toward getPoleVitals'
    connectedLights/totalLights long after the pole stopped reporting.

    Commits after EACH period type's MERGE (and its own cleanup step --
    retention prune for Hour/Day, stale-row removal for Last48Hours)
    individually, not once at the end for all of them -- a slow or
    failing period type can no longer roll back an earlier period
    type's already-computed, already-succeeded results. Each period
    type's fate -- commit on success, rollback on genuine failure -- is
    independent of what happens to the others.

    Set backfill=True for a one-off historical recompute covering
    PoleTelemetry's entire 6-month retention window for Hour/Day, instead
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
                    conn.rollback()
                    total_errors += 1
                    logging.error(
                        "loadPoleVitals: failed to recompute %s period (rolled back, other "
                        "period types unaffected): %s",
                        period_type,
                        period_error,
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
            len(PERIOD_TYPES),
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("loadPoleVitals: run failed: %s", ex)
        if sp_exec_id:
            cursor.execute(
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
            conn.commit()
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
                conn.rollback()
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
                conn.rollback()
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
