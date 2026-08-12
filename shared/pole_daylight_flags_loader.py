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

# Bounds how many not-yet-flagged rows get processed per run. Mainly
# relevant right after IsDaylight is first added (when potentially the
# entire existing PoleTelemetry history is unflagged, needing several
# runs to fully backfill) -- ongoing operation only ever has a small
# number of newly-arrived, not-yet-flagged rows per 30-minute cycle
# (loadLeadsunData's current schedule), well under this cap.
_BATCH_SIZE = 20000

# How often (in rows processed) to log a progress line during the
# per-row computation loop below -- confirmed in practice as genuinely
# necessary, not just nice-to-have: a full _BATCH_SIZE batch, now with
# up to two is_daylight() calls per row instead of one (see
# IsDaylightForLedFault's own grace-period check), can run long enough
# with zero log output in between to look indistinguishable from a
# genuine hang, especially while ALSO troubleshooting an unrelated, real
# connection issue elsewhere in the same run.
_PROGRESS_LOG_INTERVAL = 5000

# How much earlier AND later than a reading's own timestamp to ALSO
# check is_daylight() for, specifically for IsLedFaultFlag's benefit
# (see IsDaylightForLedFault's own comment on _UPDATE_IS_DAYLIGHT_SQL
# below for the full reasoning) -- confirmed in practice as long enough
# to cover the real lamp-response lag at BOTH the sunset transition (a
# lamp that's slow to turn ON) and the sunrise transition (a lamp that
# turns OFF slightly early, having sensed approaching dawn light before
# the astronomical sunrise moment), without extending so far in either
# direction that it would meaningfully weaken the fault check itself.
_LED_FAULT_GRACE_PERIOD = timedelta(hours=1)

# How long a pole must have ALREADY been in daylight before
# IsPanelFaultFlag starts expecting it to be producing -- gives the
# panel time to physically warm up right after sunrise before zero
# output counts as a fault.
_PANEL_FAULT_SUNRISE_WARMUP_PERIOD = timedelta(hours=1)

# How long before sunset IsPanelFaultFlag should ALSO stop expecting a
# pole to still be producing -- gives the panel a symmetric grace period
# to wind down before actual sunset, mirroring the warmup period above.
# Together, these two make IsDaylightForPanelFault an AND across three
# checks (now, before, after) -- the mirror-image SHAPE of
# IsDaylightForLedFault's own OR across the same three checks, but with
# the opposite operator and opposite purpose: LED's OR WIDENS the window
# where zero lamp power is exempted from a fault; Panel's AND NARROWS
# the window where zero panel output is EXPECTED to produce one.
_PANEL_FAULT_SUNSET_WINDDOWN_PERIOD = timedelta(hours=1)

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
#
# "OR t.IsDaylightForLedFault IS NULL": IsDaylightForLedFault was added
# to PoleTelemetry AFTER IsDaylight already existed and was, in
# practice, already fully backfilled for the operationally-relevant
# window (via scripts/backfill_is_daylight_last_48_hours.py). Without
# this OR, a row that already has IsDaylight set would never be
# revisited to also compute the new column, even though this loader's
# own main loop (below) always computes both together -- the same class
# of "adding CountyFips to Poles did nothing for a pole that already had
# a PoleTimeZones row" gap pole_timezones_loader.py hit for the exact
# same underlying reason. This way, simply re-running the EXISTING
# backfill script (no new one needed) naturally picks up every row
# missing either column, not just rows missing both. Same reasoning
# applies to "OR t.IsDaylightForPanelFault IS NULL" -- added later still,
# same gap, same fix.
#
# ORDER BY ... DESC (most recent first), not oldest-first: right after
# IsDaylight is (re-)added, potentially every existing PoleTelemetry row
# is unflagged at once -- oldest-first would spend however many cycles
# it takes working through old history before ever reaching today's
# data, meaning pole_vitals_loader.py's IsLedFault (which only ever
# looks at a small recent window) would keep falling through to the
# "not yet known, treat as night" case for CURRENT readings the whole
# time. Newest-first backfills the operationally-relevant window first,
# leaving only old history to catch up on whenever it gets there.
# t.LastUpload <> the sentinel value matters here specifically: a real
# production bug, not theoretical -- '9999-12-31 23:59:59.999' marks "no
# real telemetry yet" throughout this project (already excluded in, e.g.,
# the backfill script's own _COUNT_REMAINING_SQL), but was missing here.
# Without it, this loader would keep trying (and failing) to compute
# daylight for a sentinel row forever, since a sentinel row's IsDaylight
# columns can never get set, so it never stops matching the "still
# unflagged" condition above -- wasting _BATCH_SIZE capacity every run on
# rows that will never succeed, rather than genuinely wasting effort just
# once. Also a hard requirement, not just wasteful: is_daylight()'s own
# arithmetic (± _LED_FAULT_GRACE_PERIOD/_PANEL_FAULT_SUNRISE_WARMUP_PERIOD)
# can overflow Python's own datetime.max when applied to a date already
# this extreme -- confirmed in practice as the exact cause of a "date
# value out of range" failure for this exact sentinel value once the
# grace period's "+1 hour" (forward-looking) arithmetic was introduced.
_FIND_UNFLAGGED_SQL = """
SELECT TOP (?) t.LocationId, t.LastUpload, ptz.Latitude, ptz.Longitude
FROM PoleTelemetry t
JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL OR t.IsDaylightForPanelFault IS NULL)
  AND ptz.WindowsTimeZone IS NOT NULL
  AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
ORDER BY t.LastUpload DESC
"""

