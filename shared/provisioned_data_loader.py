"""
load_provisioned_pole_models() / load_provisioned_pole_serials() -- the
first two of what will grow into a "loadProvisionedData" pipeline
(mirroring loadAirTableData/loadDeviceData's own umbrella-plus-
individual-loaders shape), pulling data from Streetleaf's OWN
provisioned-poles database (a separate Azure SQL server, same
subscription -- see shared/sql_client.get_provisioned_connection())
rather than Airtable or Leadsun.

load_provisioned_pole_models(): sourced from that database's own
`products` table. Writes into the SAME PoleModels table pole_models_
loader.py's own Leadsun-sourced load_leadsun_pole_models() already
writes to -- these are additional rows, not a separate table,
distinguished by Source = 'Provisioned' (vs 'Leadsun') and a disjoint
ModelId range (see _PROVISIONED_MODEL_ID_OFFSET below). Deliberately
REUSES pole_models_loader.py's own column list and staging-table/MERGE
SQL text (imported directly, not duplicated) -- same "one shared
implementation, no silent drift" reasoning as poles_api.py reaching into
pole_vitals_api.py's own internals: this is genuinely the same target
table and the same upsert shape, just a different source and a
different subset of columns actually populated.

Source `products` schema (Streetleaf's provisioned db):
    product_id  INT IDENTITY  NOT NULL  -- becomes ModelId (+1000, see below)
    prefix      NVARCHAR(16)  NOT NULL  -- captured in ExtraFieldsJson, not its own column
    name        NVARCHAR(64)  NOT NULL  -- becomes ModelName
    is_active   BIT           NOT NULL  -- captured in ExtraFieldsJson, not its own column

Every PoleModels column this source doesn't populate (Battery,
SystemVoltage, CommType, LightDisType, IconUrl, LampsUsing,
BatteryVoltage, IsAc, IsDcOut, ModelSeries, BatteryCapacity1/2,
SolarBoardVoltage) is simply NULL for these rows -- the table's own
schema already allows this (every one of those columns is nullable),
so no DDL change was needed to support this second source.

load_provisioned_pole_serials(): sourced from that same database's own
`serials` table. Genuinely different shape from the models loader above
-- this one UPDATES existing Poles rows (matched by
serials.serial_number = Poles.ControllerId), it never inserts new Poles
rows at all. See sql/Poles/"Add ProvisionedPoleId PoleModelId
ProvisionedPoleCreatedDateTime columns.sql" for the three columns this
populates and the full reasoning (including why
ProvisionedPoleCreatedDateTime is a plain DATETIME2, not this project's
usual DATETIMEOFFSET).

Source `serials` schema (Streetleaf's provisioned db):
    serial_id      INT IDENTITY  NOT NULL  -- not used; this table's own identity, not needed here
    device_uid     NVARCHAR(64)  NOT NULL  -- becomes ProvisionedPoleId
    serial_number  NVARCHAR(32)  NOT NULL  -- the join key against Poles.ControllerId
    product_id     INT           NOT NULL  -- becomes PoleModelId (+1000, same offset as above)
    station_id     NVARCHAR(32)  NULL      -- NOT currently mapped anywhere (Poles has no
                                            -- catch-all "extra fields" column the way
                                            -- PoleModels/PoleTelemetry do) -- worth
                                            -- revisiting if this value turns out to be needed
    created_at     DATETIME2(7)  NOT NULL  -- becomes ProvisionedPoleCreatedDateTime, verbatim
"""

import os
import json
import logging
import time

from shared.sql_client import get_connection, get_provisioned_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.pole_models_loader import (
    _ALL_COLUMNS,
    _chunked,
    _STAGING_TABLE_SQL,
    _STAGING_INSERT_SQL,
    _MERGE_FROM_STAGING_SQL,
    _TRUNCATE_STAGING_SQL,
    _ROW_UPSERT_SQL,
    _UPSERT_BATCH_SIZE,
)

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Provisioned"

# Added to product_id to produce ModelId, per explicit request -- keeps
# this source's own ModelId range disjoint from Leadsun's own native
# ModelId values (which arrive as real integers straight from the
# Leadsun API, in a range this project doesn't control and has no
# guaranteed floor on). 1000 is treated as a large-enough gap for now;
# revisit if either source's own id range ever approaches it.
_PROVISIONED_MODEL_ID_OFFSET = 1000

