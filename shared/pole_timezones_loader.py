import os
import logging

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

# Two distinct meanings that used to share one SOURCE_NAME constant:
# EXECUTION_SOURCE tracks which pipeline this loader's own run belongs to
# (SP_Execution.Source) -- still "Leadsun", since this loader still runs
# as part of that pipeline's Models -> Telemetry -> TimeZones ->
# DaylightFlags -> Vitals load order, regardless of where the coordinates
# it resolves come from. COORDINATE_SOURCE tracks where each row's own
# Longitude/Latitude actually came from (PoleTimeZones.Source) -- now
# "CountyTimeZones" (a representative coordinate for that pole's county,
# via Poles.CountyFips), replacing "Airtable" (Poles.Lat/Poles.Long
# directly) entirely -- see _RESOLVE_FROM_COUNTY_SQL's own comment for
# why.
EXECUTION_SOURCE = "Leadsun"
COORDINATE_SOURCE = "CountyTimeZones"

# Resolves and caches each not-yet-seen LocationId's timezone via its
# pole's OWN county (Poles.CountyFips, itself sourced from Airtable's
# "CountyFips" field -- see shared/poles_loader.py's own comments on
# AIRTABLE_POLES_FIELDS and _clean_county_fips()), joined against
# CountyTimeZones -- a static, pre-computed reference table (see
# "sql/CountyTimeZones/Create tbl CountyTimeZones.sql" for exactly how
# every one of ITS rows was computed) -- rather than Poles.Lat/Poles.Long
# directly + a per-pole timezonefinder computation, which this replaces
# entirely.
#
# Why the switch: Poles.Lat/Poles.Long are frequently missing or
# incorrect in practice, while Poles.CountyFips is reliably populated.
# This is an explicit, deliberate tradeoff, not a strict improvement in
# every respect -- a handful of US counties genuinely span more than one
# timezone, which a single FIPS->timezone row cannot represent, so a
# pole in one of those split counties could resolve to the wrong side of
# that split. Accepted given the alternative (Lat/Long) was failing more
# often in practice than this coarser, county-level approximation would.
#
# No per-row Python computation needed at all anymore (unlike the
# timezonefinder-based version this replaced) -- the entire resolution
# is now a single, set-based SQL join, since CountyTimeZones already has
# every county's timezone pre-computed. This is why this whole loader is
# now a single MERGE statement instead of a Python loop calling
# resolve_windows_timezone() per row.
#
# INNER JOIN CountyTimeZones (not LEFT): a pole whose CountyFips is NULL,
# or doesn't match any row in CountyTimeZones (a typo, an invalid code,
# or a genuinely new/unusual FIPS this table doesn't have yet), simply
# can't be resolved via this path at all -- there's nothing to fall back
# to since Poles.Lat/Poles.Long is no longer used here. Such a pole is
# silently excluded from THIS query's results, which is why
# _COUNT_UNRESOLVABLE_SQL exists separately below: to make that gap
# visible in logs rather than silent.
#
# p.LocationId IS NOT NULL matters here specifically: a pole can exist
# in Poles before it's linked to a real Leadsun device (LocationId not
# yet assigned) -- such a pole could never appear in PoleTelemetry, so
# resolving/inserting a timezone row for it would be premature.
#
# The inner SELECT's ROW_NUMBER()/"WHERE rn = 1" wrapper deduplicates by
# LocationId before the MERGE ever sees it -- see
# _RESOLVE_FROM_COUNTY_BACKFILL_SQL's own comment for the full story:
# Poles is keyed by Id, not LocationId, so two different Poles rows can
# share the same LocationId (a real Airtable data quality issue,
# confirmed in production), which would otherwise make MERGE fail
# outright with "attempted to UPDATE or DELETE the same row more than
# once" (error 8672). This loader's own "not already resolved" filter
# has so far masked this for existing poles (whichever duplicate got
# there first via the old Lat/Long-based system already has a
# PoleTimeZones row, so this MERGE simply never revisits either one) --
# but a BRAND NEW pair of duplicate LocationIds, appearing for the first
# time after this switch, would hit the same failure here without this
# same fix.
_RESOLVE_FROM_COUNTY_SQL = """
MERGE PoleTimeZones AS target
USING (
    SELECT LocationId, Longitude, Latitude, IanaTimeZone, WindowsTimeZone
    FROM (
        SELECT
            p.LocationId,
            ctz.Longitude,
            ctz.Latitude,
            ctz.IanaTimeZone,
            ctz.WindowsTimeZone,
            ROW_NUMBER() OVER (PARTITION BY p.LocationId ORDER BY p.Id) AS rn
        FROM Poles p
        LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId
        JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
        WHERE ptz.LocationId IS NULL
          AND p.LocationId IS NOT NULL
    ) AS deduped
    WHERE rn = 1
) AS source
ON target.LocationId = source.LocationId
WHEN MATCHED THEN UPDATE SET
    Longitude       = source.Longitude,
    Latitude        = source.Latitude,
    IanaTimeZone    = source.IanaTimeZone,
    WindowsTimeZone = source.WindowsTimeZone,
    Source          = ?,
    SP_ExecId       = ?
WHEN NOT MATCHED THEN
    INSERT (LocationId, Longitude, Latitude, IanaTimeZone, WindowsTimeZone, Source, SP_ExecId)
    VALUES (source.LocationId, source.Longitude, source.Latitude, source.IanaTimeZone,
            source.WindowsTimeZone, ?, ?);
"""

