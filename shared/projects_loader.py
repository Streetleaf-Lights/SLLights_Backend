import os
import json
import logging
import time

from shared.airtable_client import fetch_all_records
from shared.airtable_removal_utils import flag_records_removed_from_airtable
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
# LeadsunProject: a JSON OBJECT column, not a plain scalar -- replaces an
# earlier, simpler LeadsunProjectId INT column entirely, per explicit
# request. This loader only EVER knows Airtable's own "ProjectId" value;
# the richer shape (ProjectName/UserName/groups/products, all sourced
# from Leadsun telemetry) gets filled in SEPARATELY, LATER, by
# pole_telemetry_loader.update_leadsun_project_details() after
# load_pole_telemetry() runs on its own, independent schedule (every 30
# minutes, vs this loader's twice-a-day cadence -- see function_app.py's
# own loadAirTableData/loadLeadsunData comments).
#
# Given that, THIS loader's own UPDATE must be a SURGICAL merge into just
# the "ProjectId" key -- NEVER a full-column overwrite. A plain
# `LeadsunProject = source.LeadsunProject` (replacing the whole column,
# the way every other column here works) would silently DESTROY whatever
# groups/products structure the Leadsun pipeline already built for this
# project on its own, most recent run -- a real, repeating data-loss bug
# (every single time this loader runs, twice a day), not a hypothetical
# edge case. JSON_MODIFY(ISNULL(target.LeadsunProject, '{}'), '$.ProjectId',
# ...) reads the row's OWN CURRENT value first and only replaces that one
# key within it, leaving groups/products (or anything else already
# there) completely untouched.
#
# A NULL source.LeadsunProjectIdValue (Airtable's own field is empty for
# this project) makes JSON_MODIFY REMOVE the "ProjectId" key entirely,
# rather than setting it to a JSON null -- SQL Server's own default
# behavior for JSON_MODIFY(..., NULL), not something worked around here.
# Accepted as-is: a project with no Leadsun ProjectID recorded simply
# has no "ProjectId" key at all until Airtable provides one, which is a
# reasonable, if not the only valid, way to represent "no value" in this
# JSON document.
_PROJECT_UPSERT_SQL = """
MERGE Projects AS target
USING (
    SELECT
        ? AS Id, ? AS Name, CAST(? AS NVARCHAR(MAX)) AS PoleNumbers, CAST(? AS NVARCHAR(MAX)) AS PoleIds, ? AS SP_ExecId,
        ? AS CustomerId, ? AS PolesUnderContract, ? AS EffectiveDate,
        CAST(? AS NVARCHAR(MAX)) AS InstallDates, CAST(? AS NVARCHAR(MAX)) AS LeadsunProjectIdValue, ? AS AirTableCreatedDateTime
) AS source
ON target.Id = source.Id
WHEN MATCHED AND NOT EXISTS (
    SELECT target.Name, target.PoleNumbers, target.PoleIds, target.CustomerId,
           target.PolesUnderContract, target.EffectiveDate, target.InstallDates,
           JSON_VALUE(target.LeadsunProject, '$.ProjectId')
    INTERSECT
    SELECT source.Name, source.PoleNumbers, source.PoleIds, source.CustomerId,
           source.PolesUnderContract, source.EffectiveDate, source.InstallDates,
           source.LeadsunProjectIdValue
)
THEN UPDATE SET
    Name               = source.Name,
    PoleNumbers        = source.PoleNumbers,
    PoleIds            = source.PoleIds,
    SP_ExecId          = source.SP_ExecId,
    CustomerId         = source.CustomerId,
    PolesUnderContract = source.PolesUnderContract,
    EffectiveDate      = source.EffectiveDate,
    InstallDates       = source.InstallDates,
    LeadsunProject     = JSON_MODIFY(ISNULL(target.LeadsunProject, '{}'), '$.ProjectId', source.LeadsunProjectIdValue)
WHEN NOT MATCHED THEN
    INSERT (Id, Name, PoleNumbers, PoleIds, SP_ExecId, CustomerId, PolesUnderContract, EffectiveDate, InstallDates, LeadsunProject, AirTableCreatedDateTime)
    VALUES (source.Id, source.Name, source.PoleNumbers, source.PoleIds, source.SP_ExecId,
            source.CustomerId, source.PolesUnderContract, source.EffectiveDate,
            source.InstallDates, JSON_MODIFY('{}', '$.ProjectId', source.LeadsunProjectIdValue), source.AirTableCreatedDateTime);
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
        # The RAW value only -- NOT a pre-built JSON string. Deliberately
        # NOT json.dumps()'d here the way PoleNumbers/PoleIds/InstallDates
        # above are: LeadsunProject is a JSON OBJECT (not a plain scalar),
        # and _PROJECT_UPSERT_SQL's own JSON_MODIFY() call needs this raw
        # value to surgically update JUST the "ProjectId" key within
        # whatever JSON is already stored there -- see that SQL's own
        # comment for why a full-column overwrite here would be a real
        # data-loss bug, not just a style choice.
        "LeadsunProjectIdValue": fields.get("Leadsun ProjectID"),
        "AirTableCreatedDateTime": _airtable_created_time_to_eastern(
            record.get("createdTime")
        ),
    }


def load_projects() -> None:
    start_time = _to_dto_string(_now_eastern())
    sp_exec_id = None
    total_success = 0
    total_errors = 0
    conn = None
    cursor = None

    try:
        # 1. Short-lived connection just to open the SP_Execution row --
        # closed immediately rather than held open through the Airtable
        # fetch below. A multi-page fetch can run for minutes; holding a
        # SQL connection open and completely idle for that whole window
        # risks an intermediate network hop (VPN, NAT/firewall) silently
        # dropping it, which then surfaces later as a communication-link
        # failure on the next DB write, well after the real problem
        # occurred.
        conn = get_connection()
        cursor = conn.cursor()
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
        cursor.close()
        conn.close()
        conn = None
        cursor = None

        # 2. Pull every page from Airtable before doing any DB writes --
        # no SQL connection open at all while this runs.
        fetch_start = time.perf_counter()
        records, offsets_seen = fetch_all_records(AIRTABLE_PROJECTS_TABLE)
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadProjects: fetched %d record(s) across %d page(s) in %.1fs.",
            len(records),
            len(offsets_seen) + 1,
            fetch_seconds,
        )

        # 3. Re-open a fresh connection for the write-heavy phase.
        conn = get_connection()
        cursor = conn.cursor()

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
                    project["LeadsunProjectIdValue"],
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

        # 3b. Flag any existing Project whose own Id wasn't in this run's
        # Airtable fetch at all -- based on `records` (everything actually
        # fetched), not on total_success, so a record that failed to
        # upsert above (still present in Airtable, just a transient DB
        # write failure) is never mistakenly flagged as removed.
        removed_flag_changes = flag_records_removed_from_airtable(
            cursor, "Projects", [record["id"] for record in records]
        )
        conn.commit()
        if removed_flag_changes:
            logging.info(
                "loadProjects: %d record(s) changed Active status.",
                removed_flag_changes,
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
            try:
                if conn is None:
                    conn = get_connection()
                    cursor = conn.cursor()
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
            except Exception as log_error:
                logging.error(
                    "loadProjects: also failed to record ErrorMessage on SP_Execution: %s",
                    log_error,
                )
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
