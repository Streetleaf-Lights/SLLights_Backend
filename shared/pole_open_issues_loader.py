import os
import logging

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string

# The base id and table id come directly from the Airtable API URL given
# for this table (https://api.airtable.com/v0/<base>/<table>?view=<view>).
# This base is confirmed SEPARATE from AIRTABLE_BASE_ID (the one
# Customers/Projects/Poles come from), hence its own dedicated env var
# rather than reusing that one.
AIRTABLE_POLE_ISSUES_BASE_ID = os.environ["AIRTABLE_POLE_ISSUES_BASE_ID"]
# A table ID (tblXXXXXXXXXXXXXX), not a human-readable table name -- the
# only identifier available for this table (its Airtable-UI display name
# wasn't given when this was built), but Airtable's API accepts either
# interchangeably, so this works exactly the same as a name would.
AIRTABLE_POLE_ISSUES_TABLE = "tblKEoTFRGOz7BT84"
# The specific Airtable view given for this table -- scopes the fetch to
# whatever that view is already configured to show in the Airtable UI, on
# top of (not instead of) the Status/PoleStatus filtering this loader
# applies itself below (see _matches_open_issue_filter()'s own comment for
# why that filtering isn't left to the view alone).
AIRTABLE_POLE_ISSUES_VIEW = "viwtIpTLO7EGuFxoX"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

# The only two Pole Status values this table is meant to hold -- matches
# CK_PoleOpenIssues_PoleStatus in "sql/PoleOpenIssues/Create tbl
# PoleOpenIssues.sql". A single named tuple, not repeated inline, so the
# Python-side filter and the SQL-side constraint can't quietly drift
# apart from each other over time.
_ALLOWED_POLE_STATUSES = ("Electrical Issue", "Structural Issue")


_ISSUE_UPSERT_SQL = """
MERGE PoleOpenIssues AS target
USING (
    SELECT ? AS Id, ? AS IssueId, ? AS PoleId, ? AS Status, ? AS PoleStatus, ? AS SP_ExecId
) AS source
ON target.Id = source.Id
WHEN MATCHED AND NOT EXISTS (
    SELECT target.IssueId, target.PoleId, target.Status, target.PoleStatus
    INTERSECT
    SELECT source.IssueId, source.PoleId, source.Status, source.PoleStatus
)
THEN UPDATE SET
    IssueId    = source.IssueId,
    PoleId     = source.PoleId,
    Status     = source.Status,
    PoleStatus = source.PoleStatus,
    SP_ExecId  = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (Id, IssueId, PoleId, Status, PoleStatus, SP_ExecId)
    VALUES (source.Id, source.IssueId, source.PoleId, source.Status, source.PoleStatus, source.SP_ExecId);
"""


