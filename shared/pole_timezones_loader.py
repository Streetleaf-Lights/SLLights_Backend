import os
import logging

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.timezone_utils import resolve_windows_timezone

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

# One representative reading's coordinates per not-yet-resolved
# LocationId. MIN() is just a deterministic way to pick ANY single
# reading per LocationId -- a stationary pole's coordinates shouldn't
# meaningfully vary between readings, so which specific reading gets
# picked doesn't matter for timezone-resolution purposes (GPS jitter of a
# few hundred meters practically never crosses a timezone boundary).
_FIND_UNRESOLVED_LOCATIONS_SQL = """
SELECT t.LocationId, MIN(t.Longitude) AS Longitude, MIN(t.Latitude) AS Latitude
FROM PoleTelemetry t
LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
WHERE ptz.LocationId IS NULL
  AND t.Longitude IS NOT NULL
  AND t.Latitude IS NOT NULL
GROUP BY t.LocationId
"""

_UPSERT_TIMEZONE_SQL = """
MERGE PoleTimeZones AS target
USING (
    SELECT ? AS LocationId, ? AS Longitude, ? AS Latitude, ? AS IanaTimeZone,
           ? AS WindowsTimeZone, ? AS Source, ? AS SP_ExecId
) AS source
ON target.LocationId = source.LocationId
WHEN MATCHED THEN UPDATE SET
    Longitude       = source.Longitude,
    Latitude        = source.Latitude,
    IanaTimeZone    = source.IanaTimeZone,
    WindowsTimeZone = source.WindowsTimeZone,
    Source          = source.Source,
    SP_ExecId       = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (LocationId, Longitude, Latitude, IanaTimeZone, WindowsTimeZone, Source, SP_ExecId)
    VALUES (source.LocationId, source.Longitude, source.Latitude, source.IanaTimeZone,
            source.WindowsTimeZone, source.Source, source.SP_ExecId);
"""


def load_pole_timezones() -> None:
    """
    Resolves and caches each not-yet-seen LocationId's timezone (from its
    PoleTelemetry Longitude/Latitude) into PoleTimeZones, for
    pole_vitals_loader.py's per-pole Hour/Day/Month/Week bucketing.

    Only resolves LocationIds NOT ALREADY in PoleTimeZones -- poles are
    stationary, so a location's timezone never changes once resolved,
    making this a one-time-per-pole cost rather than something to redo
    every 10-minute cycle. Deliberately NOT staging-table-bulk-merged like
    Poles/PoleTelemetry: this only processes brand-new LocationIds each
    run (typically zero to a handful, after the initial backfill), not
    the kind of volume that pattern exists to handle.
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
            "loadPoleTimeZones",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Find LocationIds seen in PoleTelemetry that PoleTimeZones
        # doesn't have yet
        cursor.execute(_FIND_UNRESOLVED_LOCATIONS_SQL)
        unresolved = cursor.fetchall()
        logging.info(
            "loadPoleTimeZones: %d new LocationId(s) need timezone resolution.",
            len(unresolved),
        )

        # 3. Resolve each one (Python -- timezonefinder can't run inside
        # SQL) and upsert. Per-row, not batched -- see the docstring above
        # for why that's fine here.
        for location_id, longitude, latitude in unresolved:
            try:
                iana_name, windows_name = resolve_windows_timezone(latitude, longitude)
                cursor.execute(
                    _UPSERT_TIMEZONE_SQL,
                    location_id,
                    longitude,
                    latitude,
                    iana_name,
                    windows_name,
                    SOURCE_NAME,
                    sp_exec_id,
                )
                total_success += 1
                if windows_name is None:
                    # resolve_windows_timezone() already logged *why* (Null
                    # Island, genuinely unresolvable coordinates, or an
                    # unmapped IANA zone) -- this just adds the LocationId
                    # context it doesn't have access to, so it's clear
                    # *which* pole/gateway is affected.
                    logging.warning(
                        "loadPoleTimeZones: %s has no resolved Windows timezone "
                        "(lat=%s, lng=%s) -- will fall back to the default timezone "
                        "in PoleVitals. See the warning/error above for why.",
                        location_id,
                        latitude,
                        longitude,
                    )
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadPoleTimeZones: failed to resolve/store timezone for %s: %s",
                    location_id,
                    row_error,
                )

        conn.commit()

        # 4. Close out the SP_Execution row with final counts
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
        logging.error("loadPoleTimeZones: run failed: %s", ex)
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