# Same resolution logic as _RESOLVE_FROM_COUNTY_SQL above, but WITHOUT
# the "not already in PoleTimeZones" restriction -- re-resolves and
# OVERWRITES every pole with a resolvable CountyFips, regardless of
# whether it already has a PoleTimeZones row.
#
# Why this needs to exist at all, given the normal MERGE already only
# processes not-yet-seen poles: this project's poles were already
# resolved via the OLD Lat/Long + timezonefinder approach for months
# before Poles.CountyFips existed at all. That means virtually every
# pole already has a PoleTimeZones row today -- so the normal MERGE's
# own "ptz.LocationId IS NULL" condition excludes almost everything,
# and adding CountyFips to Poles changes nothing for a pole that was
# already resolved, no matter how accurate its county data now is. This
# variant is the one-time fix for that: re-resolve everything via county
# once, then the normal, non-backfill MERGE's "only touch new poles"
# behavior is the right ongoing behavior again from that point on.
#
# The inner SELECT's ROW_NUMBER()/"WHERE rn = 1" wrapper exists for a
# real, confirmed-in-production reason: Poles is keyed by Id (the
# Airtable record id), not LocationId, so nothing stops two DIFFERENT
# Poles rows from sharing the same LocationId (a genuine Airtable data
# quality issue -- duplicate/misassigned location ids). Without
# deduplicating first, the USING subquery could produce two source rows
# for the same LocationId, and MERGE correctly refuses to guess which
# one should win, failing outright with "The MERGE statement attempted
# to UPDATE or DELETE the same row more than once" (error 8672) --
# exactly what happened in practice before this fix. The normal,
# non-backfill MERGE below was never observed hitting this, but only
# because by the time CountyFips existed, every affected LocationId
# already had a PoleTimeZones row from the old Lat/Long-based system,
# so its own "not already resolved" filter happened to exclude both
# duplicates -- masking the same underlying data issue, not fixing it.
# Ordering by p.Id ascending is an arbitrary but deterministic tiebreak
# (this doesn't know or guess which of two duplicate poles is "correct"
# -- that's a Poles data quality question, not one this loader can
# answer), paired with _COUNT_DUPLICATE_LOCATION_IDS_SQL below to
# surface the issue rather than silently resolve around it forever.
_RESOLVE_FROM_COUNTY_BACKFILL_SQL = """
MERGE PoleTimeZones AS target
USING (
    SELECT LocationId, Longitude, Latitude, IanaTimeZone, WindowsTimeZone
    FROM (
        SELECT
            p.LocationId,
            ctz.Longitude,
            ctz.Latitude,
            ctz.IanaTimeZone,
            ctz.WindowsTimeZone,
            ROW_NUMBER() OVER (PARTITION BY p.LocationId ORDER BY p.Id) AS rn
        FROM Poles p
        JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
        WHERE p.LocationId IS NOT NULL
    ) AS deduped
    WHERE rn = 1
) AS source
ON target.LocationId = source.LocationId
WHEN MATCHED THEN UPDATE SET
    Longitude       = source.Longitude,
    Latitude        = source.Latitude,
    IanaTimeZone    = source.IanaTimeZone,
    WindowsTimeZone = source.WindowsTimeZone,
    Source          = ?,
    SP_ExecId       = ?
WHEN NOT MATCHED THEN
    INSERT (LocationId, Longitude, Latitude, IanaTimeZone, WindowsTimeZone, Source, SP_ExecId)
    VALUES (source.LocationId, source.Longitude, source.Latitude, source.IanaTimeZone,
            source.WindowsTimeZone, ?, ?);
"""

