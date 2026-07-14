import os
import logging

from shared.airtable_client import fetch_all_records
from shared.sql_client import get_connection
from shared.datetime_utils import (
    now_eastern as _now_eastern,
    to_dto_string as _to_dto_string,
    airtable_created_time_to_eastern as _airtable_created_time_to_eastern,
)

# Adjust this to match the exact table name in your Airtable base.
AIRTABLE_POLES_TABLE = "Streetleaf Poles"

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

# Airtable can return these literal strings for LAT/LONG when the underlying
# formula/lookup errors out (e.g. an address that couldn't be geocoded, or a
# divide-by-zero in the formula). Add to this set if other error strings
# turn up in the wild.
_COORDINATE_ERROR_STRINGS = {"#NA", "#ERROR!", "#DIV/0!"}


# Diff-checked via INTERSECT (NULL-safe, and safe across mixed column types
# like InstallDate/Lat/Long) -- same pattern as Projects' MERGE. No columns
# here are unbounded-length JSON lists (unlike Projects' PoleNumbers/PoleIds/
# InstallDates), so there's no CAST(...AS NVARCHAR(MAX)) needed to dodge the
# ntext/INTERSECT bug that hit Projects.
_POLE_UPSERT_SQL = """
MERGE Poles AS target
USING (
    SELECT
        ? AS Id, ? AS PoleNumber, ? AS LocationId, ? AS ProjectId, ? AS CustomerId,
        ? AS InstallDate, ? AS Lat, ? AS Long, ? AS SP_ExecId, ? AS AirTableCreatedDateTime
) AS source
ON target.Id = source.Id
WHEN MATCHED AND NOT EXISTS (
    SELECT target.PoleNumber, target.LocationId, target.ProjectId, target.CustomerId,
           target.InstallDate, target.Lat, target.Long
    INTERSECT
    SELECT source.PoleNumber, source.LocationId, source.ProjectId, source.CustomerId,
           source.InstallDate, source.Lat, source.Long
)
THEN UPDATE SET
    PoleNumber  = source.PoleNumber,
    LocationId  = source.LocationId,
    ProjectId   = source.ProjectId,
    CustomerId  = source.CustomerId,
    InstallDate = source.InstallDate,
    Lat         = source.Lat,
    Long        = source.Long,
    SP_ExecId   = source.SP_ExecId
WHEN NOT MATCHED THEN
    INSERT (Id, PoleNumber, LocationId, ProjectId, CustomerId, InstallDate, Lat, Long, SP_ExecId, AirTableCreatedDateTime)
    VALUES (source.Id, source.PoleNumber, source.LocationId, source.ProjectId, source.CustomerId,
            source.InstallDate, source.Lat, source.Long, source.SP_ExecId, source.AirTableCreatedDateTime);
"""


def _clean_coordinate(value):
    """
    Lat/Long are FLOAT columns. Airtable can hand these back as strings with
    stray leading/trailing whitespace, which fails to load -- trim first.
    After trimming, treat any of _COORDINATE_ERROR_STRINGS as 0 instead of
    passing a non-numeric string through.
    """
    if isinstance(value, str):
        value = value.strip()
        if value in _COORDINATE_ERROR_STRINGS:
            return 0
    return value


def _map_record_to_pole(record: dict) -> dict:
    """Maps a raw Airtable record to Poles table columns."""
    fields = record.get("fields", {})
    # "Contracting Entity" is confirmed as the correct field for ProjectId
    # here (the same-looking label on Project Tracking that maps to
    # CustomerId there was just a naming coincidence, not a shared meaning).
    # It's a linked-record field -- list of ids, first one taken.
    project_ids = fields.get("Contracting Entity", [])
    # Customer ID is also a linked-record field (list of ids, first taken).
    customer_ids = fields.get("Customer ID", [])

    return {
        "Id": record["id"],  # Airtable's own record id, e.g. "recAbCdEfGh12345"
        "PoleNumber": fields.get("Pole Number"),
        "LocationId": fields.get("Location ID"),  # plain scalar, confirmed
        "ProjectId": (
            project_ids[0]
            if isinstance(project_ids, list) and project_ids
            else (project_ids or None)
        ),
        "CustomerId": (
            customer_ids[0]
            if isinstance(customer_ids, list) and customer_ids
            else (customer_ids or None)
        ),
        "InstallDate": fields.get("Field Installed"),
        "Lat": _clean_coordinate(fields.get("LAT")),
        "Long": _clean_coordinate(fields.get("LONG")),
        "AirTableCreatedDateTime": _airtable_created_time_to_eastern(
            record.get("createdTime")
        ),
    }


def load_poles() -> None:
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
            "loadPoles",
            ENVIRONMENT,
            start_time,
            "AirTable",
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Pull every page from Airtable before doing any DB writes
        records, offsets_seen = fetch_all_records(AIRTABLE_POLES_TABLE)
        logging.info(
            "loadPoles: fetched %d record(s) across %d page(s).",
            len(records),
            len(offsets_seen) + 1,
        )

        # 3. Upsert each pole -- insert if new, update only if something changed
        for record in records:
            pole = _map_record_to_pole(record)
            try:
                cursor.execute(
                    _POLE_UPSERT_SQL,
                    pole["Id"],
                    pole["PoleNumber"],
                    pole["LocationId"],
                    pole["ProjectId"],
                    pole["CustomerId"],
                    pole["InstallDate"],
                    pole["Lat"],
                    pole["Long"],
                    sp_exec_id,
                    pole["AirTableCreatedDateTime"],
                )
                total_success += 1
            except Exception as row_error:
                total_errors += 1
                logging.error(
                    "loadPoles: failed to upsert %s: %s",
                    pole.get("Id"),
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
        logging.error("loadPoles: run failed: %s", ex)
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