# IsDaylightForLedFault: TRUE if it was daylight at this reading's exact
# moment, OR if it was daylight _LED_FAULT_GRACE_PERIOD before that
# moment, OR if it will be daylight _LED_FAULT_GRACE_PERIOD after that
# moment -- a deliberately different definition from the strict
# IsDaylight above, used ONLY by pole_vitals_loader.py's IsLedFaultFlag.
# Symmetric on both sides: the "before" check catches a lamp that's slow
# to turn ON right after sunset; the "after" check catches the mirror
# case at the other end of the night -- a lamp that turns OFF slightly
# early, having sensed approaching dawn light before the astronomical
# sunrise moment.
#
# IsDaylightForPanelFault: TRUE if it's daylight NOW, AND it was ALSO
# already daylight _PANEL_FAULT_SUNRISE_WARMUP_PERIOD before now, AND it
# will STILL be daylight _PANEL_FAULT_SUNSET_WINDDOWN_PERIOD from now --
# i.e. "has been daylight continuously for at least the warmup period,
# and won't lose daylight again within the winddown period", used ONLY
# by pole_vitals_loader.py's IsPanelFaultFlag. Unlike IsDaylightForLedFault
# above, this doesn't extend daylight's boundaries outward in either
# direction -- it NARROWS the window where IsPanelFaultFlag expects
# output, from BOTH ends: delaying the start (sunrise warmup) and
# bringing forward the end (sunset winddown). This one check alone also
# correctly subsumes the plain nighttime case: at night, is_daylight(now)
# is already False, so IsDaylightForPanelFault is False too, without
# needing a separate "AND it's not night" condition anywhere.
#
# Why three separate columns, not one shared value: a lamp doesn't
# always turn on the INSTANT the sun crosses the sunset threshold --
# confirmed in practice, a real lamp was still off 30 minutes after
# IsDaylight flipped to 0, then correctly on by the reading after that.
# Extending IsDaylight itself to cover that lag would fix IsLedFault's
# false positive, but would break IsPanelFault in the opposite
# direction: IsPanelFault checks the OPPOSITE condition (IsDaylight = 0
# means "don't require panel output"), so an IsDaylight that stays "1"
# past real sunset would incorrectly start REQUIRING panel output during
# an hour that's actually already dark. Each fault flag needs its own
# daylight definition, tuned to its own hardware's own real-world
# response characteristics -- neither can safely reuse another's.
_UPDATE_IS_DAYLIGHT_SQL = """
UPDATE PoleTelemetry
SET IsDaylight = ?, IsDaylightForLedFault = ?, IsDaylightForPanelFault = ?
WHERE LocationId = ? AND LastUpload = ?
"""


