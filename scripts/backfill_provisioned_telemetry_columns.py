"""
Backfills BatterySoC, LightRatio, PanelPercentage, BatteryFault, LEDFault,
and ControllerFault from ExtraFieldsJson for provisioned PoleTelemetry rows
that were ingested before these columns were added to the schema.

Targets only rows where Source = 'Provisioned' AND at least one of the
new columns is NULL (meaning they predate the migration) AND ExtraFieldsJson
contains the relevant keys. Safe to re-run -- only updates rows that still
have NULL in these columns.

Usage (from the Backend/ project root):

    python3 scripts/backfill_provisioned_telemetry_columns.py

Reuses local.settings.json's "Values" (the same file `func start` reads),
so if you've already got that configured for local manual-trigger testing,
this needs no extra setup.
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


_FETCH_SQL = """
SELECT LocationId, LastUpload, ExtraFieldsJson
FROM PoleTelemetry
WHERE Source = 'Provisioned'
  AND ExtraFieldsJson IS NOT NULL
  AND (
      BatterySoC IS NULL
      OR LightRatio IS NULL
      OR PanelPercentage IS NULL
      OR BatteryFault IS NULL
      OR LEDFault IS NULL
      OR ControllerFault IS NULL
  )
"""

_UPDATE_SQL = """
UPDATE PoleTelemetry
SET
    BatterySoC      = ?,
    LightRatio      = ?,
    PanelPercentage = ?,
    BatteryFault    = ?,
    LEDFault        = ?,
    ControllerFault = ?
WHERE LocationId = ?
  AND LastUpload  = ?
  AND Source      = 'Provisioned'
"""

CHUNK_SIZE = 500


def run():
    from shared.sql_client import get_connection

    conn = get_connection()
    cursor = conn.cursor()

    logging.info("Fetching provisioned rows with NULL device columns...")
    cursor.execute(_FETCH_SQL)
    rows = cursor.fetchall()
    logging.info("Found %d rows to backfill.", len(rows))

    updates = []
    skipped = 0
    for location_id, last_upload, extra_json in rows:
        try:
            extra = json.loads(extra_json)
        except Exception:
            skipped += 1
            continue

        bat_soc   = extra.get("BatterySoC")
        light     = extra.get("LightRatio")
        panel     = extra.get("PanelPercentage")
        bat_fault = extra.get("BatteryFault")
        led_fault = extra.get("LEDFault")
        ctrl_fault = extra.get("ControllerFault")

        if all(v is None for v in (bat_soc, light, panel, bat_fault, led_fault, ctrl_fault)):
            skipped += 1
            continue

        updates.append((
            bat_soc,
            light,
            panel,
            bool(bat_fault) if bat_fault is not None else None,
            bool(led_fault) if led_fault is not None else None,
            bool(ctrl_fault) if ctrl_fault is not None else None,
            location_id,
            last_upload,
        ))

    logging.info(
        "Backfilling %d rows (%d skipped -- no relevant keys in ExtraFieldsJson).",
        len(updates), skipped,
    )

    total_updated = 0
    for i in range(0, len(updates), CHUNK_SIZE):
        chunk = updates[i:i + CHUNK_SIZE]
        for params in chunk:
            cursor.execute(_UPDATE_SQL, *params)
        conn.commit()
        total_updated += len(chunk)
        logging.info("  %d / %d rows committed.", total_updated, len(updates))

    cursor.close()
    conn.close()
    logging.info("Done. %d rows backfilled.", total_updated)


if __name__ == "__main__":
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

    run()
