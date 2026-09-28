"""
Loads the synced 'Streetleaf Poles' table from the Airtable Issues base
and stores each synced record's ID as Poles.LinkedPoleId on the matching
Poles row.

Background: Airtable does not allow linked record fields across different
bases. To link PoleIssues (in the Issues base) to Poles (in the main base),
the Poles table is synced into the Issues base as a mirror copy. Each
synced record has its own record ID (different from the original), with
OriginalPoleRecordID carrying the original Poles.Id across the sync.

Poles.LinkedPoleId is that synced record ID -- the value needed when
creating a PoleIssue via the Airtable API, as the PoleId linked field
in the Issues table points to the synced copy, not the original.

Must run AFTER loadPoles so Poles.Id values already exist to match against.
"""

import logging
import os

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string

# The Issues base -- separate from the main Airtable base
AIRTABLE_POLE_ISSUES_BASE_ID = os.environ["AIRTABLE_POLE_ISSUES_BASE_ID"]

# The synced 'Streetleaf Poles' table inside the Issues base
AIRTABLE_LINKED_POLES_TABLE = "tbltKaMb0Yitlh6bk"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

_UPDATE_SQL = """
UPDATE Poles
SET LinkedPoleId = ?
WHERE Id = ?
  AND (LinkedPoleId IS NULL OR LinkedPoleId <> ?)
"""


def load_linked_poles() -> None:
    """
    Fetches the synced Streetleaf Poles table from the Issues base and
    updates Poles.LinkedPoleId for each row whose OriginalPoleRecordID
    matches a Poles.Id. Rows in the synced table with no matching Poles
    record (e.g. stale sync entries) are skipped.

    Runs after loadPoles in the loadAirTableData pipeline.
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
            "loadLinkedPoles",
            ENVIRONMENT,
            start_time,
            "AirTable",
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        records, offsets_seen = fetch_all_records(
            AIRTABLE_LINKED_POLES_TABLE,
            base_id=AIRTABLE_POLE_ISSUES_BASE_ID,
        )
        logging.info(
            "loadLinkedPoles: fetched %d synced record(s) across %d page(s).",
            len(records),
            len(offsets_seen) + 1,
        )

        for record in records:
            linked_pole_id = record["id"]
            fields = record.get("fields", {})
            # OriginalPoleRecordID is the computed field that carries the
            # original Poles.Id from the main base across the sync boundary.
            original_ids = fields.get("OriginalPoleRecordID")
            if not original_ids:
                continue
            original_pole_id = original_ids[0] if isinstance(original_ids, list) else original_ids

            try:
                cursor.execute(_UPDATE_SQL, linked_pole_id, original_pole_id, linked_pole_id)
                if cursor.rowcount and cursor.rowcount > 0:
                    total_success += 1
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadLinkedPoles: failed to update Poles.LinkedPoleId for %s → %s: %s",
                    original_pole_id,
                    linked_pole_id,
                    row_error,
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
            "loadLinkedPoles: %d Poles.LinkedPoleId value(s) updated, %d error(s).",
            total_success,
            total_errors,
        )

    except Exception as ex:
        logging.error("loadLinkedPoles: run failed: %s", ex)
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
                    "loadLinkedPoles: also failed to record run failure: %s "
                    "(original: %s)",
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()