def _first_linked_value(value):
    """
    Airtable linked-record/lookup/multi-select fields all come back as a
    list, even when there's only ever one relevant value -- same
    "first element taken" convention poles_loader.py/projects_loader.py
    already use for their own linked fields. Used here for both PoleId
    (a genuine linked-record field -> an Airtable record id) and
    Pole Status (a lookup/multi-select field -> a plain category string,
    not a record id) -- the list-unwrapping is identical either way, only
    the kind of value inside the list differs.
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value or None


def _map_record_to_issue(record: dict) -> dict:
    """Maps a raw Airtable record to PoleOpenIssues table columns."""
    fields = record.get("fields", {})
    return {
        "Id": record["id"],  # Airtable's own record id, e.g. "recAbCdEfGh12345"
        "IssueId": fields.get("IssueID"),
        # Linked-record field -- list of ids, first taken. Should line up
        # with Poles.Id.
        "PoleId": _first_linked_value(fields.get("PoleId")),
        "Status": fields.get("Status"),
        # Lookup/multi-select field -- list of plain category strings
        # (not record ids), first taken.
        "PoleStatus": _first_linked_value(fields.get("Pole Status")),
    }


def _matches_open_issue_filter(issue: dict) -> bool:
    """
    Applied here in Python, not via Airtable's own filterByFormula --
    "Pole Status" is a lookup/multi-select field, and filterByFormula's
    array-handling functions (ARRAYJOIN/SEARCH, etc.) are easy to get
    subtly wrong for that kind of field. Filtering after fetching is
    simpler and fully within our own control, at the cost of fetching
    (and discarding) whatever AIRTABLE_POLE_ISSUES_VIEW doesn't already
    exclude on its own -- an acceptable tradeoff given this table is
    "open issues only", not the full issue history, and so should stay
    small regardless.

    Applied on top of, not instead of, whatever that view already shows
    -- "Status is Open, Pole Status is Electrical Issue or Structural
    Issue only" is treated as a hard guarantee this loader itself
    enforces, not something left entirely to how the view happens to be
    configured in the Airtable UI (which isn't visible or
    version-controlled from here, and could change without this code
    knowing).
    """
    return issue["Status"] == "Open" and issue["PoleStatus"] in _ALLOWED_POLE_STATUSES


def load_pole_open_issues() -> None:
    """
    Loads PoleOpenIssues from a dedicated Airtable view in a SEPARATE base
    from the one Customers/Projects/Poles come from, keeping only records
    where Status is 'Open' and Pole Status is 'Electrical Issue' or
    'Structural Issue' -- see _matches_open_issue_filter()'s own comment
    for why that filtering happens here in Python rather than via
    Airtable's filterByFormula.

    Unlike poles_loader.py/projects_loader.py/customers_loader.py, this
    also actively removes any existing PoleOpenIssues row that no longer
    matches that filter (e.g. an issue that's since been resolved in
    Airtable) -- this table's whole name promises it only holds
    CURRENTLY open issues, so a plain upsert-only pattern (which never
    deletes anything) would silently leave stale, no-longer-open rows
    behind forever.
    """
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()

    sp_exec_id = None
    total_success = 0
    total_errors = 0

    try:
        # 1. Open an SP_Execution row for this run
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "loadPoleOpenIssues",
            ENVIRONMENT,
            start_time,
            "AirTable",
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Pull every page from the specific view in the separate base,
        # before doing any DB writes
        records, offsets_seen = fetch_all_records(
            AIRTABLE_POLE_ISSUES_TABLE,
            base_id=AIRTABLE_POLE_ISSUES_BASE_ID,
            view=AIRTABLE_POLE_ISSUES_VIEW,
        )
        logging.info(
            "loadPoleOpenIssues: fetched %d record(s) across %d page(s).",
            len(records),
            len(offsets_seen) + 1,
        )

        # 3. Map every record, then filter down to Status='Open' + a
        # valid Pole Status -- everything after this point only ever
        # deals with the filtered set, not the raw fetch.
        mapped = [_map_record_to_issue(record) for record in records]
        matching = [issue for issue in mapped if _matches_open_issue_filter(issue)]
        logging.info(
            "loadPoleOpenIssues: %d of %d fetched record(s) match the Status/Pole Status filter.",
            len(matching),
            len(mapped),
        )

        # 4. Upsert each matching issue -- insert if new, update only if
        # something changed
        for issue in matching:
            try:
                cursor.execute(
                    _ISSUE_UPSERT_SQL,
                    issue["Id"],
                    issue["IssueId"],
                    issue["PoleId"],
                    issue["Status"],
                    issue["PoleStatus"],
                    sp_exec_id,
                )
                total_success += 1
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadPoleOpenIssues: failed to upsert %s: %s",
                    issue.get("Id"),
                    row_error,
                )

        # 5. Remove any existing row that's no longer in the matching
        # set -- see load_pole_open_issues()'s own docstring for why this
        # step exists at all. Guards the empty-list case explicitly:
        # "WHERE Id NOT IN ()" is invalid SQL, and an empty matching set
        # legitimately means "delete everything currently in the table".
        matching_ids = [issue["Id"] for issue in matching]
        if matching_ids:
            placeholders = ", ".join("?" for _ in matching_ids)
            cursor.execute(
                f"DELETE FROM PoleOpenIssues WHERE Id NOT IN ({placeholders})",
                *matching_ids,
            )
        else:
            cursor.execute("DELETE FROM PoleOpenIssues")
        logging.info("loadPoleOpenIssues: removed %d stale (no-longer-matching) row(s).", cursor.rowcount)

        conn.commit()

        # 6. Close out the SP_Execution row with final counts
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

    except Exception as ex:
        logging.error("loadPoleOpenIssues: run failed: %s", ex)
        if sp_exec_id:
            cursor.execute(
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
            conn.commit()
        raise
    finally:
        cursor.close()
        conn.close()
