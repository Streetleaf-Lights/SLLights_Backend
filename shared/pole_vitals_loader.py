import os
import logging
import time
from datetime import timedelta

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.pole_telemetry_loader import _MISSING_LAST_UPLOAD_SENTINEL

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

PERIOD_TYPES = ("Hour", "Day")

# How far back each period type recomputes on a normal (non-backfill) run.
# Wide enough to cover "the current bucket + the previous bucket" (so
# late-arriving telemetry near a boundary still lands in the right bucket)
# without rescanning PoleTelemetry's full 6-month retention window every
# 10 minutes -- the same round-trip-count trap already hit (and fixed) for
# Poles/PoleTelemetry itself. Bounded by IX_PoleTelemetry_LastUpload.
#
# Week and Month were removed entirely (see the module docstring and
# README for the full history) -- this dict only ever needs Hour/Day now.
_DEFAULT_LOOKBACK = {
    "Hour": timedelta(hours=3),
    "Day": timedelta(days=2),
}

# Wide enough to cover PoleTelemetry's entire 6-month retention window --
# for a one-off historical backfill via load_pole_vitals(backfill=True).
_BACKFILL_LOOKBACK = timedelta(days=400)


def _compute_cutoff(now, period_type: str, backfill: bool):
    """
    Returns the DTO-formatted cutoff string for the WHERE t.LastUpload >= ?
    AND t.LastUpload <> ? parameters -- pure function, kept separate from
    load_pole_vitals() so the lookback-window math is unit-testable
    without a database.
    """
    lookback = _BACKFILL_LOOKBACK if backfill else _DEFAULT_LOOKBACK[period_type]
    return _to_dto_string(now - lookback)


# Shared per-reading formulas, reused (as literal SQL, not a Python string
# template -- see the module docstring reasoning) at the top of each period
# type's CTE below:
#   BatteryPercentage = (BatteryElecCurrent1 + BatteryElecCurrent2) / 2
#   PanelPercentage   = (SolarBoardVoltage * SolarBoardElecCurrent) / SunboardPower * 100
#   LightPercentage   = (LampPower1 + LampPower2) / LightPower * 100
# NULLIF guards divide-by-zero/missing-model cases -- that reading
# contributes NULL for the affected percentage, which AVG() ignores rather
# than skewing the result or erroring.
#
# LightStatus per-reading classification (see shared/daylight_utils.py
# for IsDaylight's own computation -- that's a separate cached column on
# PoleTelemetry, not computed here, since it needs astral/Python and
# can't be expressed in T-SQL):
#   IsOnline = 0                          -> 'Working'  (no data to judge
#                                             a malfunction from, so this
#                                             does NOT flag it as broken)
#   IsDaylight IS NULL                    -> NULL (excluded, same as the
#                                             NULLIF-guarded percentages)
#   IsDaylight = 1                        -> 'DayLight' (lamp isn't
#                                             expected to be lit, so this
#                                             isn't a working/not-working
#                                             judgment at all)
#   LampPower1 > 0 OR LampPower2 > 0      -> 'Working'  (confirmed lit at
#                                             night, as expected)
#   otherwise (online, night, lamp dark)  -> 'Not Working' (the genuine
#                                             anomaly case)
#
# IsOnline and LightStatus, for Day specifically, deliberately use a
# narrower window than the rest of that period's data: the last 6 hours
# OF THAT BUCKET'S OWN END, not the whole day (which would be true for
# nearly every actively-reporting pole most of the time, making the flag
# far less useful) -- "was this pole alive/healthy toward the end of this
# period" rather than "at any point during it". Crucially, this is
# relative to each bucket's OWN PeriodEnd, computed as a SQL expression
# inside Bucketed (DATEADD(HOUR, -6, DATEADD(DAY, 1, BucketStart))) --
# NOT relative to when this loader happens to run. A historical bucket
# recomputed later (e.g. "last Tuesday", still touched by the
# current+previous incremental window) checks the same
# last-6-hours-of-that-day window every time, not whatever's 6 hours
# before "now". Hour doesn't need this at all -- its own 1-hour bucket
# boundary already serves the same purpose.

