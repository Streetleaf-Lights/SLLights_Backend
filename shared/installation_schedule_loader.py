"""
Loads the Installation Schedule from Airtable and stores StickerNumbers
on the matching Projects row.

Source: same base as PoleIssues (AIRTABLE_POLE_ISSUES_BASE_ID /
appUgSvE28S6C0Yua), table tblpUebM8MiN0dTkB, view viwMYdiYlIaO5heiG.

OriginalProjectRecordId is a linked/lookup field that carries the
original Projects.Id across. StickerNumbers is a list of sticker label
strings (e.g. ["MRN-1710", "MRN-1711", ...]) stored as a JSON array.

Projects no longer present in the view have their StickerNumbers cleared
(set to NULL) so stale data doesn't linger.

Runs after loadProjects in the loadAirTableData pipeline, and is also
available as a standalone on-demand HTTP trigger (loadInstallationSchedule).
"""

import json
import logging
import os

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string

AIRTABLE_POLE_ISSUES_BASE_ID = os.environ["AIRTABLE_POLE_ISSUES_BASE_ID"]
AIRTABLE_INSTALLATION_SCHEDULE_TABLE = "tblpUebM8MiN0dTkB"
AIRTABLE_INSTALLATION_SCHEDULE_VIEW = "viwMYdiYlIaO5heiG"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

_UPDATE_SQL = """
UPDATE Projects
SET StickerNumbers = ?
WHERE Id = ?
  AND (StickerNumbers IS NULL OR StickerNumbers <> ?)
"""

_CLEAR_SQL = """
UPDATE Projects
SET StickerNumbers = NULL
WHERE StickerNumbers IS NOT NULL
  AND Id NOT IN ({placeholders})
"""

_CLEAR_ALL_SQL = """
UPDATE Projects SET StickerNumbers = NULL WHERE StickerNumbers IS NOT NULL
"""


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _map_record(record: dict) -> dict | None:
    """
    Maps a raw Airtable record to the fields we need.
    Returns None if OriginalProjectRecordId is missing (can't link to Projects).
    """
    fields = record.get("fields", {})
    original_project_id = _first(fields.get("OriginalProjectRecordId"))
    if not original_project_id:
        return None
    sticker_numbers = fields.get("StickerNumbers") or []
    return {
        "ProjectId": original_project_id,
        "StickerNumbers": json.dumps(sticker_numbers) if sticker_numbers else None,
    }


def load_installation_schedule() -> None:
    """
    Fetches the Installation Schedule Airtable view, matches each record
    to a Projects row via OriginalProjectRecordId, and updates
    Projects.StickerNumbers. Projects no longer in the view are cleared.

    Idempotent -- safe to run multiple times.
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
            "loadInstallationSchedule",
            ENVIRONMENT,
            start_time,
            "AirTable",
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        records, offsets_seen = fetch_all_records(
            AIRTABLE_INSTALLATION_SCHEDULE_TABLE,
            base_id=AIRTABLE_POLE_ISSUES_BASE_ID,
            view=AIRTABLE_INSTALLATION_SCHEDULE_VIEW,
        )
        logging.info(
            "loadInstallationSchedule: fetched %d record(s) across %d page(s).",
            len(records),
            len(offsets_seen) + 1,
        )

        mapped = [_map_record(r) for r in records]
        valid = [m for m in mapped if m is not None]
        logging.info(
            "loadInstallationSchedule: %d of %d record(s) have a valid OriginalProjectRecordId.",
            len(valid),
            len(mapped),
        )

        # Group by ProjectId -- multiple installation schedule records can
        # reference the same project, so merge all their sticker numbers
        # into one combined list before writing.
        from collections import defaultdict
        stickers_by_project: dict[str, list] = defaultdict(list)
        for item in valid:
            if item["StickerNumbers"]:
                stickers_by_project[item["ProjectId"]].extend(
                    json.loads(item["StickerNumbers"])
                )
            else:
                # Ensure the project ID is registered even if no stickers
                stickers_by_project.setdefault(item["ProjectId"], [])

        updated_project_ids = []
        for project_id, stickers in stickers_by_project.items():
            stickers_json = json.dumps(sorted(stickers)) if stickers else None
            try:
                cursor.execute(
                    _UPDATE_SQL,
                    stickers_json,
                    project_id,
                    stickers_json,
                )
                updated_project_ids.append(project_id)
                total_success += 1
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadInstallationSchedule: failed to update Project %s: %s",
                    project_id,
                    row_error,
                )

        # Clear StickerNumbers for projects no longer in the view
        if updated_project_ids:
            placeholders = ", ".join("?" for _ in updated_project_ids)
            cursor.execute(
                _CLEAR_SQL.format(placeholders=placeholders),
                *updated_project_ids,
            )
        else:
            cursor.execute(_CLEAR_ALL_SQL)
        cleared = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        if cleared:
            logging.info(
                "loadInstallationSchedule: cleared StickerNumbers from %d project(s) no longer in view.",
                cleared,
            )

        conn.commit()

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
            len(offsets_seen) + 1,
            sp_exec_id,
        )
        conn.commit()

        logging.info(
            "loadInstallationSchedule: %d project(s) updated, %d error(s).",
            total_success,
            total_errors,
        )

    except Exception as ex:
        logging.error("loadInstallationSchedule: run failed: %s", ex)
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
                    "loadInstallationSchedule: also failed to record run failure: %s "
                    "(original: %s)",
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()
