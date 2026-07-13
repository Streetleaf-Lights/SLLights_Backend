import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection

# Adjust this to match the exact table name in your Airtable base.
AIRTABLE_CUSTOMERS_TABLE = "Companies_New"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
EASTERN = ZoneInfo("America/New_York")


def _now_eastern() -> datetime:
    """Current time as an aware datetime in America/New_York (handles EST/EDT automatically)."""
    return datetime.now(EASTERN)


def _to_dto_string(dt: datetime) -> str:
    """
    Formats an aware datetime as an explicit DATETIMEOFFSET literal string,
    e.g. '2026-07-02 14:14:39.901 -04:00'.

    pyodbc silently converts timezone-aware datetime objects to UTC (offset
    +00:00) when binding them as parameters -- the wall-clock hour ends up
    right, but the offset gets discarded. Passing a pre-formatted string
    instead lets SQL Server parse the offset directly, so it's preserved.
    """
    offset = dt.strftime("%z")  # e.g. '-0400'
    offset_fmt = f"{offset[:3]}:{offset[3:]}"  # '-04:00'
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " " + offset_fmt


def _airtable_created_time_to_eastern(created_time: str | None) -> str | None:
    """
    Converts Airtable's createdTime (always UTC, e.g. '2025-11-17T19:56:44.000Z')
    into an Eastern-time DATETIMEOFFSET string, e.g. '2025-11-17 14:56:44.000 -05:00'.
    """
    if not created_time:
        return None

    utc_dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
    eastern_dt = utc_dt.astimezone(EASTERN)
    return _to_dto_string(eastern_dt)


# Merge logic only fires the UPDATE branch when at least one of these fields
# actually differs from what's already stored, so unchanged customers are
# left untouched. SP_ExecId is intentionally excluded from the diff check
# (it's always refreshed to the latest run) but IS included in the SET list.
_UPSERT_SQL = """
MERGE Customers AS target
USING (
    SELECT
        ? AS Id, ? AS Name, ? AS ProjectNames, ? AS ProjectIds, ? AS SP_ExecId,
        ? AS Address, ? AS City, ? AS State, ? AS Zip, ? AS Phone,
        ? AS AirTableCreatedDateTime
) AS source
ON target.Id = source.Id
WHEN MATCHED AND (
    ISNULL(target.Name, '')         <> ISNULL(source.Name, '')         OR
    ISNULL(target.ProjectNames, '') <> ISNULL(source.ProjectNames, '') OR
    ISNULL(target.ProjectIds, '')   <> ISNULL(source.ProjectIds, '')   OR
    ISNULL(target.Address, '')      <> ISNULL(source.Address, '')      OR
    ISNULL(target.City, '')         <> ISNULL(source.City, '')         OR
    ISNULL(target.State, '')        <> ISNULL(source.State, '')        OR
    ISNULL(target.Zip, '')          <> ISNULL(source.Zip, '')          OR
    ISNULL(target.Phone, '')        <> ISNULL(source.Phone, '')
)
THEN UPDATE SET
    Name         = source.Name,
    ProjectNames = source.ProjectNames,
    ProjectIds   = source.ProjectIds,
    SP_ExecId      = source.SP_ExecId,
    Address      = source.Address,
    City         = source.City,
    State        = source.State,
    Zip          = source.Zip,
    Phone        = source.Phone
WHEN NOT MATCHED THEN
    INSERT (Id, Name, ProjectNames, ProjectIds, SP_ExecId, Address, City, State, Zip, Phone, AirTableCreatedDateTime)
    VALUES (source.Id, source.Name, source.ProjectNames, source.ProjectIds, source.SP_ExecId,
            source.Address, source.City, source.State, source.Zip, source.Phone,
            source.AirTableCreatedDateTime);
"""


def _map_record_to_customer(record: dict) -> dict:
    """Maps a raw Airtable record to Customers table columns."""
    fields = record.get("fields", {})
    project_names = fields.get("ProjectNames", [])
    project_ids = fields.get("Executed Projects", [])

    return {
        "Id": record["id"],  # Airtable's own record id, e.g. "recAbCdEfGh12345"
        "Name": fields.get("Name"),
        "ProjectNames": (
            json.dumps(project_names)
            if isinstance(project_names, list)
            else project_names
        ),
        "ProjectIds": (
            json.dumps(project_ids) if isinstance(project_ids, list) else project_ids
        ),
        "Address": fields.get("Street"),
        "City": fields.get("City"),
        "State": fields.get("State"),
        "Zip": fields.get("Zip"),
        "Phone": fields.get("Phone Number"),
        "AirTableCreatedDateTime": _airtable_created_time_to_eastern(
            record.get("createdTime")
        ),
    }


def load_customers() -> None:
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
            "loadCustomers",
            ENVIRONMENT,
            start_time,
            "AirTable",
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Pull every page from Airtable before doing any DB writes
        records, offsets_seen = fetch_all_records(AIRTABLE_CUSTOMERS_TABLE)
        logging.info(
            "loadCustomers: fetched %d record(s) across %d page(s).",
            len(records),
            len(offsets_seen) + 1,
        )

        # 3. Upsert each customer -- insert if new, update only if something changed
        for record in records:
            customer = _map_record_to_customer(record)
            try:
                cursor.execute(
                    _UPSERT_SQL,
                    customer["Id"],
                    customer["Name"],
                    customer["ProjectNames"],
                    customer["ProjectIds"],
                    sp_exec_id,
                    customer["Address"],
                    customer["City"],
                    customer["State"],
                    customer["Zip"],
                    customer["Phone"],
                    customer["AirTableCreatedDateTime"],
                )
                total_success += 1
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadCustomers: failed to upsert %s: %s",
                    customer.get("Id"),
                    row_error,
                )

        conn.commit()

        # 4. Close out the SP_Execution row with final counts
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
        logging.error("loadCustomers: run failed: %s", ex)
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
