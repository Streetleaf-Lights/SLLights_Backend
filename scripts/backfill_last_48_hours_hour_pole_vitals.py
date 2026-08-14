"""
One-off script to run
pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles() --
ensures EVERY pole has up to 48 hourly "Hour" PoleVitals rows covering
its own last 48 hours of real activity, ending at that SAME pole's own
latest telemetry, no matter how old that telemetry is.

A broader relative of scripts/backfill_latest_hour_pole_vitals.py, not a
replacement for it: that one only ever touches a pole's single newest
hour. This one exists for a specific, related need: pole_vitals_api.py's
GetPoleVitalsByPeriod now anchors its own 48-hour display window to each
pole's latest telemetry too -- but that only shows something useful for
an offline pole if PoleVitals rows genuinely exist across that pole's
own last 48 hours of activity in the first place. See
backfill_last_48_hours_of_hour_for_all_poles()'s own docstring for the
full reasoning.

Usage (from the Backend/ project root):

    python3 scripts/backfill_last_48_hours_hour_pole_vitals.py

Reuses local.settings.json's "Values" (the same file `func start` reads),
so if you've already got that configured for local manual-trigger testing,
this needs no extra setup. Only needs SQL_CONNECTION_STRING/ENVIRONMENT to
run backfill_last_48_hours_of_hour_for_all_poles() itself, but importing
pole_vitals_loader pulls in pole_telemetry_loader -> leadsun_client, which
reads LEADSUN_CLIENT_CERT_PEM eagerly at import time even though this
script never calls fetch_lamps() -- so that (and LEADSUN_SERVER_CA_CERT/
LEADSUN_SKIP_HOSTNAME_CHECK, if your setup needs them) must be present
too, or the import itself will fail. Same situation as
scripts/backfill_latest_hour_pole_vitals.py and
scripts/run_pole_vitals_backfill.py.

If your local machine can't reach the same Azure SQL Server (e.g. firewall
rules only allow Azure-to-Azure traffic), run this instead from the
deployed Function App's Kudu/SSH console (Advanced Tools in the Portal),
where all the same values are already set as real App Settings -- no
local.settings.json needed there at all.

This does more work per pole than the single-bucket backfill (up to 48
rows instead of 1), across every pole at once in a single MERGE -- expect
this to take meaningfully longer to run, especially the first time.
"""

import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


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


if __name__ == "__main__":
    # Without this, backfill_last_48_hours_of_hour_for_all_poles()'s
    # logging.info()/logging.error() calls are silently swallowed --
    # there's no Azure Functions runtime here to auto-configure a
    # handler like there is in production.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    found_settings_file = load_local_settings_into_env()
    if not found_settings_file:
        logging.warning(
            "local.settings.json not found at %s -- assuming required env vars "
            "(SQL_CONNECTION_STRING, ENVIRONMENT, LEADSUN_CLIENT_CERT_PEM) are "
            "already set some other way.",
            PROJECT_ROOT / "local.settings.json",
        )

    environment = os.environ.get("ENVIRONMENT", "Dev")
    refuse_if_prod(environment)

    from shared.pole_vitals_loader import backfill_last_48_hours_of_hour_for_all_poles

    logging.info(
        "Running last-48-hours-of-Hour-per-pole backfill (every pole's own last "
        "48 hours of activity, ending at its own latest telemetry, regardless of "
        "age) against ENVIRONMENT=%s ...",
        environment,
    )
    backfill_last_48_hours_of_hour_for_all_poles()
    logging.info(
        "Backfill complete. Every pole with at least one PoleTelemetry reading now "
        "has up to 48 hourly Hour PoleVitals rows covering its own last 48 hours "
        "of real activity."
    )
