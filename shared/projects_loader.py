import os
import json
import logging
import time

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection
from shared.datetime_utils import (
    now_eastern as _now_eastern,
    to_dto_string as _to_dto_string,
    airtable_created_time_to_eastern as _airtable_created_time_to_eastern,
)

# Adjust this to match the exact table name in your Airtable base.
AIRTABLE_PROJECTS_TABLE = "Project Tracking"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")


# Diff-checked via INTERSECT rather than ISNULL(col, '') <> ISNULL(col, '').
# INTERSECT treats NULLs as equal by default in T-SQL, and unlike the
# ISNULL(..., '') pattern used in Customers' MERGE, it works safely across
# mixed column types -- EffectiveDate is a DATE column, and ISNULL(DateCol, '')
# would fail trying to implicitly convert '' to a date.
#
# CAST(...AS NVARCHAR(MAX)) on PoleNumbers/PoleIds/InstallDates: pyodbc binds
# string parameters as the legacy `ntext` type once they cross a length
# threshold (long JSON-encoded lists), and ntext can't be used as an operand
# to INTERSECT/UNION/EXCEPT ("data type ntext ... is not comparable").
# Casting forces the server to treat these as proper nvarchar(max), which IS
# comparable, regardless of what type the driver guessed for the parameter.
_PROJECT_UPSERT_SQL = """
MERGE Projects AS target
USING (
    SELECT
        ? AS Id, ? AS Name, CAST(? AS NVARCHAR(MAX)) AS PoleNumbers, CAST(? AS NVARCHAR(MAX)) AS PoleIds, ? AS SP_ExecId,
        ? AS CustomerId, ? AS PolesUnderContract, ? AS EffectiveDate,
        CAST(? AS NVARCHAR(MAX)) AS InstallDates, ? AS AirTableCreatedDateTime
) AS source
ON target.Id = source.Id
WHEN MATCHED AND NOT EXISTS (
    SELECT target.Name, target.PoleNumbers, target.PoleIds, target.CustomerId,
           target.PolesUnderContract, target.EffectiveDate, target.InstallDates
    INTERSECT
    SELECT source.Name, source.PoleNumbers, source.PoleIds, source.CustomerId,
           source.PolesUnderContract, source.EffectiveDate, source.InstallDates
)
THEN UPDATE SET
    Name               = source.Name,
    PoleNumbers        = source.PoleNumbers,
    PoleIds            = source.PoleIds,
    SP_ExecId          = source.SP_ExecId,
    CustomerId         = source.CustomerId,
    PolesUnderContract = source.PolesUnderContract,
    EffectiveDate      = source.EffectiveDate,
    InstallDates       = source.InstallDates
WHEN NOT MATCHED THEN
    INSERT (Id, Name, PoleNumbers, PoleIds, SP_ExecId, CustomerId, PolesUnderContract, EffectiveDate, InstallDates, AirTableCreatedDateTime)
    VALUES (source.Id, source.Name, source.PoleNumbers, source.PoleIds, source.SP_ExecId,
            source.CustomerId, source.PolesUnderContract, source.EffectiveDate,
            source.InstallDates, source.AirTableCreatedDateTime);
"""


def _map_record_to_project(record: dict) -> dict:
    """Maps a raw Airtable record to Projects table columns."""
    fields = record.get("fields", {})
    # ASSUMPTION: Airtable field name for PoleNumbers still unconfirmed --
    # only PoleIds ("Streetleaf Poles") has been confirmed so far.
    pole_numbers = fields.get("PoleNumbers", [])
    pole_ids = fields.get("Streetleaf Poles", [])
    # InstallDates is now plural/multi-valued, same list-of-values shape as
    # PoleNumbers/PoleIds -- JSON-encoded into an NVARCHAR(MAX) column.
    install_dates = fields.get("Install Date(S)", [])
    # ASSUMPTION: "Contracting Entity" is treated as an Airtable
    # linked-record field returning a list of linked record ids even for a
    # "single link" relationship. Projects.CustomerId is singular, so this
    # takes the first linked id and drops the rest. Confirm the real shape.
    customer_ids = fields.get("Contracting Entity", [])

    return {
        "Id": record["id"],  # Airtable's own record id, e.g. "recAbCdEfGh12345"
        "Name": fields.get("Executed Project"),
        "PoleNumbers": (
            json.dumps(pole_numbers) if isinstance(pole_numbers, list) else pole_numbers
        ),
        "PoleIds": (
            json.dumps(pole_ids) if isinstance(pole_ids, list) else pole_ids
        ),
        "CustomerId": (
            customer_ids[0]
            if isinstance(customer_ids, list) and customer_ids
            else (customer_ids or None)
        ),
        "PolesUnderContract": fields.get("Lights Under Contract"),
        "EffectiveDate": fields.get("Effective Date"),
        "InstallDates": (
            json.dumps(install_dates) if isinstance(install_dates, list) else install_dates
        ),
        "AirTableCreatedDateTime": _airtable_created_time_to_eastern(
            record.get("createdTime")
        ),
    }


def load_projects() -> None:
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
            "loadProjects",
            ENVIRONMENT,
            start_time,
            "AirTable",
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Pull every page from Airtable before doing any DB writes
        fetch_start = time.perf_counter()
        records, offsets_seen = fetch_all_records(AIRTABLE_PROJECTS_TABLE)
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadProjects: fetched %d record(s) across %d page(s) in %.1fs.",
            len(records),
            len(offsets_seen) + 1,
            fetch_seconds,
        )

        # 3. Upsert each project -- insert if new, update only if something changed
        upsert_start = time.perf_counter()
        for record in records:
            project = _map_record_to_project(record)
            try:
                cursor.execute(
                    _PROJECT_UPSERT_SQL,
                    project["Id"],
                    project["Name"],
                    project["PoleNumbers"],
                    project["PoleIds"],
                    sp_exec_id,
                    project["CustomerId"],
                    project["PolesUnderContract"],
                    project["EffectiveDate"],
                    project["InstallDates"],
                    project["AirTableCreatedDateTime"],
                )
                total_success += 1
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadProjects: failed to upsert %s: %s",
                    project.get("Id"),
                    row_error,
                )

        conn.commit()
        logging.info(
            "loadProjects: upsert phase took %.1fs for %d record(s).",
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
        logging.error("loadProjects: run failed: %s", ex)
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