_FETCH_PRODUCTS_SQL = "SELECT product_id, prefix, name, is_active FROM products"


def _fetch_provisioned_products() -> list:
    """
    Short-lived connection to the PROVISIONED database specifically --
    opened, queried, and closed immediately, well before any connection
    to THIS project's own database is opened for the write phase below.
    Same "don't hold a connection open across an unrelated round trip"
    discipline as every other loader's fetch phase here, just with a
    second SQL database standing in for what's normally an HTTP fetch
    (Airtable/Leadsun) -- the reasoning is identical either way.
    """
    conn = get_provisioned_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(_FETCH_PRODUCTS_SQL)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _map_product_row(row) -> dict:
    """
    Maps one (product_id, prefix, name, is_active) row into PoleModels'
    column shape, per explicit mapping:
      product_id + 1000 -> ModelId
      name              -> ModelName
      250 (fixed)       -> SunboardPower
      40  (fixed)       -> LightPower
    prefix/is_active aren't dropped -- captured in ExtraFieldsJson
    instead, same "don't silently lose source fields" convention
    pole_models_loader.py's own _map_model_record() already follows for
    its Leadsun source.
    """
    product_id, prefix, name, is_active = row
    return {
        "ModelId": product_id + _PROVISIONED_MODEL_ID_OFFSET,
        "ModelName": name,
        "SunboardPower": 250,
        "LightPower": 40,
        "ExtraFieldsJson": json.dumps({"Prefix": prefix, "IsActive": bool(is_active)}),
    }


def _build_row(mapped: dict, sp_exec_id) -> tuple:
    """Assembles the final param tuple in _ALL_COLUMNS order. A small,
    near-identical twin of pole_models_loader._build_row() rather than a
    direct import of it -- that function closes over ITS OWN module's
    SOURCE_NAME ('Leadsun'), so reusing it here would silently tag every
    provisioned row as Leadsun-sourced."""
    values = dict(mapped)
    values["Source"] = SOURCE_NAME
    values["SP_ExecId"] = sp_exec_id
    return tuple(values.get(col) for col in _ALL_COLUMNS)


def load_provisioned_pole_models() -> None:
    start_time = _to_dto_string(_now_eastern())
    sp_exec_id = None
    total_success = 0
    total_errors = 0
    conn = None
    cursor = None

    try:
        # 1. Short-lived connection to OUR db just for the SP_Execution
        # start row -- closed immediately rather than held open through
        # the provisioned-db fetch below, same reasoning as every other
        # loader's own fetch-then-write connection discipline here.
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "loadProvisionedPoleModels",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()
        conn = None
        cursor = None

        # 2. Pull every product from the provisioned database -- no
        # connection to OUR db open at all while this runs.
        fetch_start = time.perf_counter()
        products = _fetch_provisioned_products()
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadProvisionedPoleModels: fetched %d record(s) in %.1fs.",
            len(products),
            fetch_seconds,
        )

        # 3. Re-open a fresh connection to OUR db for the write phase,
        # and map + upsert in chunks (same staging-table/MERGE machinery
        # pole_models_loader.py's own Leadsun-sourced loader uses,
        # imported directly rather than duplicated).
        conn = get_connection()
        cursor = conn.cursor()
        cursor.fast_executemany = True

        upsert_start = time.perf_counter()
        param_rows = [_build_row(_map_product_row(row), sp_exec_id) for row in products]

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
                    "loadProvisionedPoleModels: chunk of %d failed to bulk-merge (%s); "
                    "retrying row-by-row.",
                    len(batch),
                    batch_error,
                )
                cursor.execute(_TRUNCATE_STAGING_SQL)
                for row in batch:
                    try:
                        cursor.execute(_ROW_UPSERT_SQL, row)
                        total_success += 1
                    except Exception as row_error:
                        total_errors += 1
                        logging.error(
                            "loadProvisionedPoleModels: failed to upsert %s: %s",
                            row[0],  # ModelId is the first positional param
                            row_error,
                        )

        conn.commit()
        logging.info(
            "loadProvisionedPoleModels: upsert phase took %.1fs for %d record(s).",
            time.perf_counter() - upsert_start,
            len(products),
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
            1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("loadProvisionedPoleModels: run failed: %s", ex)
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
                    "loadProvisionedPoleModels: also failed to record ErrorMessage on "
                    "SP_Execution: %s",
                    log_error,
                )
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