def load_pole_daylight_flags() -> None:
    """
    Computes and caches IsDaylight, IsDaylightForLedFault, AND
    IsDaylightForPanelFault on PoleTelemetry rows missing any one of
    them, using PoleTimeZones' Latitude/Longitude for each LocationId --
    deliberately NOT PoleTelemetry's own Longitude/Latitude columns --
    and each row's own LastUpload timestamp.

    IsDaylight is the strict, exact-moment answer -- not read directly by
    either fault flag anymore, but still computed and stored as the
    common building block both of the other two are derived from.
    IsDaylightForLedFault and IsDaylightForPanelFault are each a
    deliberately different, more forgiving definition, tuned to that one
    specific fault flag's own hardware response characteristics -- see
    _UPDATE_IS_DAYLIGHT_SQL's own comment for why these need to be three
    separate columns, not one shared value.

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
        # inside SQL) and collect successes for a bulk write.
        #
        # This loop now does up to TWICE the is_daylight() calls per row
        # it used to (see IsDaylightForLedFault's own grace-period check
        # below), with no network I/O of its own in between -- purely
        # CPU-bound. Confirmed in practice this can run long enough,
        # with no log output at all in between, to look indistinguishable
        # from a genuine hang (e.g. while troubleshooting an unrelated,
        # real connection issue elsewhere in this same run) -- the
        # periodic progress line below exists specifically so a long
        # silent stretch here is never ambiguous with one again.
        updates = []
        total_unflagged = len(unflagged)
        for index, (location_id, last_upload, latitude, longitude) in enumerate(
            unflagged, start=1
        ):
            if index % _PROGRESS_LOG_INTERVAL == 0:
                logging.info(
                    "loadPoleDaylightFlags: computed %d/%d reading(s) so far ...",
                    index,
                    total_unflagged,
                )
            try:
                daylight = is_daylight(last_upload, latitude, longitude)
                # IsLedFaultFlag's own, more forgiving daylight
                # definition -- daylight NOW, or daylight
                # _LED_FAULT_GRACE_PERIOD before now (catches a lamp
                # that's slow to turn ON right after sunset), or daylight
                # _LED_FAULT_GRACE_PERIOD from now (catches the mirror
                # case: a lamp that turns OFF slightly early, sensing
                # approaching dawn light before the astronomical sunrise
                # moment). Deliberately `daylight or ... or ...`
                # (short-circuits as soon as any check is already True)
                # rather than computing every check unconditionally --
                # confirmed daylight from an earlier check already
                # answers the question, no need for is_daylight()'s real
                # astronomical math again. Worst case (a reading deep in
                # the night, nowhere near either transition) still runs
                # all three checks, not just the original two.
                daylight_for_led_fault = (
                    daylight
                    or is_daylight(
                        last_upload - _LED_FAULT_GRACE_PERIOD, latitude, longitude
                    )
                    or is_daylight(
                        last_upload + _LED_FAULT_GRACE_PERIOD, latitude, longitude
                    )
                )
                # IsPanelFaultFlag's own, more forgiving daylight
                # definition -- daylight NOW, AND it was ALSO already
                # daylight _PANEL_FAULT_SUNRISE_WARMUP_PERIOD before now,
                # AND it will STILL be daylight
                # _PANEL_FAULT_SUNSET_WINDDOWN_PERIOD from now.
                # Deliberately `daylight and ... and ...` (short-circuits
                # to False as soon as any check fails -- no need to
                # compute the remaining ones) rather than an `or` chain
                # like IsLedFaultFlag's own check above: this one
                # NARROWS when daylight counts for panel-output purposes
                # from BOTH ends, it doesn't extend daylight's boundaries
                # outward. At night, this stops after the first check;
                # within the first hour after sunrise, it stops after
                # the second; the third (sunset winddown) only ever runs
                # once it's confirmed both daylight now AND past warmup.
                daylight_for_panel_fault = (
                    daylight
                    and is_daylight(
                        last_upload - _PANEL_FAULT_SUNRISE_WARMUP_PERIOD,
                        latitude,
                        longitude,
                    )
                    and is_daylight(
                        last_upload + _PANEL_FAULT_SUNSET_WINDDOWN_PERIOD,
                        latitude,
                        longitude,
                    )
                )
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
                updates.append(
                    (
                        daylight,
                        daylight_for_led_fault,
                        daylight_for_panel_fault,
                        location_id,
                        _to_dto_string(last_upload),
                    )
                )
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
        #
        # Deliberately NOT using cursor.fast_executemany = True here,
        # despite the real performance cost of that choice (this loader
        # can process tens of thousands of rows per run). fast_executemany
        # infers a fixed buffer size for variable-length string parameters
        # from the batch -- LocationId varies in length across poles --
        # and depending on pyodbc version, a row whose LocationId doesn't
        # fit the inferred size can get silently mis-bound: executemany()
        # raises no exception (so this still looks like success, and
        # total_success still gets incremented), but the WHERE clause
        # then matches zero rows for that write. This was confirmed in
        # practice: a direct, single-row UPDATE with values copied
        # straight from the table matched correctly, but the batched
        # write reported success while changing nothing -- exactly what
        # this pyodbc behavior produces, and fast_executemany was the
        # one difference between those two paths.
        if updates:
            try:
                cursor.executemany(_UPDATE_IS_DAYLIGHT_SQL, updates)
                total_success += len(updates)
                # cursor.rowcount after executemany() is driver-dependent
                # (some ODBC drivers report the total across all batched
                # statements, others -1/unknown, never just plain 0 unless
                # genuinely zero rows matched) -- not reliable enough to
                # base success/failure on in general, but a clean 0 here
                # specifically is worth a loud warning: that exact
                # symptom (no exception raised, but the WHERE clause
                # silently matched nothing) is the failure mode this
                # whole function has already hit twice now (once from a
                # pyodbc DATETIMEOFFSET binding gotcha, fixed via
                # _to_dto_string() above; once from fast_executemany's
                # own binding behavior, just removed above) -- so if it
                # recurs for a third, different reason, this should
                # surface it immediately rather than silently reporting
                # success again. Worth noting this check itself can miss
                # the exact failure it's meant to catch, if the driver
                # reports -1/unknown instead of a real 0 -- it's a
                # backstop, not a guarantee.
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
                for (
                    daylight,
                    daylight_for_led_fault,
                    daylight_for_panel_fault,
                    location_id,
                    last_upload,
                ) in updates:
                    try:
                        cursor.execute(
                            _UPDATE_IS_DAYLIGHT_SQL,
                            daylight,
                            daylight_for_led_fault,
                            daylight_for_panel_fault,
                            location_id,
                            last_upload,
                        )
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
            # Deliberately a SEPARATE try/except, and a FRESH connection,
            # not the one already in `conn`/`cursor` above. If `ex`
            # itself was a connection-level failure (e.g. SQLSTATE 08S01,
            # "Communication link failure") -- exactly the kind of
            # failure this whole except block exists to record -- then
            # `conn`/`cursor` are themselves the broken resource, and
            # reusing them here is close to guaranteed to fail too. That
            # SECOND failure would then propagate instead of `ex`,
            # replacing a specific, useful error ("communication link
            # failure while committing") with a confusing, unrelated one
            # ("communication link failure while trying to log the first
            # communication link failure") -- exactly what happened in
            # practice before this fix. A fresh connection gives this
            # attempt an actual chance of succeeding when the original
            # one is what died; wrapping it means even if THIS also
            # fails, `ex` -- the original, more informative exception --
            # is still what actually gets raised below, not this one.
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
                    "loadPoleDaylightFlags: additionally failed to record this run's "
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
