"""
Recomputes PoleVitals for ALL provisioned poles by calling
load_provisioned_pole_vitals(backfill=True), which uses the extended
_BACKFILL_LOOKBACK window (400 days) instead of the normal rolling window.

Run this after:
  1. Deploying a new pole_vitals_loader.py (formula changes)
  2. Running backfill_provisioned_telemetry_columns.py (column population)

Usage (from the Backend/ project root):

    python3 scripts/backfill_provisioned_pole_vitals.py
"""

import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_local_settings_into_env(project_root: Path = PROJECT_ROOT) -> bool:
    settings_path = project_root / "local.settings.json"
    if not settings_path.exists():
        return False
    with open(settings_path) as f:
        settings = json.load(f)
    for key, value in settings.get("Values", {}).items():
        os.environ.setdefault(key, value)
    return True


def refuse_if_prod(environment: str) -> None:
    if environment == "Prod":
        raise SystemExit(
            "Refusing to run against ENVIRONMENT=Prod from this script. "
            "Point local.settings.json's ENVIRONMENT at Dev/Staging, or run "
            "this from the deployed environment's own Kudu/SSH console "
            "instead if you specifically mean to target that environment."
        )


if __name__ == "__main__":
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

    logging.info(
        "Running provisioned pole vitals backfill against ENVIRONMENT=%s ...",
        environment,
    )

    from shared.pole_vitals_loader import load_provisioned_pole_vitals
    load_provisioned_pole_vitals(backfill=True)

    logging.info("Backfill complete.")