_FETCH_SERIALS_SQL = "SELECT device_uid, serial_number, product_id, created_at FROM serials"

# A real, non-# staging table -- same convention (and same reasoning:
# TRUNCATE + reuse across chunks, rather than a session-scoped #temp
# table) as pole_models_loader.py's own _STAGING_TABLE_SQL.
_SERIALS_STAGING_TABLE_SQL = """
IF OBJECT_ID('Staging_PoleSerials') IS NULL
BEGIN
    CREATE TABLE Staging_PoleSerials (
        SerialNumber                   NVARCHAR(32)  NOT NULL,
        ProvisionedPoleId              NVARCHAR(64)  NULL,
        PoleModelId                    INT           NULL,
        ProvisionedPoleCreatedDateTime DATETIME2(7)  NULL
    );
END
"""

_SERIALS_STAGING_INSERT_SQL = """
INSERT INTO Staging_PoleSerials
    (SerialNumber, ProvisionedPoleId, PoleModelId, ProvisionedPoleCreatedDateTime)
VALUES (?, ?, ?, ?)
"""

_SERIALS_TRUNCATE_STAGING_SQL = "TRUNCATE TABLE Staging_PoleSerials"

# UPDATE ONLY -- deliberately no WHEN NOT MATCHED / INSERT branch at
# all, per explicit request: a serials row whose own serial_number
# matches no CURRENT Poles.ControllerId is simply not applied to
# anything. Poles rows themselves only ever come from Airtable via
# load_poles() -- this loader never creates one.
_SERIALS_UPDATE_FROM_STAGING_SQL = """
UPDATE p
SET
    p.ProvisionedPoleId = s.ProvisionedPoleId,
    p.PoleModelId = s.PoleModelId,
    p.ProvisionedPoleCreatedDateTime = s.ProvisionedPoleCreatedDateTime
FROM Poles p
JOIN Staging_PoleSerials s ON p.ControllerId = s.SerialNumber
"""

_SERIALS_ROW_UPDATE_SQL = """
UPDATE Poles
SET ProvisionedPoleId = ?, PoleModelId = ?, ProvisionedPoleCreatedDateTime = ?
WHERE ControllerId = ?
"""


def _fetch_provisioned_serials() -> list:
    """Same short-lived-connection-to-the-PROVISIONED-database discipline
    as _fetch_provisioned_products() above."""
    conn = get_provisioned_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(_FETCH_SERIALS_SQL)
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def _map_serial_row(row) -> dict:
    """
    Maps one (device_uid, serial_number, product_id, created_at) row per
    explicit mapping:
      device_uid          -> ProvisionedPoleId
      product_id + 1000   -> PoleModelId (same offset, same reasoning,
                              as _map_product_row()'s own ModelId --
                              this is what makes a serial's own
                              PoleModelId land on the exact same row
                              load_provisioned_pole_models() wrote for
                              that same product_id)
      created_at           -> ProvisionedPoleCreatedDateTime, VERBATIM --
                              see this module's own docstring for why no
                              timezone conversion is applied.
    station_id is intentionally NOT mapped anywhere -- see this module's
    own docstring.
    """
    device_uid, serial_number, product_id, created_at = row
    return {
        "SerialNumber": serial_number,
        "ProvisionedPoleId": device_uid,
        "PoleModelId": product_id + _PROVISIONED_MODEL_ID_OFFSET,
        "ProvisionedPoleCreatedDateTime": created_at,
    }