# Counts poles that (per the comment on _RESOLVE_FROM_COUNTY_SQL above)
# the MERGE above could never resolve at all -- not yet in PoleTimeZones,
# AND either missing CountyFips entirely or holding a value that doesn't
# match any row in CountyTimeZones. Purely diagnostic (never written
# anywhere) -- exists so this gap shows up as a specific, actionable log
# line instead of just a lower-than-expected TotalSuccessfulRecords
# count with no indication of why.
_COUNT_UNRESOLVABLE_SQL = """
SELECT COUNT(*)
FROM Poles p
LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId
LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
WHERE ptz.LocationId IS NULL
  AND p.LocationId IS NOT NULL
  AND ctz.FIPS IS NULL
"""

# Backfill counterpart of _COUNT_UNRESOLVABLE_SQL -- no "not already in
# PoleTimeZones" restriction, matching _RESOLVE_FROM_COUNTY_BACKFILL_SQL's
# own scope: every pole with no resolvable CountyFips, not just
# never-before-seen ones.
_COUNT_UNRESOLVABLE_BACKFILL_SQL = """
SELECT COUNT(*)
FROM Poles p
LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
WHERE p.LocationId IS NOT NULL
  AND ctz.FIPS IS NULL
"""

# Counts distinct LocationIds claimed by more than one Poles row -- a
# genuine Airtable data quality issue (Poles is keyed by its own Id, not
# LocationId, so nothing prevents two different pole records from
# sharing one), confirmed in production as the root cause of a real
# MERGE failure (error 8672, "attempted to UPDATE or DELETE the same row
# more than once"). The MERGE statements above now defend against this
# via ROW_NUMBER() deduplication (see their own comments), so this
# doesn't block them from running -- but it silently picks whichever
# duplicate has the lowest Poles.Id as an arbitrary tiebreak, which is
# not the same as the underlying data actually being correct. Purely
# diagnostic (never written anywhere) -- exists so this data quality
# issue stays visible in logs rather than being silently papered over
# forever.
_COUNT_DUPLICATE_LOCATION_IDS_SQL = """
SELECT COUNT(*)
FROM (
    SELECT LocationId
    FROM Poles
    WHERE LocationId IS NOT NULL
    GROUP BY LocationId
    HAVING COUNT(*) > 1
) AS duplicates
"""


