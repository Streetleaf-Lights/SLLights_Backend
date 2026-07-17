import os
import logging
import time
from datetime import timedelta

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.pole_telemetry_loader import _MISSING_LAST_UPLOAD_SENTINEL

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

PERIOD_TYPES = ("Hour", "Day", "Week", "Month")

# How far back each period type recomputes on a normal (non-backfill) run.
# Wide enough to cover "the current bucket + the previous bucket" (so
# late-arriving telemetry near a boundary still lands in the right bucket)
# without rescanning PoleTelemetry's full 6-month retention window every
# 10 minutes -- the same round-trip-count trap already hit (and fixed) for
# Poles/PoleTelemetry itself. Bounded by IX_PoleTelemetry_LastUpload.
_DEFAULT_LOOKBACK = {
    "Hour": timedelta(hours=3),
    "Day": timedelta(days=2),
    "Week": timedelta(days=8),
    "Month": timedelta(days=35),
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

_HOUR_MERGE_SQL = """
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(3)) AS LocalTime,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.LastUpload >= ?
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Bucketed AS (
    SELECT
        LocationId,
        DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101') AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        LocationId,
        BucketStart,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Hour' AS PeriodType,
        BucketStart AT TIME ZONE 'Eastern Standard Time' AS PeriodStart,
        DATEADD(HOUR, 1, BucketStart) AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount,
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
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.RecordCount, source.Source, source.SP_ExecId);
"""

_DAY_MERGE_SQL = """
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(3)) AS LocalTime,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.LastUpload >= ?
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Bucketed AS (
    SELECT
        LocationId,
        CAST(LocalTime AS DATE) AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        LocationId,
        BucketStart,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Day' AS PeriodType,
        CAST(BucketStart AS DATETIME2(3)) AT TIME ZONE 'Eastern Standard Time' AS PeriodStart,
        CAST(DATEADD(DAY, 1, BucketStart) AS DATETIME2(3)) AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount,
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
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.RecordCount, source.Source, source.SP_ExecId);
"""

_MONTH_MERGE_SQL = """
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(3)) AS LocalTime,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.LastUpload >= ?
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Bucketed AS (
    SELECT
        LocationId,
        DATEFROMPARTS(YEAR(LocalTime), MONTH(LocalTime), 1) AS BucketStart,
        BatteryPercentage, PanelPercentage, LightPercentage
    FROM TelemetryWithVitals
),
Aggregated AS (
    SELECT
        LocationId,
        BucketStart,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, BucketStart
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Month' AS PeriodType,
        CAST(BucketStart AS DATETIME2(3)) AT TIME ZONE 'Eastern Standard Time' AS PeriodStart,
        CAST(DATEADD(MONTH, 1, BucketStart) AS DATETIME2(3)) AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount,
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
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.RecordCount, source.Source, source.SP_ExecId);
"""

# Week uses the Workweek table (not raw date math) to define bucket
# boundaries, per the explicit request to use "the Workweek definition".
_WEEK_MERGE_SQL = """
;WITH TelemetryWithVitals AS (
    SELECT
        t.LocationId,
        CAST(t.LastUpload AT TIME ZONE 'Eastern Standard Time' AS DATETIME2(3)) AS LocalTime,
        (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
        (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
        (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage
    FROM PoleTelemetry t
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    WHERE t.LastUpload >= ?
      AND t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see pole_telemetry_loader.py)
),
Bucketed AS (
    SELECT
        tv.LocationId,
        w.StartDate AS BucketStart,
        w.EndDate AS BucketEnd,
        tv.BatteryPercentage, tv.PanelPercentage, tv.LightPercentage
    FROM TelemetryWithVitals tv
    JOIN Workweek w ON CAST(tv.LocalTime AS DATE) BETWEEN w.StartDate AND w.EndDate
),
Aggregated AS (
    SELECT
        LocationId,
        BucketStart,
        BucketEnd,
        AVG(BatteryPercentage) AS AvgBatteryPercentage,
        AVG(PanelPercentage)   AS AvgPanelPercentage,
        AVG(LightPercentage)   AS AvgLightPercentage,
        COUNT(*)                AS RecordCount
    FROM Bucketed
    GROUP BY LocationId, BucketStart, BucketEnd
)
MERGE PoleVitals AS target
USING (
    SELECT
        LocationId,
        'Week' AS PeriodType,
        CAST(BucketStart AS DATETIME2(3)) AT TIME ZONE 'Eastern Standard Time' AS PeriodStart,
        CAST(DATEADD(DAY, 1, BucketEnd) AS DATETIME2(3)) AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd,
        AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount,
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
    RecordCount           = source.RecordCount,
    Source                = source.Source,
    SP_ExecId             = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, PeriodType, PeriodStart, PeriodEnd, AvgBatteryPercentage, AvgPanelPercentage, AvgLightPercentage, RecordCount, Source, SP_ExecId)
    VALUES (source.LocationId, source.PeriodType, source.PeriodStart, source.PeriodEnd, source.AvgBatteryPercentage, source.AvgPanelPercentage, source.AvgLightPercentage, source.RecordCount, source.Source, source.SP_ExecId);
"""

_MERGE_SQL_BY_PERIOD_TYPE = {
    "Hour": _HOUR_MERGE_SQL,
    "Day": _DAY_MERGE_SQL,
    "Week": _WEEK_MERGE_SQL,
    "Month": _MONTH_MERGE_SQL,
}


def _is_benign_null_aggregate_warning(exc: Exception) -> bool:
    """
    SQLSTATE 01003 ("Warning: Null value is eliminated by an aggregate or
    other SET operation") is SQL Server's informational notice that
    AVG()/etc. skipped over a NULL -- not a real failure. It's the
    designed, expected consequence of this loader's NULLIF-guarded
    PanelPercentage/LightPercentage formulas: a reading with a missing
    model or a zero SunboardPower/LightPower is *supposed* to drop out of
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
    Recomputes PoleVitals from PoleTelemetry + PoleModels (+ Workweek for
    the Week period type). Each period type is its own MERGE -- no
    per-row Python loop or staging table needed here, unlike the other
    loaders, since the SQL aggregation itself produces a modest number of
    output rows (bounded by distinct LocationIds x a couple of buckets),
    not thousands of individually-bound parameter rows.

    Set backfill=True for a one-off historical recompute covering
    PoleTelemetry's entire 6-month retention window, instead of the small
    "current + previous bucket" window used on every normal run.
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

        # 2. Recompute each period type. A failure in one doesn't block
        # the others -- they're independent aggregations, so isolating
        # failures per period type (rather than per row, as in the other
        # loaders) is the natural granularity here.
        upsert_start = time.perf_counter()
        now = _now_eastern()
        for period_type in PERIOD_TYPES:
            merge_sql = _MERGE_SQL_BY_PERIOD_TYPE[period_type]
            cutoff = _compute_cutoff(now, period_type, backfill)
            try:
                cursor.execute(
                    merge_sql, cutoff, _MISSING_LAST_UPLOAD_SENTINEL, SOURCE_NAME, sp_exec_id
                )
                affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                total_success += affected
                logging.info(
                    "loadPoleVitals: %s period recomputed, %d row(s) affected (since %s).",
                    period_type,
                    affected,
                    cutoff,
                )
            except Exception as period_error:
                if _is_benign_null_aggregate_warning(period_error):
                    # SQLSTATE 01003 ("Warning: Null value is eliminated by
                    # an aggregate...") is informational, not a real
                    # failure -- see _is_benign_null_aggregate_warning's
                    # docstring. The MERGE itself completed; only pyodbc's
                    # exception-raising made it look like a failure.
                    affected = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    total_success += affected
                    logging.info(
                        "loadPoleVitals: %s period recomputed, %d row(s) affected (since %s) -- "
                        "some reading(s) had a missing/zero SunboardPower or LightPower and were "
                        "excluded from that specific average, which is expected, not an error.",
                        period_type,
                        affected,
                        cutoff,
                    )
                else:
                    total_errors += 1
                    logging.error(
                        "loadPoleVitals: failed to recompute %s period: %s",
                        period_type,
                        period_error,
                    )

        conn.commit()
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