def _dedupe_serials_by_serial_number(mapped_rows: list) -> list:
    """
    Guards against a real correctness hazard, not a hypothetical one: if
    the source `serials` table ever has more than one row for the same
    serial_number, a plain UPDATE ... FROM JOIN with multiple matching
    rows on the join's OTHER side has UNDEFINED behavior in SQL Server --
    which of the duplicate rows' own values actually get written is not
    guaranteed to be consistent from run to run. Deduplicating here, in
    Python, before anything is staged, makes the outcome deterministic:
    for a given serial_number, the row with the latest
    ProvisionedPoleCreatedDateTime wins -- the most recently
    (re-)provisioned record for that serial is treated as the current
    truth. Ties (identical timestamps) keep whichever appeared LAST in
    the fetched order, which is an arbitrary but stable tie-break, not a
    meaningful choice -- true duplicate timestamps for the same serial
    aren't expected to occur in practice.
    """
    by_serial_number = {}
    for mapped in mapped_rows:
        existing = by_serial_number.get(mapped["SerialNumber"])
        if existing is None or (
            mapped["ProvisionedPoleCreatedDateTime"] is not None
            and (
                existing["ProvisionedPoleCreatedDateTime"] is None
                or mapped["ProvisionedPoleCreatedDateTime"] >= existing["ProvisionedPoleCreatedDateTime"]
            )
        ):
            by_serial_number[mapped["SerialNumber"]] = mapped
    return list(by_serial_number.values())


def load_provisioned_pole_serials() -> None:
    start_time = _to_dto_string(_now_eastern())
    sp_exec_id = None
    total_success = 0
    total_errors = 0
    conn = None
    cursor = None

    try:
        # 1. Short-lived connection to OUR db just for the SP_Execution
        # start row.
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "loadProvisionedPoleSerials",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()
        cursor.close()
        conn.close()
        conn = None
        cursor = None

        # 2. Pull every serial from the provisioned database -- no
        # connection to OUR db open at all while this runs.
        fetch_start = time.perf_counter()
        serials = _fetch_provisioned_serials()
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadProvisionedPoleSerials: fetched %d record(s) in %.1fs.",
            len(serials),
            fetch_seconds,
        )

        mapped_rows = _dedupe_serials_by_serial_number(
            [_map_serial_row(row) for row in serials]
        )

        # 3. Re-open a fresh connection to OUR db for the write phase --
        # a bulk staged UPDATE, not an upsert (see this module's own
        # docstring: this loader never inserts new Poles rows).
        conn = get_connection()
        cursor = conn.cursor()
        cursor.fast_executemany = True

        upsert_start = time.perf_counter()
        param_rows = [
            (
                mapped["SerialNumber"],
                mapped["ProvisionedPoleId"],
                mapped["PoleModelId"],
                mapped["ProvisionedPoleCreatedDateTime"],
            )
            for mapped in mapped_rows
        ]

        if param_rows:
            cursor.execute(_SERIALS_STAGING_TABLE_SQL)

        for batch in _chunked(param_rows, _UPSERT_BATCH_SIZE):
            try:
                cursor.executemany(_SERIALS_STAGING_INSERT_SQL, batch)
                cursor.execute(_SERIALS_UPDATE_FROM_STAGING_SQL)
                cursor.execute(_SERIALS_TRUNCATE_STAGING_SQL)
                total_success += len(batch)
            except Exception as batch_error:
                logging.warning(
                    "loadProvisionedPoleSerials: chunk of %d failed to bulk-update (%s); "
                    "retrying row-by-row.",
                    len(batch),
                    batch_error,
                )
                cursor.execute(_SERIALS_TRUNCATE_STAGING_SQL)
                for row in batch:
                    serial_number, provisioned_pole_id, pole_model_id, created_at = row
                    try:
                        cursor.execute(
                            _SERIALS_ROW_UPDATE_SQL,
                            provisioned_pole_id,
                            pole_model_id,
                            created_at,
                            serial_number,
                        )
                        total_success += 1
                    except Exception as row_error:
                        total_errors += 1
                        logging.error(
                            "loadProvisionedPoleSerials: failed to update serial %s: %s",
                            serial_number,
                            row_error,
                        )

        conn.commit()
        logging.info(
            "loadProvisionedPoleSerials: update phase took %.1fs for %d record(s) "
            "(%d fetched, %d after deduping by serial number).",
            time.perf_counter() - upsert_start,
            len(param_rows),
            len(serials),
            len(mapped_rows),
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
            1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("loadProvisionedPoleSerials: run failed: %s", ex)
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
                    "loadProvisionedPoleSerials: also failed to record ErrorMessage on "
                    "SP_Execution: %s",
                    log_error,
                )
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
