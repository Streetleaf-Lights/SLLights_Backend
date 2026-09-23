import os
import logging
import time
from datetime import timedelta

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.pole_telemetry_loader import _MISSING_LAST_UPLOAD_SENTINEL

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"
PROVISIONED_SOURCE_NAME = "Provisioned"

PERIOD_TYPES = ("Hour", "Last48Hours")

# Number of Hour rows to keep per PoleId (30 days of hourly buckets).
# Last48Hours is excluded -- it's always exactly one row per pole.
_RETENTION_LIMITS = {"Hour": 720}

# Lookback windows for normal scheduled runs.
# Hour: covers the current bucket plus the previous one so late-arriving
#   telemetry near a boundary still lands in the right bucket.
# Last48Hours: always the full 48-hour rolling window, every run.
_DEFAULT_LOOKBACK = {
    "Hour": timedelta(hours=3),
    "Last48Hours": timedelta(hours=48),
}

# Wide enough to cover the entire PoleTelemetry retention window --
# used only for load_*_pole_vitals(backfill=True) on the Hour period.
_BACKFILL_LOOKBACK = timedelta(days=400)


def _compute_cutoff(now, period_type: str, backfill: bool):
    """
    Returns the DTO-formatted cutoff string for WHERE t.LastUpload >= ?.
    For Hour: 3-hour rolling window normally, 400-day window on backfill.
    For Last48Hours: always 48 hours regardless of backfill flag.
    """
    if backfill and period_type == "Hour":
        lookback = _BACKFILL_LOOKBACK
    else:
        lookback = _DEFAULT_LOOKBACK[period_type]
    return _to_dto_string(now - lookback)


# ---------------------------------------------------------------------------
# Leadsun vitals SQL
# ---------------------------------------------------------------------------
#
# Fault-flag aggregation rules (shared by all Leadsun MERGEs):
#   IsLedFault/IsBatteryFault/IsPanelFault: MAX over the window -- any
#     single faulted reading marks the whole bucket faulted.
#   IsOpenIssueFault: single most-recent reading's value (ROW_NUMBER +
#     MAX(CASE WHEN rn=1...)) -- it's a current fact about the pole from
#     PoleOpenIssues, not something to aggregate across a window.
#   IsPoleFault: OR of all four.
#   IsOnline: MAX over the window ("was any reading online").
#
# IsLedFaultFlag uses IsDaylightForLedFault (±1h grace period around
# sunrise/sunset). IsPanelFaultFlag uses IsDaylightForPanelFault (must
# have been daylight for 1h already AND daylight for 1h more). These
# are separate columns in PoleTelemetry because each fault flag needs a
# different daylight definition tuned to its hardware's response lag.
# NULL daylight flags: treated as "unknown = subject to normal check"
# (same as confirmed-night for LED, confirmed-past-warmup for panel).

