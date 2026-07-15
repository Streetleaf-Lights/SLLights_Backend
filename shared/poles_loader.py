import os
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
AIRTABLE_POLES_TABLE = "Streetleaf Poles"

# _map_record_to_pole() below only reads these -- restricting the API
# request to just them (instead of every field on a Pole record) shrinks
# each page's response payload, which can shave some time off the fetch.
# Real savings depend on how many other fields the live table has.
AIRTABLE_POLES_FIELDS = [
    "Pole Number",
    "Location ID",
    "Contracting Entity",
    "Customer ID",
    "Field Installed",
    "LAT",
    "LONG",
]

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

# fast_executemany cut round trips (14k -> ~28), but the server still runs
# each MERGE statement in a batch individually -- that per-statement
# execution cost is the next bottleneck. _UPSERT_BATCH_SIZE now also sizes
# the staging-table chunks below (see _STAGING_TABLE_SQL and friends),
# where the real win comes from: one set-based MERGE per chunk instead of
# one per row, even within a batch.
_UPSERT_BATCH_SIZE = 2000

# Airtable can return these literal strings for LAT/LONG when the underlying
# formula/lookup errors out (e.g. an address that couldn't be geocoded, or a
# divide-by-zero in the formula). Add to this set if other error strings
# turn up in the wild.
_COORDINATE_ERROR_STRINGS = {"#NA", "#ERROR!", "#DIV/0!"}


def _chunked(items, size):
    """Splits a list into consecutive chunks of at most `size` items each."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


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

# --------------------------------------------------------------------------
# Staging-table bulk path: stage a whole chunk, then run ONE set-based MERGE
# against it, instead of executing _POLE_UPSERT_SQL once per row (even
# batched via executemany, that's still N individual statement executions
# on the server). This is what actually gets 14k+ poles under a minute --
# fast_executemany batching alone only cuts round trips, not server-side
# per-statement execution cost.
#
# Tradeoff: a single bad row can fail the whole chunk's set-based MERGE
# (not just that row), unlike the per-row granularity of _POLE_UPSERT_SQL.
# load_poles() below falls back to row-by-row (via _POLE_UPSERT_SQL) for
# any chunk that fails this way, so the "blast radius" of a bad row is at
# most one _UPSERT_BATCH_SIZE-sized chunk, not the whole run.
#
# #PolesStaging is a local temp table -- scoped to this connection/session
# and dropped automatically when the connection closes. The IF OBJECT_ID
# guard is defensive in case a pooled/reused connection ever left one
# behind.
_STAGING_TABLE_SQL = """
IF OBJECT_ID('tempdb..#PolesStaging') IS NOT NULL DROP TABLE #PolesStaging;
CREATE TABLE #PolesStaging (
    Id                      VARCHAR(50)       NULL,
    PoleNumber              NVARCHAR(100)     NULL,
    LocationId              VARCHAR(50)       NULL,
    ProjectId               VARCHAR(50)       NULL,
    CustomerId              VARCHAR(50)       NULL,
    InstallDate             DATE              NULL,
    Lat                     FLOAT             NULL,
    Long                    FLOAT             NULL,
    SP_ExecId               INT               NULL,
    AirTableCreatedDateTime DATETIMEOFFSET(3) NULL
);
"""

_STAGING_INSERT_SQL = """
INSERT INTO #PolesStaging (Id, PoleNumber, LocationId, ProjectId, CustomerId, InstallDate, Lat, Long, SP_ExecId, AirTableCreatedDateTime)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_MERGE_FROM_STAGING_SQL = """
MERGE Poles AS target
USING #PolesStaging AS source
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

_TRUNCATE_STAGING_SQL = "TRUNCATE TABLE #PolesStaging"


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
    # Array-binds parameters for executemany() batches below instead of
    # sending one round trip per row.
    cursor.fast_executemany = True

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
        fetch_start = time.perf_counter()
        records, offsets_seen = fetch_all_records(AIRTABLE_POLES_TABLE, fields=AIRTABLE_POLES_FIELDS)
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadPoles: fetched %d record(s) across %d page(s) in %.1fs.",
            len(records),
            len(offsets_seen) + 1,
            fetch_seconds,
        )

        # 3. Map every record, then bulk-upsert in chunks: stage a chunk,
        # run one set-based MERGE against the whole staged chunk, truncate,
        # repeat. A chunk that fails this way falls back to running
        # _POLE_UPSERT_SQL row-by-row for just that chunk, so a single bad
        # pole doesn't cost more than one chunk's worth of rows (MERGE is
        # idempotent, so re-running already-applied rows from a
        # partially-failed chunk during the fallback is harmless).
        upsert_start = time.perf_counter()
        poles = [_map_record_to_pole(record) for record in records]
        param_rows = [
            (
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
            for pole in poles
        ]

        if param_rows:
            cursor.execute(_STAGING_TABLE_SQL)

        for batch in _chunked(param_rows, _UPSERT_BATCH_SIZE):
            try:
                cursor.executemany(_STAGING_INSERT_SQL, batch)
                cursor.execute(_MERGE_FROM_STAGING_SQL)
                cursor.execute(_TRUNCATE_STAGING_SQL)
                total_success += len(batch)
            except Exception as batch_error:
                logging.warning(
                    "loadPoles: chunk of %d failed to bulk-merge (%s); retrying row-by-row.",
                    len(batch),
                    batch_error,
                )
                cursor.execute(_TRUNCATE_STAGING_SQL)
                for row in batch:
                    try:
                        cursor.execute(_POLE_UPSERT_SQL, row)
                        total_success += 1
                    except Exception as row_error:
                        total_errors += 1
                        logging.error(
                            "loadPoles: failed to upsert %s: %s",
                            row[0],  # Id is the first positional param
                            row_error,
                        )

        conn.commit()
        logging.info(
            "loadPoles: upsert phase took %.1fs for %d record(s).",
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