def load_pole_timezones(backfill: bool = False) -> None:
    """
    Resolves and caches each not-yet-seen LocationId's timezone (via its
    pole's own county -- see _RESOLVE_FROM_COUNTY_SQL's own comment for
    the full reasoning and the Lat/Long-based approach this replaced)
    into PoleTimeZones, for pole_vitals_loader.py's per-pole Hour/Day/
    Last48Hours bucketing and pole_daylight_flags_loader.py's sunrise/
    sunset calculations.

    Only resolves LocationIds NOT ALREADY in PoleTimeZones -- poles are
    stationary, so a location's timezone never changes once resolved,
    making this a one-time-per-pole cost rather than something to redo
    every 30-minute cycle. A single set-based MERGE, not a per-row Python
    loop -- unlike the timezonefinder-based version this replaced, there
    is no per-row computation left to do at all; CountyTimeZones already
    has every possible answer pre-computed, so this is exactly the kind
    of bulk, all-at-once operation SQL itself is suited for.

    Set backfill=True to instead RE-resolve and overwrite EVERY pole with
    a resolvable CountyFips, regardless of whether it already has a
    PoleTimeZones row -- see _RESOLVE_FROM_COUNTY_BACKFILL_SQL's own
    comment for why this exists: this project's poles were already
    resolved via the old Lat/Long-based approach for months before
    Poles.CountyFips existed, so without this, adding/correcting
    CountyFips values does nothing for any pole that already has a
    PoleTimeZones row -- which, in practice, is nearly all of them. This
    is a one-time (or run-again-if-CountyFips-values-get-corrected-later)
    operation, not part of the normal scheduled loadLeadsunData cycle --
    see scripts/run_pole_timezones_backfill.py for how to invoke it.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()

    sp_exec_id = None
    total_success = 0
    total_errors = 0

    resolve_sql = _RESOLVE_FROM_COUNTY_BACKFILL_SQL if backfill else _RESOLVE_FROM_COUNTY_SQL
    count_unresolvable_sql = _COUNT_UNRESOLVABLE_BACKFILL_SQL if backfill else _COUNT_UNRESOLVABLE_SQL

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

        # 2. Resolve every eligible LocationId whose pole's county is
        # known, in one set-based MERGE -- "eligible" meaning
        # "not-yet-seen" normally, or "every pole" when backfill=True.
        cursor.execute(
            resolve_sql,
            COORDINATE_SOURCE,
            sp_exec_id,
            COORDINATE_SOURCE,
            sp_exec_id,
        )
        total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        logging.info(
            "loadPoleTimeZones: resolved %d LocationId(s) via CountyTimeZones%s.",
            total_success,
            " (backfill)" if backfill else "",
        )

        # 3. Separately report anything the MERGE above could never have
        # resolved at all (see _COUNT_UNRESOLVABLE_SQL's own comment) --
        # diagnostic only, not counted as an error (there's nothing this
        # run itself did wrong; it's a Poles data-completeness gap).
        cursor.execute(count_unresolvable_sql)
        unresolvable_count = cursor.fetchone()[0]
        if unresolvable_count > 0:
            logging.warning(
                "loadPoleTimeZones: %d pole(s) have a LocationId but no resolvable "
                "CountyFips (missing entirely, or not found in CountyTimeZones) -- "
                "these will keep falling back to the default timezone in "
                "PoleVitals/daylight calculations until Poles.CountyFips is "
                "corrected for them.",
                unresolvable_count,
            )

        # 3b. Separately report LocationIds claimed by more than one
        # Poles row (see _COUNT_DUPLICATE_LOCATION_IDS_SQL's own comment)
        # -- the MERGE above already defends against this via
        # deduplication so it still completes, but an arbitrary tiebreak
        # (lowest Poles.Id) picking a "winner" isn't the same as the
        # underlying Poles data actually being correct.
        cursor.execute(_COUNT_DUPLICATE_LOCATION_IDS_SQL)
        duplicate_location_id_count = cursor.fetchone()[0]
        if duplicate_location_id_count > 0:
            logging.warning(
                "loadPoleTimeZones: %d LocationId(s) are claimed by more than one "
                "Poles row -- resolved using an arbitrary tiebreak (lowest Poles.Id), "
                "but this is a Poles data quality issue worth correcting at the "
                "source rather than relying on that tiebreak indefinitely.",
                duplicate_location_id_count,
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
                    "loadPoleTimeZones: additionally failed to record this run's "
                    "failure in SP_Execution (Id=%s): %s -- that row will be left "
                    "with EndDateTime still NULL. The ORIGINAL failure (%s) is "
                    "what's actually raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()
