import os
import logging

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.daylight_utils import is_daylight

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

# Bounds how many not-yet-flagged rows get processed per run. Mainly
# relevant right after IsDaylight is first added (when potentially the
# entire existing PoleTelemetry history is unflagged, needing several
# runs to fully backfill) -- ongoing operation only ever has a small
# number of newly-arrived, not-yet-flagged rows per 10-minute cycle, well
# under this cap.
_BATCH_SIZE = 10000

# INNER JOIN (not LEFT): a row whose LocationId has no PoleTimeZones
# entry yet can't have its daylight status computed at all -- it's left
# for a later cycle once load_pole_timezones() has caught up, matching
# the load-order dependency (TimeZones runs before this loader).
#
# WindowsTimeZone IS NOT NULL: a NULL WindowsTimeZone means that
# location's stored Latitude/Longitude couldn't be trusted (Null Island,
# out-of-range/corrupted values -- see shared/timezone_utils.py).
# is_daylight() has no defensive validation of its own, so feeding it
# those same untrustworthy coordinates isn't safe.
_FIND_UNFLAGGED_SQL = """
SELECT TOP (?) t.LocationId, t.LastUpload, ptz.Latitude, ptz.Longitude
FROM PoleTelemetry t
JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
WHERE t.IsDaylight IS NULL
  AND ptz.WindowsTimeZone IS NOT NULL
ORDER BY t.LastUpload
"""

_UPDATE_IS_DAYLIGHT_SQL = """
UPDATE PoleTelemetry
SET IsDaylight = ?
WHERE LocationId = ? AND LastUpload = ?
"""


def load_pole_daylight_flags() -> None:
    """
    Computes and caches IsDaylight on PoleTelemetry rows that don't have
    it yet, using PoleTimeZones' Latitude/Longitude for each LocationId
    -- deliberately NOT PoleTelemetry's own Longitude/Latitude columns --
    and each row's own LastUpload timestamp.

    A reading's timestamp never changes once recorded, so "was it
    daylight at that exact moment, at that pole's location" is a fact
    that never changes either -- computed once, cached forever, same
    reasoning as PoleTimeZones caching each pole's timezone once.

    is_daylight() (shared/daylight_utils.py) is a Python computation
    (built on the astral library) -- there is no way to run it inside a
    T-SQL query, which is why this is a separate loader/cached column
    rather than being computed inline in pole_vitals_loader.py's
    aggregation SQL, the same reasoning that motivated PoleTimeZones
    existing at all instead of calling timezonefinder from SQL.
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
            "loadPoleDaylightFlags",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Find not-yet-flagged readings whose location we trust
        cursor.execute(_FIND_UNFLAGGED_SQL, _BATCH_SIZE)
        unflagged = cursor.fetchall()
        logging.info(
            "loadPoleDaylightFlags: %d not-yet-flagged reading(s) found (capped at %d per run).",
            len(unflagged),
            _BATCH_SIZE,
        )

        # 3. Compute is_daylight() per row (Python -- astral can't run
        # inside SQL) and collect successes for a bulk write
        updates = []
        for location_id, last_upload, latitude, longitude in unflagged:
            try:
                daylight = is_daylight(last_upload, latitude, longitude)
                # last_upload came back from the SELECT above as a
                # timezone-aware Python datetime (via sql_client.py's
                # DATETIMEOFFSET output converter). Binding that same raw
                # datetime object back as a WRITE parameter is exactly the
                # pyodbc + DATETIMEOFFSET gotcha this project already
                # knows about (pyodbc silently mishandles a tz-aware
                # datetime as an input parameter) -- every other loader
                # here formats as an explicit offset string via
                # _to_dto_string() before binding, and this one needs to
                # as well, or WHERE LastUpload = ? silently matches zero
                # rows (not an error -- executemany() doesn't raise for a
                # WHERE clause that matches nothing), which was exactly
                # what was happening here before this fix.
                updates.append((daylight, location_id, _to_dto_string(last_upload)))
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadPoleDaylightFlags: failed to compute IsDaylight for %s @ %s: %s",
                    location_id,
                    last_upload,
                    row_error,
                )

        # 4. Write the successfully-computed flags back, batched for
        # speed with a per-row fallback if the batch itself fails --
        # same fallback shape used elsewhere in this project's loaders.
        if updates:
            try:
                cursor.fast_executemany = True
                cursor.executemany(_UPDATE_IS_DAYLIGHT_SQL, updates)
                total_success += len(updates)
                # cursor.rowcount after executemany() is driver-dependent
                # (some ODBC drivers report the total across all batched
                # statements, others -1/unknown, never just plain 0 unless
                # genuinely zero rows matched) -- not reliable enough to
                # base success/failure on in general, but a clean 0 here
                # specifically is worth a loud warning: that exact
                # symptom (no exception raised, but the WHERE clause
                # silently matched nothing) was the real bug this
                # _to_dto_string() fix addresses, so if it recurs for a
                # different reason, this should surface it immediately
                # rather than silently reporting success again.
                if cursor.rowcount == 0:
                    logging.warning(
                        "loadPoleDaylightFlags: batch update reported 0 rows affected for "
                        "%d attempted update(s) -- the WHERE clause may not be matching "
                        "(the same class of bug this loader was previously fixed for).",
                        len(updates),
                    )
            except Exception as batch_error:
                logging.warning(
                    "loadPoleDaylightFlags: batch update failed (%s), falling back to "
                    "row-by-row.",
                    batch_error,
                )
                for daylight, location_id, last_upload in updates:
                    try:
                        cursor.execute(_UPDATE_IS_DAYLIGHT_SQL, daylight, location_id, last_upload)
                        total_success += 1
                    except Exception as row_error:
                        total_errors += 1
                        logging.error(
                            "loadPoleDaylightFlags: failed to store IsDaylight for %s @ %s: %s",
                            location_id,
                            last_upload,
                            row_error,
                        )

        conn.commit()

        # 5. Close out the SP_Execution row with final counts
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
        logging.error("loadPoleDaylightFlags: run failed: %s", ex)
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
