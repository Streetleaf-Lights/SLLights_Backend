import os
import logging
from datetime import timedelta

from shared.sql_client import get_connection
from shared.datetime_utils import (
    now_eastern as _now_eastern,
    to_dto_string as _to_dto_string,
)
from shared.daylight_utils import is_daylight

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"
PROVISIONED_SOURCE_NAME = "Provisioned"

# Rows processed per run. During initial backfill (entire history unflagged)
# this limits each run; at steady state only a small number of newly-arrived
# rows are unflagged per cycle.
_BATCH_SIZE = 20000

# Progress log interval during the per-row compute loop -- the astral
# calculations are CPU-bound and can run silently long enough to look
# like a hang without these periodic lines.
_PROGRESS_LOG_INTERVAL = 5000

# Grace period for IsDaylightForLedFault: lamp response lags real sunrise/sunset
# by up to an hour in both directions, so we expand the "daylight" window
# symmetrically. IsDaylightForLedFault = True if daylight at (now ± grace period).
_LED_FAULT_GRACE_PERIOD = timedelta(hours=1)

# Warmup/winddown for IsDaylightForPanelFault: panel must have been daylight
# for this long already (warmup) AND will still be daylight for this long more
# (winddown) before zero output counts as a fault.
_PANEL_FAULT_SUNRISE_WARMUP_PERIOD = timedelta(hours=1)
_PANEL_FAULT_SUNSET_WINDDOWN_PERIOD = timedelta(hours=1)

# INNER JOIN: rows with no PoleTimeZones entry can't be flagged yet.
# WindowsTimeZone IS NOT NULL: NULL means coordinates were untrustworthy.
# ORDER BY DESC: flags recent rows first so vitals computations see correct
#   daylight status for current data even while the historical backlog clears.
# Sentinel exclusion: '9999-12-31...' rows can never be flagged (and their
#   timestamp arithmetic overflows with the grace period offsets).
_FIND_UNFLAGGED_SQL = """
SELECT TOP (?) t.PoleId, t.LastUpload, ptz.Latitude, ptz.Longitude
FROM PoleTelemetry t
JOIN PoleTimeZones ptz ON t.PoleId = ptz.VendorPoleId
WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL OR t.IsDaylightForPanelFault IS NULL)
  AND ptz.WindowsTimeZone IS NOT NULL
  AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
ORDER BY t.LastUpload DESC
"""

# IsDaylightForLedFault: daylight at (now OR now-grace OR now+grace).
# IsDaylightForPanelFault: daylight at (now AND now-warmup AND now+winddown).
# IsDaylight: strict exact-moment answer (stored for reference, not
#   directly used by either fault flag anymore).
_UPDATE_IS_DAYLIGHT_SQL = """
UPDATE PoleTelemetry
SET IsDaylight = ?, IsDaylightForLedFault = ?, IsDaylightForPanelFault = ?
WHERE PoleId = ? AND LastUpload = ?
"""

# Same as _FIND_UNFLAGGED_SQL but joins PoleTimeZones on ProvisionedPoleId
# (provisioned poles store ProvisionedPoleId as their PoleTelemetry.PoleId).
_FIND_PROVISIONED_UNFLAGGED_SQL = """
SELECT TOP (?) t.PoleId, t.LastUpload, ptz.Latitude, ptz.Longitude
FROM PoleTelemetry t
JOIN PoleTimeZones ptz ON t.PoleId = ptz.ProvisionedPoleId
WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL OR t.IsDaylightForPanelFault IS NULL)
  AND ptz.WindowsTimeZone IS NOT NULL
  AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
ORDER BY t.LastUpload DESC
"""


def _compute_daylight_triple(last_upload, latitude, longitude):
    """
    Computes all three daylight flags for one telemetry row.
    Returns (is_day, is_day_led, is_day_panel).
    Short-circuits as soon as the answer is known for each flag.
    """
    is_day = is_daylight(last_upload, latitude, longitude)

    is_day_led = (
        is_day
        or is_daylight(last_upload - _LED_FAULT_GRACE_PERIOD, latitude, longitude)
        or is_daylight(last_upload + _LED_FAULT_GRACE_PERIOD, latitude, longitude)
    )

    is_day_panel = (
        is_day
        and is_daylight(last_upload - _PANEL_FAULT_SUNRISE_WARMUP_PERIOD, latitude, longitude)
        and is_daylight(last_upload + _PANEL_FAULT_SUNSET_WINDDOWN_PERIOD, latitude, longitude)
    )

    return is_day, is_day_led, is_day_panel


def _write_daylight_updates(cursor, conn, updates, sp_name):
    """
    Bulk-writes daylight flag updates in 2,000-row chunks via executemany(),
    falling back to row-by-row on chunk failure.
    Returns (success_count, error_count).
    """
    success = 0
    errors = 0
    chunk_size = 2000

    for start in range(0, len(updates), chunk_size):
        chunk = updates[start: start + chunk_size]
        try:
            cursor.executemany(_UPDATE_IS_DAYLIGHT_SQL, chunk)
            conn.commit()
            if cursor.rowcount == 0:
                logging.warning(
                    "%s: batch update reported 0 rows affected for %d row(s) -- "
                    "possible PoleId/LastUpload mismatch.",
                    sp_name, len(chunk),
                )
            success += len(chunk)
        except Exception as chunk_ex:
            conn.rollback()
            logging.warning(
                "%s: chunk of %d failed (%s); falling back to row-by-row.",
                sp_name, len(chunk), chunk_ex,
            )
            for row in chunk:
                try:
                    cursor.execute(_UPDATE_IS_DAYLIGHT_SQL, *row)
                    conn.commit()
                    success += 1
                except Exception as row_ex:
                    conn.rollback()
                    errors += 1
                    logging.warning(
                        "%s: failed to store IsDaylight for %s @ %s: %s",
                        sp_name, row[3], row[4], row_ex,
                    )

    return success, errors


