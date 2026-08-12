"""
One-off script to run a full RE-resolution of PoleTimeZones -- i.e.
pole_timezones_loader.load_pole_timezones(backfill=True) -- outside of
the normal loadLeadsunData timer cycle, which only resolves LocationIds
that don't already have a PoleTimeZones row.

Why this is needed at all: this project's poles were resolved via the
old Lat/Long + timezonefinder approach for months before Poles.CountyFips
existed. That means virtually every pole already has a PoleTimeZones
row today -- so simply adding (or correcting) CountyFips values on Poles
changes nothing on its own; the normal, non-backfill MERGE only touches
poles it's never seen before. This script re-resolves and OVERWRITES
every pole with a resolvable CountyFips, once, so the whole fleet
actually starts using county-based resolution instead of whatever the
old Lat/Long-based computation happened to produce.

Usage (from the Backend/ project root):

    python3 scripts/run_pole_timezones_backfill.py

Reuses local.settings.json's "Values" (the same file `func start` reads,
and the same approach scripts/run_pole_vitals_backfill.py already uses),
so if you've already got that configured for local manual-trigger
testing, this needs no extra setup. Only needs SQL_CONNECTION_STRING/
ENVIRONMENT -- unlike run_pole_vitals_backfill.py, importing
pole_timezones_loader does NOT transitively pull in the Leadsun client,
so no LEADSUN_CLIENT_CERT_PEM/LEADSUN_SERVER_CA_CERT is needed here.

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
    # Without this, load_pole_timezones()'s logging.info()/logging.error()
    # calls are silently swallowed -- there's no Azure Functions runtime
    # here to auto-configure a handler like there is in production.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    found_settings_file = load_local_settings_into_env()
    if not found_settings_file:
        logging.warning(
            "local.settings.json not found at %s -- assuming required env vars "
            "(SQL_CONNECTION_STRING, ENVIRONMENT) are already set some other way.",
            PROJECT_ROOT / "local.settings.json",
        )

    environment = os.environ.get("ENVIRONMENT", "Dev")
    refuse_if_prod(environment)

    from shared.pole_timezones_loader import load_pole_timezones

    logging.info(
        "Running PoleTimeZones backfill (re-resolving EVERY pole via CountyFips, "
        "overwriting any existing row) against ENVIRONMENT=%s ...",
        environment,
    )
    load_pole_timezones(backfill=True)
    logging.info(
        "Backfill complete. loadPoleVitals/loadPoleDaylightFlags' next runs will "
        "use the newly-resolved timezones."
    )
