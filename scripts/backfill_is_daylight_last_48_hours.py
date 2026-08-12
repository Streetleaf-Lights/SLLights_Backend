"""
One-off script to fully backfill PoleTelemetry.IsDaylight for ONLY the
last 48 hours -- i.e. specifically the window pole_vitals_loader.py's
Last48Hours period type reads -- rather than waiting for
load_pole_daylight_flags()'s normal, incremental, whole-history backfill
(bounded at _BATCH_SIZE=20000 rows per 30-minute loadLeadsunData cycle)
to naturally work its way through however much unflagged history exists.

Why this is needed at all, given load_pole_daylight_flags() already
processes newest-first: with potentially over a million PoleTelemetry
rows landing in any given 48-hour window (based on ~11,666 rows every
~30 minutes), even newest-first could take most of a day of normal
30-minute cycles to fully cover just that recent window, let alone the
full 6-month history behind it. This script doesn't change that
loader's normal behavior at all -- it just calls it repeatedly, right
now, in a tight loop, checking after each call whether the last-48-hours
window is fully flagged yet, until it is (rather than only one batch's
worth of newest-first progress per normal 30-minute cycle).

Usage (from the Backend/ project root):

    python3 scripts/backfill_is_daylight_last_48_hours.py

Reuses local.settings.json's "Values" (the same file `func start` reads
and the same approach scripts/run_pole_vitals_backfill.py already uses),
so if you've already got that configured for local manual-trigger
testing, this needs no extra setup. Needs SQL_CONNECTION_STRING/
ENVIRONMENT to run load_pole_daylight_flags() itself, plus
LEADSUN_CLIENT_CERT_PEM (and LEADSUN_SERVER_CA_CERT/
LEADSUN_SKIP_HOSTNAME_CHECK, if your setup needs them) -- importing
pole_daylight_flags_loader doesn't itself need these, but
pole_telemetry_loader.py (imported transitively via
shared/pole_vitals_loader.py, which this script also touches to compute
the same 48-hour cutoff pole_vitals_loader.py itself uses) reads
LEADSUN_CLIENT_CERT_PEM eagerly at import time regardless.

If your local machine can't reach the same Azure SQL Server (e.g.
firewall rules only allow Azure-to-Azure traffic), run this instead from
the deployed Function App's Kudu/SSH console (Advanced Tools in the
Portal), where all the same values are already set as real App Settings
-- no local.settings.json needed there at all.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Generous headroom over the ~56 iterations a ~1.12M-row/48-hour window
# would need at _BATCH_SIZE=20000 per call -- exists purely so a genuine
# problem (e.g. is_daylight() failing for the same rows every single
# call, never actually making progress) surfaces as a clear, loud stop
# rather than this script looping unbounded forever.
_MAX_ITERATIONS = 300

# A single iteration failing (e.g. a transient "Communication link
# failure" from a dropped connection mid-batch) shouldn't kill this
# entire, otherwise-successful run and lose all its progress -- each
# retry opens a genuinely fresh connection (load_pole_daylight_flags()
# always does), giving a real chance of recovering from exactly that
# kind of transient blip. But CONSECUTIVE failures, specifically, are a
# different signal from occasional ones -- if it's failing over and
# over with no successful iteration in between, that's a persistent
# problem (sustained connectivity issues, credentials, etc.), not a
# one-off blip, and this should say so and stop well before burning
# through the full _MAX_ITERATIONS budget waiting to find that out.
_MAX_CONSECUTIVE_FAILURES = 5
_RETRY_BACKOFF_SECONDS = 10

# OR, not just IsDaylight IS NULL: mirrors pole_daylight_flags_loader.py's
# own _FIND_UNFLAGGED_SQL, which checks both columns for the same reason
# -- IsDaylightForLedFault was added after IsDaylight already existed
# and was, in practice, already backfilled for this window, so a row
# missing only the newer column must still count as "still pending"
# here too, or this script would report "done" while that column
# remained unflagged.
_COUNT_REMAINING_SQL = """
SELECT COUNT(*)
FROM PoleTelemetry t
JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL)
  AND ptz.WindowsTimeZone IS NOT NULL
  AND t.LastUpload >= ?
  AND t.LastUpload <> ?