def load_leadsun_pole_daylight_flags() -> None:
    """
    Computes and stores IsDaylight, IsDaylightForLedFault, and
    IsDaylightForPanelFault on Leadsun PoleTelemetry rows that are missing
    any of these flags, using PoleTimeZones.Latitude/Longitude (not
    PoleTelemetry's own coordinates) and each row's own LastUpload timestamp.

    Processes up to _BATCH_SIZE rows per run (most-recent first). At
    steady state only a small number of new rows arrive per cycle;
    during initial setup or after adding a new flag column several runs
    are needed to backfill the full history.

    Requires load_leadsun_pole_timezones() to have already run (PoleTimeZones
    rows must exist before daylight flags can be computed).
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()
    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
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

        cursor.execute(_FIND_UNFLAGGED_SQL, _BATCH_SIZE)
        unflagged = cursor.fetchall()
        logging.info(
            "loadPoleDaylightFlags: %d not-yet-flagged reading(s) found (capped at %d per run).",
            len(unflagged),
            _BATCH_SIZE,
        )

        updates = []
        total_unflagged = len(unflagged)
        for index, (pole_id, last_upload, latitude, longitude) in enumerate(unflagged, start=1):
            if index % _PROGRESS_LOG_INTERVAL == 0:
                logging.info(
                    "loadPoleDaylightFlags: computed %d/%d reading(s) so far ...",
                    index,
                    total_unflagged,
                )
            try:
                is_day, is_day_led, is_day_panel = _compute_daylight_triple(
                    last_upload, latitude, longitude
                )
                updates.append((is_day, is_day_led, is_day_panel, pole_id, _to_dto_string(last_upload)))
            except Exception as row_ex:
                total_errors += 1
                logging.warning(
                    "loadPoleDaylightFlags: failed to compute IsDaylight for %s @ %s: %s",
                    pole_id, last_upload, row_ex,
                )

        success, errors = _write_daylight_updates(cursor, conn, updates, "loadPoleDaylightFlags")
        total_success += success
        total_errors += errors

        cursor.execute(
            """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = 1,
                IsFinalBatch = 1
            WHERE Id = ?
            """,
            _to_dto_string(_now_eastern()),
            total_success,
            total_errors,
            sp_exec_id,
        )
        conn.commit()

        logging.info(
            "loadPoleDaylightFlags: %d updated, %d errors.",
            total_success,
            total_errors,
        )

    except Exception as ex:
        logging.error("loadPoleDaylightFlags: run failed: %s", ex)
        if sp_exec_id:
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
                    "loadPoleDaylightFlags: additionally failed to record this run's failure "
                    "(Id=%s): %s -- recovery also failed (original: %s)",
                    sp_exec_id, recording_error, ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()


def load_provisioned_pole_daylight_flags() -> None:
    """
    Same as load_leadsun_pole_daylight_flags() but for provisioned poles.
    Joins PoleTimeZones on ProvisionedPoleId (not VendorPoleId) since
    provisioned rows use ProvisionedPoleId as their PoleTelemetry.PoleId.

    Loops until all unflagged rows are processed (unlike the Leadsun
    function which processes one batch per scheduled run) because provisioned
    poles are event-driven and may accumulate many unflagged rows between runs.

    Requires load_provisioned_pole_timezones() to have already run.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()
    sp_exec_id = None
    total_success = 0
    total_errors = 0
    batch_count = 0

    try:
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "loadProvisionedPoleDaylightFlags",
            ENVIRONMENT,
            start_time,
            PROVISIONED_SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        while True:
            cursor.execute(_FIND_PROVISIONED_UNFLAGGED_SQL, _BATCH_SIZE)
            rows = cursor.fetchall()
            if not rows:
                break

            batch_count += 1
            updates = []
            for i, (pole_id, last_upload, latitude, longitude) in enumerate(rows):
                if i > 0 and i % _PROGRESS_LOG_INTERVAL == 0:
                    logging.info(
                        "loadProvisionedPoleDaylightFlags: computed %d/%d rows in batch %d.",
                        i, len(rows), batch_count,
                    )
                try:
                    is_day, is_day_led, is_day_panel = _compute_daylight_triple(
                        last_upload, latitude, longitude
                    )
                    updates.append((is_day, is_day_led, is_day_panel, pole_id, _to_dto_string(last_upload)))
                except Exception as row_ex:
                    total_errors += 1
                    logging.warning(
                        "loadProvisionedPoleDaylightFlags: failed to compute IsDaylight "
                        "for %s @ %s: %s",
                        pole_id, last_upload, row_ex,
                    )

            if updates:
                success, errors = _write_daylight_updates(
                    cursor, conn, updates, "loadProvisionedPoleDaylightFlags"
                )
                total_success += success
                total_errors += errors

            cursor.execute(
                """
                UPDATE SP_Execution
                SET BatchCount = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?
                WHERE Id = ?
                """,
                batch_count, total_success, total_errors, sp_exec_id,
            )
            conn.commit()

            if len(rows) < _BATCH_SIZE:
                break

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

        logging.info(
            "loadProvisionedPoleDaylightFlags: complete. %d updated, %d errors, %d batch(es).",
            total_success, total_errors, batch_count,
        )

    except Exception as ex:
        logging.error("loadProvisionedPoleDaylightFlags: run failed: %s", ex)
        if sp_exec_id:
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
                    "loadProvisionedPoleDaylightFlags: also failed to record run failure "
                    "(Id=%s): %s (original: %s)",
                    sp_exec_id, recording_error, ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()
