"""
One-off script to run
pole_vitals_loader.backfill_last_known_48_hours_for_offline_poles_after_formula_change()
-- force-recomputes EVERY genuinely offline pole's LastKnown48Hours row
under the CURRENT Last48Hours/LastKnown48Hours computation logic,
bypassing the normal "skip if this pole's own data hasn't changed"
optimization that load_pole_vitals() itself uses on every scheduled run.

Background: that normal optimization is a real, deliberate performance
fix in its own right (see pole_vitals_loader.py's own comment on
_LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL) -- it correctly
assumes a pole's own telemetry not changing means nothing needs
recomputing. That assumption breaks the one time the COMPUTATION LOGIC
itself changes instead of the data. Without this backfill, every
already-offline pole's LastKnown48Hours row would stay silently stuck on
whatever OLD logic last computed it, since nothing about that pole's own
telemetry ever prompts the normal path to revisit it.

Genuinely GENERIC, not tied to one specific past change -- re-run this
same script after ANY future change to Last48Hours/LastKnown48Hours' own
computation logic (first used for a change to IsPanelFaultFlag's own
formula, used again for a change to how AvgPanelPercentage/
AvgLightPercentage are averaged) -- no script changes needed for either.

Only needed for OFFLINE poles specifically. Last48Hours, and
LastKnown48Hours for any CURRENTLY ACTIVE pole (a direct copy of that
same pole's own Last48Hours row), both fully recompute from scratch on
every single loadPoleVitals run regardless of this kind of change -- the
very next scheduled run already reflects new computation logic for those
poles, no backfill needed there at all.

BATCHED, after a real production incident: a single, unbounded execution
covering every offline pole at once took long enough (potentially many
months' worth of accumulated offline poles, since this backfill's whole
point is that the normal path never revisits them) to hit a TCP-level
connection timeout partway through (SQLSTATE 08S01, "TCP Provider: Error
code 0x274C (10060)" -- WSAETIMEDOUT), losing all progress since nothing
had committed yet. Now processes a bounded number of poles per execution
(500 by default), committing after every batch, so a later batch's own
failure never undoes an earlier one's already-committed progress -- see
backfill_last_known_48_hours_for_offline_poles_after_formula_change()'s
own docstring for the full reasoning, including why this makes the
overall backfill more robust rather than fully cross-run resumable.

Run this ONCE, right after deploying a change to Last48Hours/
LastKnown48Hours' own computation logic. Not something that needs
running again afterward on any regular basis -- the normal "skip if
unchanged" path is correct and sufficient once this one-off catch-up has
run.

Usage (from the Backend/ project root):

    python3 scripts/backfill_last_known_48_hours_offline_poles.py
    python3 scripts/backfill_last_known_48_hours_offline_poles.py --batch-size 200

--batch-size overrides the default of 500 poles per execution -- lower
it if connection timeouts persist even with the default (e.g. an
especially slow or unreliable network path to the SQL Server), raise it
if the default is comfortably fast and you'd rather finish in fewer
round trips.

Reuses local.settings.json's "Values" (the same file `func start` reads),
so if you've already got that configured for local manual-trigger testing,
this needs no extra setup. Only needs SQL_CONNECTION_STRING/ENVIRONMENT to
run the backfill function itself, but importing pole_vitals_loader pulls
in pole_telemetry_loader -> leadsun_client, which reads
LEADSUN_CLIENT_CERT_PEM eagerly at import time even though this script
never calls fetch_lamps() -- so that (and LEADSUN_SERVER_CA_CERT/
LEADSUN_SKIP_HOSTNAME_CHECK, if your setup needs them) must be present
too, or the import itself will fail. Same situation as this project's
other backfill scripts.

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
    # Without this, the backfill function's own logging.info()/
    # logging.error() calls are silently swallowed -- there's no Azure
    # Functions runtime here to auto-configure a handler like there is
    # in production.
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

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Poles processed per execution (default: 500). Lower this if "
        "connection timeouts persist even at the default.",
    )
    args = parser.parse_args()

    from shared.pole_vitals_loader import (
        backfill_last_known_48_hours_for_offline_poles_after_formula_change,
    )

    logging.info(
        "Running LastKnown48Hours force-recompute for offline poles (every "
        "genuinely offline pole's row, under the current computation logic, "
        "regardless of whether its own telemetry has changed) against "
        "ENVIRONMENT=%s, batch_size=%d ...",
        environment,
        args.batch_size,
    )
    backfill_last_known_48_hours_for_offline_poles_after_formula_change(
        batch_size=args.batch_size
    )
    logging.info(
        "Backfill complete. Every currently-offline pole's LastKnown48Hours row "
        "now reflects the current computation logic. No need to run this again "
        "unless that logic changes once more."
    )