_HOUR_MERGE_SQL = """
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage,
        CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
        CASE
            WHEN t.IsOnline = 0 THEN 'Working'
            WHEN t.IsDaylight IS NULL THEN NULL
            WHEN t.IsDaylight = 1 THEN 'DayLight'
            WHEN (t.LampPower1 > 0 OR t.LampPower2 > 0) THEN 'Working'
            ELSE 'Not Working'
        END AS LightStatusPerRow
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
        IsOnlineFlag, LightStatusPerRow
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
        MAX(IsOnlineFlag)      AS IsOnlineAgg,
        CASE
            WHEN MAX(CASE WHEN LightStatusPerRow = 'Not Working' THEN 1 ELSE 0 END) = 1 THEN 'Not Working'
            WHEN MAX(CASE WHEN LightStatusPerRow = 'Working' THEN 1 ELSE 0 END) = 1 THEN 'Working'
            ELSE 'DayLight'
        END AS LightStatusAgg,
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
        IsOnlineAgg AS IsOnline, LightStatusAgg AS LightStatus,
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
    LightStatus           = source.LightStatus,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, LightStatus, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.LightStatus, source.RecordCount, source.Source, source.SP_ExecId);
"""

_DAY_MERGE_SQL = """
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS DATETIME2(3)) AS LocalTime,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage,
        t.IsOnline,
        t.IsDaylight,
        t.LampPower1,
        t.LampPower2
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
        BatteryPercentage, PanelPercentage, LightPercentage,
        -- "Last 6 hours" is relative to THIS bucket's own end (the day's
        -- own midnight), not to when this query happens to run -- a
        -- historical Day recomputed later always checks the same 18:00-
        -- midnight window of that specific day.
        CASE
            WHEN LocalTime >= DATEADD(HOUR, -6, DATEADD(DAY, 1, CAST(CAST(LocalTime AS DATE) AS DATETIME2(3))))
                 AND IsOnline = 1
            THEN 1 ELSE 0
        END AS IsOnlineRecentFlag,
        CASE
            WHEN LocalTime < DATEADD(HOUR, -6, DATEADD(DAY, 1, CAST(CAST(LocalTime AS DATE) AS DATETIME2(3)))) THEN NULL
            WHEN IsOnline = 0 THEN 'Working'
            WHEN IsDaylight IS NULL THEN NULL
            WHEN IsDaylight = 1 THEN 'DayLight'
            WHEN (LampPower1 > 0 OR LampPower2 > 0) THEN 'Working'
            ELSE 'Not Working'
        END AS LightStatusPerRow
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
        CASE
            WHEN MAX(CASE WHEN LightStatusPerRow = 'Not Working' THEN 1 ELSE 0 END) = 1 THEN 'Not Working'
            WHEN MAX(CASE WHEN LightStatusPerRow = 'Working' THEN 1 ELSE 0 END) = 1 THEN 'Working'
            ELSE 'DayLight'
        END AS LightStatusAgg,
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
        IsOnlineAgg AS IsOnline, LightStatusAgg AS LightStatus,
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
    LightStatus           = source.LightStatus,
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, IsOnline, LightStatus, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.IsOnline, source.LightStatus, source.RecordCount, source.Source, source.SP_ExecId);
"""

_MERGE_SQL_BY_PERIOD_TYPE = {
    "Hour": _HOUR_MERGE_SQL,
    "Day": _DAY_MERGE_SQL,
}


def _is_benign_null_aggregate_warning(exc: Exception) -> bool:
    """
    SQLSTATE 01003 ("Warning: Null value is eliminated by an aggregate or
    other SET operation") is SQL Server's informational notice that
    AVG()/etc. skipped over a NULL -- not a real failure. It's the
    designed, expected consequence of this loader's NULLIF-guarded
    PanelPercentage/LightPercentage formulas (and now also LightStatus's
    NULL-when-IsDaylight-is-unknown case): a reading with a missing
    model, zero SunboardPower/LightPower, or unresolved daylight status
    is *supposed* to drop out of that specific average/classification.
    pyodbc still raises it as a Python exception though (SQLSTATE class
    "01" is warning, not error, but pyodbc doesn't distinguish for the
    purposes of cursor.execute() raising), so without this check a MERGE
    that actually completed successfully gets logged and counted as a
    failure.
    """
    args = getattr(exc, "args", ())
    return bool(args) and args[0] == "01003"


