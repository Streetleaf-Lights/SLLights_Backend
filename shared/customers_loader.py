import os
import json
import logging
import time

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection
from shared.datetime_utils import (
    EASTERN,
    now_eastern as _now_eastern,
    to_dto_string as _to_dto_string,
    airtable_created_time_to_eastern as _airtable_created_time_to_eastern,
)

# Adjust this to match the exact table name in your Airtable base.
AIRTABLE_CUSTOMERS_TABLE = "Companies_New"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")


# Merge logic only fires the UPDATE branch when at least one of these fields
# actually differs from what's already stored, so unchanged customers are
# left untouched. SP_ExecId is intentionally excluded from the diff check
# (it's always refreshed to the latest run) but IS included in the SET list.
#
# CAST(...AS NVARCHAR(MAX)) on ProjectNames/ProjectIds: pyodbc binds string
# parameters as the legacy `ntext` type once they cross a length threshold
# (long JSON-encoded lists), and ntext doesn't support comparison operators
# like <> -- this is the same bug that broke Projects' INTERSECT diff-check
# for records with many poles. Customers hasn't hit it yet (needs a customer
# with enough linked projects to push the JSON string past the threshold),
# but the mechanism is identical, so casting here preempts it.
_UPSERT_SQL = """
MERGE Customers AS target
USING (
    SELECT
        ? AS Id, ? AS Name, CAST(? AS NVARCHAR(MAX)) AS ProjectNames, CAST(? AS NVARCHAR(MAX)) AS ProjectIds, ? AS SP_ExecId,
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
        fetch_start = time.perf_counter()
        records, offsets_seen = fetch_all_records(AIRTABLE_CUSTOMERS_TABLE)
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadCustomers: fetched %d record(s) across %d page(s) in %.1fs.",
            len(records),
            len(offsets_seen) + 1,
            fetch_seconds,
        )

        # 3. Upsert each customer -- insert if new, update only if something changed
        upsert_start = time.perf_counter()
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
        logging.info(
            "loadCustomers: upsert phase took %.1fs for %d record(s).",
            time.perf_counter() - upsert_start,
            len(records),
        )

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
