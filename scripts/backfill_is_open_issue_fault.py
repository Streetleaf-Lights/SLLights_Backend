"""
One-off script to run
pole_telemetry_loader.backfill_is_open_issue_fault_for_all_poles() --
corrects IsOpenIssueFault on EXISTING PoleTelemetry rows (within each
pole's own last 48 hours of activity, ending at that pole's own latest
reading) using the now-corrected PoleOpenIssues.PoleId -> Poles.Id join.

Background: PoleOpenIssues.PoleId used to be sourced from Airtable's
"PoleId" field, which links to a synced/mirror table, NOT the real Poles
table this project's own Poles.Id comes from -- so the join
_fetch_location_ids_with_open_issues() depends on never matched
correctly, meaning IsOpenIssueFault has likely been 0/False for
essentially every pole regardless of whether it actually had an open
issue, since pole_telemetry_loader.py was first built. Fixed by sourcing
from Airtable's "PoleRecordID" field instead (see
pole_open_issues_loader.py's own comments).

Run this AFTER:
  1. Deploying the corrected pole_open_issues_loader.py.
  2. Running loadPoleOpenIssues at least once with that fix in place
     (so PoleOpenIssues.PoleId in SQL is actually corrected).

Run this BEFORE re-running
scripts/backfill_last_48_hours_hour_pole_vitals.py -- that one only ever
reads whatever IsOpenIssueFault is ALREADY stored on PoleTelemetry and
aggregates it into PoleVitals; it cannot fix a wrong per-reading value
itself. Running it before this script would just re-aggregate the same,
still-incorrect values.

Usage (from the Backend/ project root):

    python3 scripts/backfill_is_open_issue_fault.py

Reuses local.settings.json's "Values" (the same file `func start` reads),
so if you've already got that configured for local manual-trigger testing,
this needs no extra setup. Only needs SQL_CONNECTION_STRING/ENVIRONMENT to
run backfill_is_open_issue_fault_for_all_poles() itself, but importing
pole_telemetry_loader pulls in leadsun_client directly, which reads
LEADSUN_CLIENT_CERT_PEM eagerly at import time even though this script
never calls fetch_lamps() -- so that (and LEADSUN_SERVER_CA_CERT/
LEADSUN_SKIP_HOSTNAME_CHECK, if your setup needs them) must be present
too, or the import itself will fail. Same situation as
scripts/backfill_latest_hour_pole_vitals.py and
scripts/run_pole_vitals_backfill.py.

If your local machine can't reach the same Azure SQL Server (e.g. firewall
rules only allow Azure-to-Azure traffic), run this instead from the
deployed Function App's Kudu/SSH console (Advanced Tools in the Portal),
where all the same values are already set as real App Settings -- no
local.settings.json needed there at all.
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
    # Without this, backfill_is_open_issue_fault_for_all_poles()'s
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

    from shared.pole_telemetry_loader import backfill_is_open_issue_fault_for_all_poles

    logging.info(
        "Running IsOpenIssueFault backfill (correcting existing PoleTelemetry rows "
        "within each pole's own last 48 hours of activity) against ENVIRONMENT=%s ...",
        environment,
    )
    backfill_is_open_issue_fault_for_all_poles()
    logging.info(
        "Backfill complete. Remember to re-run "
        "scripts/backfill_last_48_hours_hour_pole_vitals.py next, so PoleVitals "
        "reflects these corrected values too."
    )