def load_pole_vitals(backfill: bool = False) -> None:
    """
    Recomputes PoleVitals from PoleTelemetry + PoleModels + PoleTimeZones.
    Each period type is its own MERGE -- no per-row Python loop or
    staging table needed here, unlike the other loaders, since the SQL
    aggregation itself produces a modest number of output rows (bounded
    by distinct LocationIds x a couple of buckets), not thousands of
    individually-bound parameter rows.

    Only Hour and Day period types -- Week and Month were removed
    entirely (see the module docstring and README for the full history:
    Week's Workweek-table join produced a genuine row-explosion bug that
    got fixed, but even after fixing it and scaling up compute, Week
    remained the dominant cost of every run by a wide margin, and the
    decision was made to stop computing Week/Month rather than keep
    tuning them further).

    Commits after EACH period type individually, not once at the end for
    all of them -- a slow or failing period type (e.g. genuine database
    resource contention, or an external kill like the hosting platform's
    own function-execution timeout) can no longer roll back an earlier
    period type's already-computed, already-succeeded results. Each
    period type's fate -- commit on success, rollback on genuine failure
    -- is now independent of what happens to the others.

    Set backfill=True for a one-off historical recompute covering
    PoleTelemetry's entire 6-month retention window, instead of the small
    "current + previous bucket" window used on every normal run. This
    also affects IsOnline/LightStatus's "last 6 hours" window for Day,
    since a backfilled bucket's own PeriodEnd is what that window is
    relative to -- see the module-level comment above _HOUR_MERGE_SQL
    for the full reasoning.
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
        # each one succeeds -- NOT once at the end for both. This matters
        # in practice, not just in theory: if one period type is slow
        # (e.g. genuine database resource contention) or fails outright,
        # the other's already-computed rows are durably saved the moment
        # its own commit happens, rather than sitting in an open
        # transaction that a stuck period type (or an external kill, e.g.
        # the platform's own function-execution timeout) would roll back,
        # discarding already-finished work along with whatever never
        # finished. (This was originally motivated by a real incident
        # with Week specifically, back when it still existed -- see the
        # README for that history.) A failure in one period type still
        # doesn't block the other -- they're independent aggregations --
        # but each now succeeds or fails (and commits or rolls back)
        # entirely on its own.
        upsert_start = time.perf_counter()
        now = _now_eastern()
        for period_type in PERIOD_TYPES:
            merge_sql = _MERGE_SQL_BY_PERIOD_TYPE[period_type]
            cutoff = _compute_cutoff(now, period_type, backfill)
            params = (cutoff, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id)

            try:
                cursor.execute(merge_sql, *params)
                affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                conn.commit()
                total_success += affected
                logging.info(
                    "loadPoleVitals: %s period recomputed and committed, %d row(s) affected (since %s).",
                    period_type,
                    affected,
                    cutoff,
                )
            except Exception as period_error:
                if _is_benign_null_aggregate_warning(period_error):
                    # SQLSTATE 01003 ("Warning: Null value is eliminated by
                    # an aggregate...") is informational, not a real
                    # failure -- see _is_benign_null_aggregate_warning's
                    # docstring. The MERGE itself completed, so this still
                    # commits -- only pyodbc's exception-raising made it
                    # look like a failure.
                    affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    conn.commit()
                    total_success += affected
                    logging.info(
                        "loadPoleVitals: %s period recomputed and committed, %d row(s) affected "
                        "(since %s) -- some reading(s) had a missing/zero SunboardPower, "
                        "LightPower, or unresolved daylight status and were excluded from that "
                        "specific average/classification, which is expected, not an error.",
                        period_type,
                        affected,
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
