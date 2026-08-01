import os
import logging

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.timezone_utils import resolve_windows_timezone

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

# Two distinct meanings that used to share one SOURCE_NAME constant, now
# split apart since they've diverged: EXECUTION_SOURCE tracks which
# pipeline this loader's own run belongs to (SP_Execution.Source) --
# still "Leadsun", since this loader still runs as part of that
# pipeline's Models -> Telemetry -> TimeZones -> DaylightFlags -> Vitals
# load order, regardless of where the coordinates it resolves come from.
# COORDINATE_SOURCE tracks where each row's own Longitude/Latitude
# actually came from (PoleTimeZones.Source) -- now "Airtable" (via
# Poles.Long/Poles.Lat), not "Leadsun" (PoleTelemetry's raw GPS) --
# see _FIND_UNRESOLVED_LOCATIONS_SQL's own comment for why this changed.
EXECUTION_SOURCE = "Leadsun"
COORDINATE_SOURCE = "Airtable"

# One row per not-yet-resolved LocationId, sourced from Poles (Airtable),
# not PoleTelemetry (Leadsun's raw device GPS) -- changed because
# PoleTelemetry's own Latitude/Longitude are the ones documented
# elsewhere in this codebase as occasionally corrupted/placeholder values
# (see pole_daylight_flags_loader.py's own comments on preferring
# PoleTimeZones' cached coordinates over PoleTelemetry's raw ones for
# exactly this reason). Poles.Lat/Poles.Long, coming from Airtable, are
# the more reliable source.
#
# No GROUP BY/MIN() needed here (unlike the PoleTelemetry-based version
# this replaced, which had to pick one representative reading out of
# many time-series rows per LocationId) -- Poles is a reference table,
# one row per pole, so there's exactly one Lat/Long to read per
# LocationId already.
#
# p.LocationId IS NOT NULL matters here specifically: a pole can exist
# in Poles before it's linked to a real Leadsun device (LocationId not
# yet assigned). The old PoleTelemetry-driven query never had to guard
# against this, since a pole with no LocationId could never have
# appeared in PoleTelemetry in the first place -- reading directly from
# Poles now needs this filter explicit, or a NULL LocationId would
# satisfy "ptz.LocationId IS NULL" via the LEFT JOIN and attempt to
# resolve/insert a timezone row for a pole with no real location at all.
_FIND_UNRESOLVED_LOCATIONS_SQL = """
SELECT p.LocationId, p.Long AS Longitude, p.Lat AS Latitude
FROM Poles p
LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId
WHERE ptz.LocationId IS NULL
  AND p.LocationId IS NOT NULL
  AND p.Long IS NOT NULL
  AND p.Lat IS NOT NULL
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
    Resolves and caches each not-yet-seen LocationId's timezone (from
    Poles' own Lat/Long, i.e. Airtable's coordinates for that pole -- not
    PoleTelemetry's raw device GPS, which is the known-occasionally-bad
    source; see _FIND_UNRESOLVED_LOCATIONS_SQL's own comment) into
    PoleTimeZones, for pole_vitals_loader.py's per-pole Hour/Day/Month/Week
    bucketing.

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
            EXECUTION_SOURCE,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Find LocationIds in Poles that PoleTimeZones doesn't have yet
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
                    COORDINATE_SOURCE,
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