# ---------------------------------------------------------------------------
# _BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL
# ---------------------------------------------------------------------------
# Ensures every pole has an up-to-date Hour row for its own most-recent
# telemetry bucket -- even poles that have gone silent and fall outside
# the normal 3-hour rolling window. Each pole's window floats to its own
# MAX(LastUpload) independently; no global time cutoff parameter.
_BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH MaxReadingPerPole AS (
    SELECT
        t.PoleId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    WHERE t.Source = 'Leadsun'
      AND t.LastUpload <> ?
    GROUP BY t.PoleId
),
LatestBucketPerPole AS (
    SELECT
        mr.PoleId,
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
    LEFT JOIN PoleTimeZones ptz ON mr.PoleId = ptz.VendorPoleId
),
TelemetryWithVitals AS (
    SELECT
        t.PoleId,
        lb.TimeZoneName,
        lb.BucketStart,
        CASE WHEN t.BatteryElecCurrent2 IS NULL
             THEN t.BatteryElecCurrent1
             ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0
        END AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1
             ELSE t.LampPower1 + t.LampPower2
        END / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1 ELSE t.LampPower1 + t.LampPower2 END) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 END) < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE t.BatteryElecCurrent1 + t.BatteryElecCurrent2 END) = (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN 100 ELSE 200 END) THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.LastUpload
    FROM PoleTelemetry t
    JOIN LatestBucketPerPole lb ON t.PoleId = lb.PoleId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.Source = 'Leadsun'
      AND t.LastUpload <> ?
      AND CAST(t.LastUpload AT TIME ZONE lb.TimeZoneName AS DATETIME2(3)) >= lb.BucketStart
      AND CAST(t.LastUpload AT TIME ZONE lb.TimeZoneName AS DATETIME2(3)) < DATEADD(HOUR, 1, lb.BucketStart)
),
Bucketed AS (
    SELECT
        PoleId,
        TimeZoneName,
        BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        ROW_NUMBER() OVER (
            PARTITION BY PoleId, BucketStart
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        PoleId,
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
    GROUP BY PoleId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        PoleId,
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
ON target.PoleId = source.PoleId
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
    INSERT (PoleId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.PoleId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# ---------------------------------------------------------------------------
# _BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL
# ---------------------------------------------------------------------------
# Covers each pole's own last 48 hours of activity, with each pole's
# window ending at its own MaxLastUpload independently. Used by the
# backfill script rather than the normal scheduled loader.
_BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH MaxReadingPerPole AS (
    SELECT
        t.PoleId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    WHERE t.Source = 'Leadsun'
      AND t.LastUpload <> ?
    GROUP BY t.PoleId
),
TelemetryWithVitals AS (
    SELECT
        t.PoleId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        CASE WHEN t.BatteryElecCurrent2 IS NULL
             THEN t.BatteryElecCurrent1
             ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0
        END AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1
             ELSE t.LampPower1 + t.LampPower2
        END / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1 ELSE t.LampPower1 + t.LampPower2 END) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 END) < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE t.BatteryElecCurrent1 + t.BatteryElecCurrent2 END) = (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN 100 ELSE 200 END) THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.LastUpload
    FROM PoleTelemetry t
    JOIN MaxReadingPerPole mr ON t.PoleId = mr.PoleId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    LEFT JOIN PoleTimeZones ptz ON t.PoleId = ptz.VendorPoleId
    WHERE t.Source = 'Leadsun'
      AND t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)
      AND t.LastUpload <= mr.MaxLastUpload
      AND t.LastUpload <> ?
),
Bucketed AS (
    SELECT
        PoleId,
        TimeZoneName,
        DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        ROW_NUMBER() OVER (
            PARTITION BY PoleId, DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        PoleId,
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
    GROUP BY PoleId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        PoleId,
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
ON target.PoleId = source.PoleId
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
    INSERT (PoleId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.PoleId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# ---------------------------------------------------------------------------
# _HOUR_MERGE_SQL — normal scheduled Leadsun Hour recompute
# ---------------------------------------------------------------------------
# Joins PoleTimeZones on VendorPoleId (Leadsun poles).
# PanelPercentage/LightPercentage default to 80W/30W rated capacity when
# ModelId has no matching PoleModels row, so unmatched poles still
# contribute to averages rather than silently dropping out.
_HOUR_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.PoleId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        CASE WHEN t.BatteryElecCurrent2 IS NULL
             THEN t.BatteryElecCurrent1
             ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0
        END AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1
             ELSE t.LampPower1 + t.LampPower2
        END / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        -- Lights are off during daylight (IsDaylightForLedFault uses ±1h
        -- grace period around sunrise/sunset for lamp response lag).
        -- NULL IsDaylightForLedFault falls through to the lamp check.
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1 ELSE t.LampPower1 + t.LampPower2 END) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 END) < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        -- Panel fault only applies when IsDaylightForPanelFault=1 (has been
        -- daylight ≥1h AND daylight for ≥1h more) AND battery not full.
        -- NULL IsDaylightForPanelFault falls through to the panel check.
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE t.BatteryElecCurrent1 + t.BatteryElecCurrent2 END) = (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN 100 ELSE 200 END) THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.LastUpload
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    LEFT JOIN PoleTimeZones ptz ON t.PoleId = ptz.VendorPoleId
    WHERE t.Source = 'Leadsun'
      AND t.LastUpload >= ?
      AND t.LastUpload <> ?
),
Bucketed AS (
    SELECT
        PoleId,
        TimeZoneName,
        DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        ROW_NUMBER() OVER (
            PARTITION BY PoleId, DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        PoleId,
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
    GROUP BY PoleId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        PoleId,
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
ON target.PoleId = source.PoleId
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
    INSERT (PoleId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.PoleId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# ---------------------------------------------------------------------------
# _LAST_48_HOURS_MERGE_SQL — rolling 48-hour window, one row per pole
# ---------------------------------------------------------------------------
# No PoleTimeZones join (no timezone bucketing needed -- 48h is a pure
# duration comparison on UTC instants). MERGE key is PoleId+PeriodType
# only (not PeriodStart) so PeriodStart/PeriodEnd are overwritten in
# place each run, keeping exactly one row per pole.
#
# AvgPanelPercentage: only daylight readings where battery isn't full.
# AvgLightPercentage: only nighttime readings.
_LAST_48_HOURS_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.PoleId,
        CASE WHEN t.BatteryElecCurrent2 IS NULL
             THEN t.BatteryElecCurrent1
             ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0
        END AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(ISNULL(pm.SunboardPower, 80), 0) * 100.0 AS PanelPercentage,
        CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1
             ELSE t.LampPower1 + t.LampPower2
        END / NULLIF(ISNULL(pm.LightPower, 30), 0) * 100.0 AS LightPercentage,
        t.IsDaylightForPanelFault,
        t.IsDaylightForLedFault,
        CASE WHEN t.BatteryElecCurrent2 IS NULL
             THEN t.BatteryElecCurrent1
             ELSE t.BatteryElecCurrent1 + t.BatteryElecCurrent2
        END AS BatteryElecCurrentTotal,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (CASE WHEN t.LampPower2 IS NULL THEN t.LampPower1 ELSE t.LampPower1 + t.LampPower2 END) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 END) < 10 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN t.BatteryElecCurrent1 ELSE t.BatteryElecCurrent1 + t.BatteryElecCurrent2 END) = (CASE WHEN t.BatteryElecCurrent2 IS NULL THEN 100 ELSE 200 END) THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        ROW_NUMBER() OVER (PARTITION BY t.PoleId ORDER BY t.LastUpload DESC) AS LatestOverall
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.Source = 'Leadsun'
      AND t.LastUpload >= ?
      AND t.LastUpload <> ?
),
Aggregated AS (
    SELECT
        PoleId,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(CASE WHEN ISNULL(IsDaylightForPanelFault, 1) = 1 AND BatteryElecCurrentTotal <> 200 THEN PanelPercentage END) AS AvgPanelPercentage,
        AVG(CASE WHEN ISNULL(IsDaylightForLedFault, 0) = 0 THEN LightPercentage END) AS AvgLightPercentage,
        MAX(IsOnlineFlag)       AS IsOnlineAgg,
        MAX(IsLedFaultFlag)     AS IsLedFaultAgg,
        MAX(IsBatteryFaultFlag) AS IsBatteryFaultAgg,
        MAX(IsPanelFaultFlag)   AS IsPanelFaultAgg,
        MAX(CASE WHEN LatestOverall = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
        COUNT(*)                AS RecordCount
    FROM TelemetryWithVitals
    GROUP BY PoleId
)
MERGE PoleVitals AS target
USING (
    SELECT
        PoleId,
        'Last48Hours' AS PeriodType,
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
ON target.PoleId = source.PoleId
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
    INSERT (PoleId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.PoleId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

_MERGE_SQL_BY_PERIOD_TYPE = {
    "Hour": _HOUR_MERGE_SQL,
    "Last48Hours": _LAST_48_HOURS_MERGE_SQL,
}

_RETENTION_PRUNE_SQL = """
;WITH Ranked AS (
    SELECT PoleId, PeriodStart,
           ROW_NUMBER() OVER (PARTITION BY PoleId ORDER BY PeriodStart DESC) AS rn
    FROM PoleVitals
    WHERE PeriodType = ?
)
DELETE pv
FROM PoleVitals pv
JOIN Ranked r ON pv.PoleId = r.PoleId AND pv.PeriodStart = r.PeriodStart
WHERE pv.PeriodType = ? AND r.rn > ?
"""

# Removes stale Last48Hours rows for poles that have no telemetry in the
# current 48-hour window. Without this, a silent pole's last-known values
# would persist forever, misleadingly counting toward connected-lights totals.
_LAST_48_HOURS_STALE_ROW_PRUNE_SQL = """
DELETE pv
FROM PoleVitals pv
WHERE pv.PeriodType = 'Last48Hours'
  AND NOT EXISTS (
      SELECT 1 FROM PoleTelemetry t
      WHERE t.PoleId = pv.PoleId
        AND t.LastUpload >= ?
        AND t.LastUpload <> ?
  )
"""


def _run_cleanup_for_period_type(cursor, period_type: str, cutoff: str) -> int:
    """Runs the appropriate cleanup after each period type's MERGE commits.
    Hour: count-based retention prune (keep newest 720).
    Last48Hours: remove rows for poles with no recent telemetry.
    Returns number of rows removed."""
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
    """Returns True for SQLSTATE 01003 -- SQL Server's informational warning
    that AVG() skipped a NULL (expected when NULLIF guards produce NULLs for
    PoleModels rows with SunboardPower/LightPower = 0). pyodbc raises this as
    a Python exception even though the MERGE completed successfully."""
    args = getattr(exc, "args", ())
    return bool(args) and args[0] == "01003"


def _safe_rollback(conn, context: str) -> None:
    """Attempts rollback without letting a failed rollback mask the original
    exception. A broken connection can raise on rollback itself; this logs
    the rollback failure as a warning and continues."""
    try:
        conn.rollback()
    except Exception as rollback_error:
        logging.warning(
            "%s: rollback itself failed (connection likely broken): %s",
            context,
            rollback_error,
        )


def _open_sp_execution(cursor, name: str, source: str) -> int:
    """Inserts an SP_Execution row and returns its Id."""
    cursor.execute(
        """
        INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
        OUTPUT INSERTED.Id
        VALUES (?, ?, ?, ?, 0, 0)
        """,
        name,
        ENVIRONMENT,
        _to_dto_string(_now_eastern()),
        source,
    )
    return cursor.fetchone()[0]


def _close_sp_execution(cursor, sp_exec_id: int, success: int, errors: int, batches: int) -> None:
    """Updates the SP_Execution row with final counts."""
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
        success,
        errors,
        batches,
        sp_exec_id,
    )


def _record_sp_execution_failure(sp_exec_id: int, ex: Exception, success: int, errors: int) -> None:
    """Records a run failure in SP_Execution using a fresh connection, so a
    broken main connection doesn't prevent the error from being logged."""
    try:
        recovery_conn = get_connection()
        recovery_cursor = recovery_conn.cursor()
        try:
            recovery_cursor.execute(
                """
                UPDATE SP_Execution
                SET EndDateTime = ?, ErrorMessage = ?,
                    TotalSuccessfulRecords = ?, TotalErrorRecords = ?
                WHERE Id = ?
                """,
                _to_dto_string(_now_eastern()),
                str(ex),
                success,
                errors,
                sp_exec_id,
            )
            recovery_conn.commit()
        finally:
            recovery_cursor.close()
            recovery_conn.close()
    except Exception as recording_error:
        logging.error(
            "SP_Execution Id=%s: additionally failed to record run failure: %s "
            "(original error was: %s)",
            sp_exec_id,
            recording_error,
            ex,
        )


def _run_vitals_for_source(
    sp_name: str,
    source_name: str,
    merge_sql_by_period_type: dict,
    backfill: bool,
) -> None:
    """
    Shared implementation for both load_leadsun_pole_vitals() and
    load_provisioned_pole_vitals(). Runs each period type's MERGE in order,
    commits + runs cleanup after each, and records the run in SP_Execution.

    Commits after each period type independently -- a slow or failing period
    type cannot roll back an earlier one's already-committed results.
    """
    conn = get_connection()
    cursor = conn.cursor()
    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        sp_exec_id = _open_sp_execution(cursor, sp_name, source_name)
        conn.commit()

        upsert_start = time.perf_counter()
        now = _now_eastern()
        for period_type in PERIOD_TYPES:
            merge_sql = merge_sql_by_period_type[period_type]
            cutoff = _compute_cutoff(now, period_type, backfill)
            params = (cutoff, _MISSING_LAST_UPLOAD_SENTINEL, source_name, sp_exec_id)

            try:
                cursor.execute(merge_sql, *params)
                affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                pruned = _run_cleanup_for_period_type(cursor, period_type, cutoff)
                conn.commit()
                total_success += affected
                logging.info(
                    "%s: %s period committed, %d row(s) affected, %d pruned (since %s).",
                    sp_name, period_type, affected, pruned, cutoff,
                )
            except Exception as period_error:
                if _is_benign_null_aggregate_warning(period_error):
                    affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    pruned = _run_cleanup_for_period_type(cursor, period_type, cutoff)
                    conn.commit()
                    total_success += affected
                    logging.info(
                        "%s: %s period committed, %d row(s) affected, %d pruned (since %s) "
                        "-- null aggregate warning (expected, not an error).",
                        sp_name, period_type, affected, pruned, cutoff,
                    )
                else:
                    _safe_rollback(conn, f"{sp_name} ({period_type})")
                    total_errors += 1
                    logging.error(
                        "%s: %s period failed (rolled back, other periods unaffected): %s",
                        sp_name, period_type, period_error,
                    )

        logging.info("%s: recompute phase took %.1fs.", sp_name, time.perf_counter() - upsert_start)

        _close_sp_execution(cursor, sp_exec_id, total_success, total_errors, len(PERIOD_TYPES))
        conn.commit()

    except Exception as ex:
        logging.error("%s: run failed: %s", sp_name, ex)
        if sp_exec_id:
            _record_sp_execution_failure(sp_exec_id, ex, total_success, total_errors)
        raise
    finally:
        cursor.close()
        conn.close()


def load_leadsun_pole_vitals(backfill: bool = False) -> None:
    """
    Recomputes PoleVitals from PoleTelemetry + PoleModels + PoleTimeZones
    for all Leadsun poles. Runs Hour and Last48Hours period types in order,
    committing and cleaning up after each independently.

    Set backfill=True to widen the Hour lookback to 400 days (the full
    PoleTelemetry retention window) instead of the normal 3-hour window.
    Has no effect on Last48Hours, which always uses a 48-hour window.
    """
    _run_vitals_for_source("loadPoleVitals", SOURCE_NAME, _MERGE_SQL_BY_PERIOD_TYPE, backfill)


def backfill_latest_hour_for_all_poles() -> None:
    """
    Ensures every Leadsun pole has an up-to-date Hour PoleVitals row for
    its own most-recent telemetry bucket, even poles that have gone silent
    and fall outside the normal 3-hour rolling window.

    Run manually after correcting stale pole data. Only touches Hour.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()
    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        sp_exec_id = _open_sp_execution(cursor, "backfillLatestHourPoleVitals", SOURCE_NAME)
        conn.commit()

        params = (_MISSING_LAST_UPLOAD_SENTINEL, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id)
        try:
            cursor.execute(_BACKFILL_LATEST_HOUR_PER_POLE_MERGE_SQL, *params)
            total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
            logging.info(
                "backfillLatestHourPoleVitals: %d pole(s)' latest Hour vitals recomputed.",
                total_success,
            )
        except Exception as merge_error:
            if _is_benign_null_aggregate_warning(merge_error):
                total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
                logging.info(
                    "backfillLatestHourPoleVitals: %d pole(s)' latest Hour vitals recomputed "
                    "-- null aggregate warning (expected, not an error).",
                    total_success,
                )
            else:
                _safe_rollback(conn, "backfillLatestHourPoleVitals")
                total_errors = 1
                logging.error("backfillLatestHourPoleVitals: MERGE failed: %s", merge_error)
                raise

        _close_sp_execution(cursor, sp_exec_id, total_success, total_errors, 1)
        conn.commit()

    except Exception as ex:
        logging.error("backfillLatestHourPoleVitals: run failed: %s", ex)
        if sp_exec_id:
            _record_sp_execution_failure(sp_exec_id, ex, total_success, total_errors)
        raise
    finally:
        cursor.close()
        conn.close()


def backfill_last_48_hours_of_hour_for_all_poles() -> None:
    """
    Recomputes up to 48 hourly Hour rows per Leadsun pole, each pole using
    its own last 48 hours of activity ending at its own MaxLastUpload.

    Run after formula changes to correct the recent hourly history for all
    poles at once. Only touches Hour.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()
    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        sp_exec_id = _open_sp_execution(cursor, "backfillLast48HoursOfHourPoleVitals", SOURCE_NAME)
        conn.commit()

        params = (_MISSING_LAST_UPLOAD_SENTINEL, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id)
        try:
            cursor.execute(_BACKFILL_LAST_48_HOURS_OF_HOUR_PER_POLE_MERGE_SQL, *params)
            total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            conn.commit()
            logging.info(
                "backfillLast48HoursOfHourPoleVitals: %d Hour vitals row(s) recomputed.",
                total_success,
            )
        except Exception as merge_error:
            if _is_benign_null_aggregate_warning(merge_error):
                total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
                logging.info(
                    "backfillLast48HoursOfHourPoleVitals: %d Hour vitals row(s) recomputed "
                    "-- null aggregate warning (expected, not an error).",
                    total_success,
                )
            else:
                _safe_rollback(conn, "backfillLast48HoursOfHourPoleVitals")
                total_errors = 1
                logging.error(
                    "backfillLast48HoursOfHourPoleVitals: MERGE failed: %s", merge_error
                )
                raise

        _close_sp_execution(cursor, sp_exec_id, total_success, total_errors, 1)
        conn.commit()

    except Exception as ex:
        logging.error("backfillLast48HoursOfHourPoleVitals: run failed: %s", ex)
        if sp_exec_id:
            _record_sp_execution_failure(sp_exec_id, ex, total_success, total_errors)
        raise
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Provisioned-pole vitals SQL
# ---------------------------------------------------------------------------
#
# Uses device-reported columns (BatterySoC, LightRatio, PanelPercentage,
# BatteryFault, LEDFault, ControllerFault) instead of derived expressions
# from raw current/power readings -- the device pre-computes these.
#
# Hour MERGE difference from Leadsun: joins PoleTimeZones on ProvisionedPoleId
# (not VendorPoleId) since provisioned rows use ProvisionedPoleId as their
# PoleTelemetry.PoleId. No PoleModels join needed (no SunboardPower/LightPower).
#
# Last48Hours MERGE: written as a standalone string (not generated from
# _LAST_48_HOURS_MERGE_SQL via .replace()) -- easier to read and maintain.

PROVISIONED_SOURCE_NAME = "Provisioned"

_PROVISIONED_HOUR_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.PoleId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        t.BatterySoC       AS BatteryPercentage,
        t.PanelPercentage  AS PanelPercentage,
        t.LightRatio       AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE
            WHEN ISNULL(t.IsDaylightForLedFault, 0) = 1 THEN 0
            WHEN t.LEDFault = 1 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN t.BatteryFault = 1 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN t.ControllerFault = 1 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        t.IsDaylightForPanelFault,
        t.IsDaylightForLedFault,
        t.LastUpload
    FROM PoleTelemetry t
    LEFT JOIN PoleTimeZones ptz ON t.PoleId = ptz.ProvisionedPoleId
    WHERE t.Source = 'Provisioned'
      AND t.LastUpload >= ?
      AND t.LastUpload <> ?
),
Bucketed AS (
    SELECT
        PoleId,
        TimeZoneName,
        DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage,
        IsOnlineFlag, IsLedFaultFlag, IsBatteryFaultFlag, IsPanelFaultFlag, IsOpenIssueFault,
        IsDaylightForPanelFault, IsDaylightForLedFault,
        ROW_NUMBER() OVER (
            PARTITION BY PoleId, DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')
            ORDER BY LastUpload DESC
        ) AS LatestInBucket
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        PoleId,
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
    GROUP BY PoleId, TimeZoneName, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        PoleId,
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
ON target.PoleId = source.PoleId
   AND target.PeriodType = source.PeriodType
   AND target.PeriodStart = source.PeriodStart
WHEN MATCHED AND NOT EXISTS (
    SELECT source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage,
           source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault,
           source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount
    INTERSECT
    SELECT target.AvgBatteryPercentage, target.AvgPanelPercentage, target.AvgLightPercentage,
           target.IsOnline, target.IsLedFault, target.IsBatteryFault, target.IsPanelFault,
           target.IsOpenIssueFault, target.IsPoleFault, target.RecordCount
)
THEN UPDATE SET
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
    INSERT (PoleId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.PoleId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

# Provisioned Last48Hours: device pre-computes percentages and fault flags,
# so no raw-current math, no PoleModels join, no daylight filtering on averages.
# Written as a standalone string (not derived from _LAST_48_HOURS_MERGE_SQL).
_PROVISIONED_LAST_48_HOURS_MERGE_SQL = """
SET ANSI_WARNINGS OFF;
;WITH TelemetryWithVitals AS (
    SELECT
        t.PoleId,
        t.BatterySoC      AS BatteryPercentage,
        t.PanelPercentage AS PanelPercentage,
        t.LightRatio      AS LightPercentage,
        t.IsDaylightForPanelFault,
        t.IsDaylightForLedFault,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE
            WHEN ISNULL(t.IsDaylightForLedFault, 0) = 1 THEN 0
            WHEN t.LEDFault = 1 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        CASE WHEN t.BatteryFault = 1 THEN 1 ELSE 0 END AS IsBatteryFaultFlag,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN t.ControllerFault = 1 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.IsOpenIssueFault,
        ROW_NUMBER() OVER (PARTITION BY t.PoleId ORDER BY t.LastUpload DESC) AS LatestOverall
    FROM PoleTelemetry t
    WHERE t.Source = 'Provisioned'
      AND t.LastUpload >= ?
      AND t.LastUpload <> ?
),
Aggregated AS (
    SELECT
        PoleId,
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
    GROUP BY PoleId
)
MERGE PoleVitals AS target
USING (
    SELECT
        PoleId,
        'Last48Hours' AS PeriodType,
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
ON target.PoleId = source.PoleId
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
    INSERT (PoleId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, IsLedFault, IsBatteryFault, IsPanelFault, IsOpenIssueFault, IsPoleFault, RecordCount, Source, SP_ExecId)
    VALUES (source.PoleId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.IsLedFault, source.IsBatteryFault, source.IsPanelFault, source.IsOpenIssueFault, source.IsPoleFault, source.RecordCount, source.Source, source.SP_ExecId);
SET ANSI_WARNINGS ON;
"""

_PROVISIONED_MERGE_SQL_BY_PERIOD_TYPE = {
    "Hour": _PROVISIONED_HOUR_MERGE_SQL,
    "Last48Hours": _PROVISIONED_LAST_48_HOURS_MERGE_SQL,
}


def load_provisioned_pole_vitals(backfill: bool = False) -> None:
    """
    Recomputes PoleVitals for provisioned poles using device-reported
    percentages and fault flags (BatterySoC, LightRatio, PanelPercentage,
    BatteryFault, LEDFault, ControllerFault). Hour MERGE joins PoleTimeZones
    on ProvisionedPoleId; Last48Hours needs no timezone join.

    Must run after load_provisioned_pole_daylight_flags().
    """
    _run_vitals_for_source(
        "loadProvisionedPoleVitals",
        PROVISIONED_SOURCE_NAME,
        _PROVISIONED_MERGE_SQL_BY_PERIOD_TYPE,
        backfill,
    )
