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
# classification entirely -- IsDaylight/LightStatus no longer exist
# anywhere in this schema; see the README for that history).
#
# Four independent fault signals, computed per PoleTelemetry reading:
#   IsLedFault      = (LampPower1 + LampPower2) = 0
#   IsBatteryFault  = (BatteryElecCurrent1 + BatteryElecCurrent2) / 2 < 10
#   IsPanelFault    = (SolarBoardVoltage * SolarBoardElecCurrent) = 0
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

_HOUR_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1 ELSE 0 END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1 ELSE 0 END AS IsPanelFaultFlag,
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
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage,
        t.IsOnline,
        CASE WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1 ELSE 0 END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1 ELSE 0 END AS IsPanelFaultFlag,
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
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1 ELSE 0 END AS IsLedFaultFlag,
        CASE WHEN (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1 ELSE 0 END AS IsPanelFaultFlag,
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
        DATEADD(HOUR, -48, SYSDATETIMEOFFSET()) AS PeriodStart,
        SYSDATETIMEOFFSET() AS PeriodEnd,
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
# type's own MERGE commits. Only Hour/Day are in _RETENTION_LIMITS (see
# that dict's own comment for why Last48Hours doesn't need this at all).
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


def _is_benign_null_aggregate_warning(exc: Exception) -> bool:
    """
    SQLSTATE 01003 ("Warning: Null value is eliminated by an aggregate or
    other SET operation") is SQL Server's informational notice that
    AVG()/etc. skipped over a NULL -- not a real failure. It's the
    designed, expected consequence of this loader's NULLIF-guarded
    PanelPercentage/LightPercentage formulas: a reading with a missing
    model, or zero SunboardPower/LightPower, is *supposed* to drop out of
    that specific average. pyodbc still raises it as a Python exception
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
    unpruned table. Last48Hours needs no pruning -- it's structurally
    always exactly one row per pole (see _LAST_48_HOURS_MERGE_SQL's own
    comment for why its MERGE is built that way).

    Commits after EACH period type's MERGE (and, for Hour/Day, its
    retention prune) individually, not once at the end for all of them
    -- a slow or failing period type can no longer roll back an earlier
    period type's already-computed, already-succeeded results. Each
    period type's fate -- commit on success, rollback on genuine failure
    -- is independent of what happens to the others.

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

                retention_limit = _RETENTION_LIMITS.get(period_type)
                pruned = 0
                if retention_limit is not None:
                    cursor.execute(_RETENTION_PRUNE_SQL, period_type, period_type, retention_limit)
                    pruned = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

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
                    retention_limit = _RETENTION_LIMITS.get(period_type)
                    pruned = 0
                    if retention_limit is not None:
                        cursor.execute(_RETENTION_PRUNE_SQL, period_type, period_type, retention_limit)
                        pruned = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    conn.commit()
                    total_success += affected
                    logging.info(
                        "loadPoleVitals: %s period recomputed and committed, %d row(s) affected, "
                        "%d stale row(s) pruned (since %s) -- some reading(s) had a missing/zero "
                        "SunboardPower or LightPower and were excluded from that specific average, "
                        "which is expected, not an error.",
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