"""


def load_local_settings_into_env(project_root: Path = PROJECT_ROOT) -> bool:
    """
    Reads local.settings.json's "Values" into os.environ (only for keys
    not already set -- won't clobber anything explicitly exported in the
    calling shell). Returns False if the file doesn't exist, so the
    caller can fall back to "assume env vars are already set some other
    way" instead of hard-failing.
    """
    settings_path = project_root / "local.settings.json"
    if not settings_path.exists():
        return False

    with open(settings_path) as f:
        settings = json.load(f)

    for key, value in settings.get("Values", {}).items():
        os.environ.setdefault(key, value)
    return True


def refuse_if_prod(environment: str) -> None:
    """Same safety convention as this project's manual HTTP triggers and
    live integration tests: never let a one-off script run against Prod
    by accident."""
    if environment == "Prod":
        raise SystemExit(
            "Refusing to run against ENVIRONMENT=Prod from this script. "
            "Point local.settings.json's ENVIRONMENT at Dev/Staging, or run "
            "this from the deployed environment's own Kudu/SSH console "
            "instead if you specifically mean to target that environment."
        )


def count_remaining_unflagged_in_window(cutoff: str, sentinel: str) -> int:
    """
    How many PoleTelemetry rows within [cutoff, now] still have
    IsDaylight OR IsDaylightForLedFault IS NULL (see
    _COUNT_REMAINING_SQL's own comment for why both, not just the
    first), and a resolvable PoleTimeZones entry -- i.e. rows
    load_pole_daylight_flags() COULD flag but hasn't yet. Mirrors
    _FIND_UNFLAGGED_SQL's own WindowsTimeZone IS NOT NULL / INNER JOIN
    conditions exactly, so a row that can never be flagged at all (no
    resolved timezone yet) doesn't count as "still pending" here either
    -- otherwise this loop would never terminate waiting on rows
    load_pole_daylight_flags() itself would also skip forever.
    """
    from shared.sql_client import get_connection

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(_COUNT_REMAINING_SQL, cutoff, sentinel)
        return cursor.fetchone()[0]
    finally:
        cursor.close()
        conn.close()


def run_backfill_loop(
    cutoff: str,
    sentinel: str,
    load_pole_daylight_flags_fn,
    count_remaining_fn,
    sleep_fn=time.sleep,
) -> None:
    """
    The actual backfill loop, factored out from __main__ so it's
    directly testable without mocking module-level imports or exercising
    the whole script-as-a-module machinery. load_pole_daylight_flags_fn/
    count_remaining_fn are taken as explicit parameters (rather than
    calling load_pole_daylight_flags()/count_remaining_unflagged_in_window()
    directly) specifically so tests can substitute controlled fakes for
    both -- dependency injection for testability, not because either is
    otherwise reused. sleep_fn defaults to the real time.sleep, but tests
    substitute a no-op so retry-with-backoff tests run instantly rather
    than actually pausing for _RETRY_BACKOFF_SECONDS each time.

    Raises SystemExit if _MAX_ITERATIONS or _MAX_CONSECUTIVE_FAILURES is
    hit -- see those constants' own comments for the reasoning behind
    each cap.
    """
    remaining = count_remaining_fn(cutoff, sentinel)
    logging.info("%d unflagged row(s) currently in this window.", remaining)

    iteration = 0
    consecutive_failures = 0
    while remaining > 0:
        iteration += 1
        if iteration > _MAX_ITERATIONS:
            raise SystemExit(
                f"Stopping after {_MAX_ITERATIONS} iterations with {remaining} row(s) "
                "still unflagged in the last-48-hours window -- this is far more than "
                "expected and likely means load_pole_daylight_flags() is failing "
                "repeatedly for the same rows rather than making progress. Check the "
                "ERROR-level log lines above for the actual failure reason before "
                "re-running this script."
            )

        logging.info("Iteration %d: calling load_pole_daylight_flags() ...", iteration)
        try:
            load_pole_daylight_flags_fn()
            consecutive_failures = 0
        except Exception as iteration_error:
            consecutive_failures += 1
            logging.error(
                "Iteration %d failed (%s) -- %d consecutive failure(s) so far.",
                iteration,
                iteration_error,
                consecutive_failures,
            )
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                raise SystemExit(
                    f"Stopping after {consecutive_failures} consecutive failures -- this "
                    "looks like a persistent issue (sustained connectivity problems, "
                    "credentials, etc.), not a one-off transient blip. See the ERROR-level "
                    "log lines above for the actual failure reason."
                )
            logging.info(
                "Waiting %ds before retrying (a fresh connection each retry gives a real "
                "chance of recovering from a transient failure) ...",
                _RETRY_BACKOFF_SECONDS,
            )
            sleep_fn(_RETRY_BACKOFF_SECONDS)
            continue  # retry this same iteration rather than rechecking `remaining` yet

        remaining = count_remaining_fn(cutoff, sentinel)
        logging.info("%d unflagged row(s) remaining in this window.", remaining)

    logging.info(
        "Done -- every PoleTelemetry row in the last 48 hours with a resolvable "
        "timezone now has IsDaylight set. loadPoleVitals' next run should produce a "
        "fully-correct Last48Hours rollup."
    )


if __name__ == "__main__":
    # Without this, load_pole_daylight_flags()'s logging.info()/
    # logging.error() calls are silently swallowed -- there's no Azure
    # Functions runtime here to auto-configure a handler like there is
    # in production.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    found_settings_file = load_local_settings_into_env()
    if not found_settings_file:
        logging.warning(
            "local.settings.json not found at %s -- assuming required env vars "
            "(SQL_CONNECTION_STRING, ENVIRONMENT, LEADSUN_CLIENT_CERT_PEM, and "
            "LEADSUN_SERVER_CA_CERT/LEADSUN_SKIP_HOSTNAME_CHECK if needed) are "
            "already set some other way.",
            PROJECT_ROOT / "local.settings.json",
        )

    environment = os.environ.get("ENVIRONMENT", "Dev")
    refuse_if_prod(environment)

    from shared.pole_daylight_flags_loader import load_pole_daylight_flags
    from shared.pole_telemetry_loader import _MISSING_LAST_UPLOAD_SENTINEL
    from shared.pole_vitals_loader import _compute_cutoff
    from shared.datetime_utils import now_eastern

    # The SAME 48-hour cutoff pole_vitals_loader.py's own Last48Hours
    # period type computes -- this script's whole point is making sure
    # THAT specific window is fully flagged, so it needs to describe
    # exactly the same window, not an approximation of it.
    cutoff = _compute_cutoff(now_eastern(), "Last48Hours", backfill=False)
    logging.info(
        "Backfilling IsDaylight for PoleTelemetry rows since %s (ENVIRONMENT=%s) ...",
        cutoff,
        environment,
    )

    run_backfill_loop(
        cutoff,
        _MISSING_LAST_UPLOAD_SENTINEL,
        load_pole_daylight_flags,
        count_remaining_unflagged_in_window,
    )
