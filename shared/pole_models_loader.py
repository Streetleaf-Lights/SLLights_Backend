import os
import json
import logging
import time

from shared.leadsun_client import fetch_models
from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Leadsun"

# Same reasoning as poles_loader.py/pole_telemetry_loader.py: bulk-stage a
# chunk, then run one set-based MERGE against the whole chunk instead of
# one MERGE execution per row. PoleModels is a small reference table (a
# catalog of device models, not per-device telemetry), so the volume here
# doesn't need this the way Poles/PoleRawData do -- kept anyway for
# consistency with the rest of the Leadsun pipeline.
_UPSERT_BATCH_SIZE = 2000


def _chunked(items, size):
    """Splits a list into consecutive chunks of at most `size` items each."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _capitalize_key(key: str) -> str:
    """
    camelCase/lowerCamel -> PascalCase, matching this project's column
    naming convention. Only the first letter is uppercased -- Python's
    str.capitalize() would incorrectly lowercase the rest of the string
    too (e.g. "modelName".capitalize() -> "Modelname", not "ModelName").
    """
    return key[0].upper() + key[1:] if key else key


# Fields confirmed (against a real /models response) to arrive as
# numeric-looking strings ("80", "12.8", ...) that should be stored as
# actual numbers, not text.
#
# NOT included: "LampsUsing" ("00000001" in the sample) -- despite looking
# numeric, this reads as a bitmask-style string (same pattern as
# PoleRawData's SolarBoardDcStatus/LampBatteryStatus), where leading zeros
# are meaningful. Converting it to an int would silently lose that.
# "ModelId" isn't listed either: it arrives as a real JSON integer
# already, not a string, so there's nothing to convert.
_NUMERIC_STRING_FIELDS = {
    "SunboardPower",
    "LightPower",
    "Battery",
    "SystemVoltage",
    "BatteryVoltage",
    "BatteryCapacity1",
    "BatteryCapacity2",
    "SolarBoardVoltage",
}


def _parse_numeric_string(value):
    """
    Converts a numeric-looking string to int (no decimal point) or float.
    Returns None for missing/empty values, or the original value unchanged
    if it isn't numeric at all (shouldn't happen for the known numeric
    fields, but a safe fallback rather than raising).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value  # already a real number -- pass through unchanged
    stripped = value.strip()
    if stripped == "":
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return value


# Confirmed against a real Leadsun /models response. Order here is the
# single source of truth for column order everywhere below (DDL, staging
# table, INSERT/UPDATE column lists, and the Python param tuple).
#
# ExtraFieldsJson is a safety net, not part of the confirmed API shape:
# any key Leadsun sends that ISN'T one of the columns below (e.g. a field
# added in a future firmware/API update) gets captured there instead of
# silently dropped -- same pattern as PoleRawData.
_ALL_COLUMNS = [
    "ModelId",  # PK -- native JSON integer from the API, no rename needed
    "Source",
    "SP_ExecId",
    "ModelName",
    "SunboardPower",
    "LightPower",
    "Battery",
    "SystemVoltage",
    "CommType",
    "LightDisType",
    "IconUrl",
    "LampsUsing",
    "BatteryVoltage",
    "IsAc",
    "IsDcOut",
    "ModelSeries",
    "BatteryCapacity1",
    "BatteryCapacity2",
    "SolarBoardVoltage",
    "ExtraFieldsJson",
]

_PK_COLUMNS = ["ModelId"]
_NON_KEY_COLUMNS = [c for c in _ALL_COLUMNS if c not in _PK_COLUMNS]
# SP_ExecId is always refreshed regardless of whether anything else
# changed (same convention as every other loader here), so it's excluded
# from the "did anything actually change" diff check but still appears in
# the UPDATE SET list above.
_DIFF_CHECK_COLUMNS = [c for c in _NON_KEY_COLUMNS if c != "SP_ExecId"]

_LOADER_OWNED_FIELDS = {"Source", "SP_ExecId"}
_API_DATA_COLUMNS = [
    c for c in _ALL_COLUMNS if c not in _LOADER_OWNED_FIELDS and c != "ExtraFieldsJson"
]


def _map_model_record(record: dict) -> dict:
    """
    Maps one raw Leadsun model record into PoleModels' shape: every
    confirmed field gets its own typed column (see _ALL_COLUMNS), with
    known numeric-as-string fields converted to real numbers. Anything NOT
    in the known column list is captured in ExtraFieldsJson instead of
    dropped.

    Returns a dict keyed by _API_DATA_COLUMNS (everything except Source/
    SP_ExecId, which the caller adds -- they aren't sourced from the API).
    """
    capitalized = {}
    for raw_key, value in record.items():
        key = _capitalize_key(raw_key)
        if isinstance(value, str):
            value = value.strip()
        capitalized[key] = value

    for field in _NUMERIC_STRING_FIELDS:
        if field in capitalized:
            capitalized[field] = _parse_numeric_string(capitalized[field])

    extra = {k: v for k, v in capitalized.items() if k not in _API_DATA_COLUMNS}

    result = {col: capitalized.get(col) for col in _API_DATA_COLUMNS}
    result["ExtraFieldsJson"] = json.dumps(extra, default=str) if extra else None
    return result


def _build_row(mapped: dict, sp_exec_id) -> tuple:
    """Assembles the final param tuple in _ALL_COLUMNS order."""
    values = dict(mapped)
    values["Source"] = SOURCE_NAME
    values["SP_ExecId"] = sp_exec_id
    return tuple(values.get(col) for col in _ALL_COLUMNS)


def _sql_column_list(columns: list) -> str:
    return ", ".join(columns)


def _sql_placeholder_list(columns: list) -> str:
    return ", ".join("?" for _ in columns)


def _sql_source_select_list(columns: list) -> str:
    return ", ".join(f"? AS {c}" for c in columns)


def _sql_update_set_list(columns: list) -> str:
    return ",\n    ".join(f"{c} = source.{c}" for c in columns)


def _sql_insert_values_list(columns: list) -> str:
    return ", ".join(f"source.{c}" for c in columns)


def _sql_diff_select_list(columns: list, prefix: str) -> str:
    # CAST(...AS NVARCHAR(MAX)) on ExtraFieldsJson guards against the same
    # ntext/INTERSECT bug that hit Projects' PoleNumbers/PoleIds once a
    # JSON-encoded string crosses pyodbc's length threshold for guessing
    # ntext -- unlikely to matter here (it's usually empty), but cheap
    # insurance.
    parts = []
    for c in columns:
        if c == "ExtraFieldsJson":
            parts.append(f"CAST({prefix}.{c} AS NVARCHAR(MAX)) AS {c}")
        else:
            parts.append(f"{prefix}.{c}")
    return ", ".join(parts)


_STAGING_TABLE_SQL = """
IF OBJECT_ID('tempdb..#PoleModelsStaging') IS NOT NULL DROP TABLE #PoleModelsStaging;
CREATE TABLE #PoleModelsStaging (
    ModelId           INT           NULL,
    Source            VARCHAR(50)   NULL,
    SP_ExecId         INT           NULL,
    ModelName         NVARCHAR(100) NULL,
    SunboardPower     FLOAT         NULL,
    LightPower        FLOAT         NULL,
    Battery           FLOAT         NULL,
    SystemVoltage     FLOAT         NULL,
    CommType          NVARCHAR(50)  NULL,
    LightDisType      NVARCHAR(50)  NULL,
    IconUrl           NVARCHAR(500) NULL,
    LampsUsing        VARCHAR(20)   NULL,
    BatteryVoltage    FLOAT         NULL,
    IsAc              BIT           NULL,
    IsDcOut           BIT           NULL,
    ModelSeries       NVARCHAR(100) NULL,
    BatteryCapacity1  FLOAT         NULL,
    BatteryCapacity2  FLOAT         NULL,
    SolarBoardVoltage FLOAT         NULL,
    ExtraFieldsJson   NVARCHAR(MAX) NULL
);
"""

_STAGING_INSERT_SQL = (
    f"INSERT INTO #PoleModelsStaging ({_sql_column_list(_ALL_COLUMNS)})\n"
    f"VALUES ({_sql_placeholder_list(_ALL_COLUMNS)})"
)

# Diff-checked via INTERSECT (NULL-safe across the mixed column types
# here -- floats, a bit, strings).
_MERGE_FROM_STAGING_SQL = f"""
MERGE PoleModels AS target
USING #PoleModelsStaging AS source
ON target.ModelId = source.ModelId
WHEN MATCHED AND NOT EXISTS (
    SELECT {_sql_diff_select_list(_DIFF_CHECK_COLUMNS, 'target')}
    INTERSECT
    SELECT {_sql_diff_select_list(_DIFF_CHECK_COLUMNS, 'source')}
)
THEN UPDATE SET
    {_sql_update_set_list(_NON_KEY_COLUMNS)}
WHEN NOT MATCHED THEN
    INSERT ({_sql_column_list(_ALL_COLUMNS)})
    VALUES ({_sql_insert_values_list(_ALL_COLUMNS)});
"""

_TRUNCATE_STAGING_SQL = "TRUNCATE TABLE #PoleModelsStaging"

# Single-row fallback, used only if a chunk's bulk staging+merge fails.
_ROW_UPSERT_SQL = f"""
MERGE PoleModels AS target
USING (
    SELECT {_sql_source_select_list(_ALL_COLUMNS)}
) AS source
ON target.ModelId = source.ModelId
WHEN MATCHED AND NOT EXISTS (
    SELECT {_sql_diff_select_list(_DIFF_CHECK_COLUMNS, 'target')}
    INTERSECT
    SELECT {_sql_diff_select_list(_DIFF_CHECK_COLUMNS, 'source')}
)
THEN UPDATE SET
    {_sql_update_set_list(_NON_KEY_COLUMNS)}
WHEN NOT MATCHED THEN
    INSERT ({_sql_column_list(_ALL_COLUMNS)})
    VALUES ({_sql_insert_values_list(_ALL_COLUMNS)});
"""


def load_pole_models() -> None:
    start_time = _to_dto_string(_now_eastern())
    conn = get_connection()
    cursor = conn.cursor()
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
            "loadPoleModels",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Pull every model record from Leadsun before doing any DB writes
        fetch_start = time.perf_counter()
        models = fetch_models()
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadPoleModels: fetched %d record(s) in %.1fs.",
            len(models),
            fetch_seconds,
        )

        # 3. Map + upsert in chunks. ModelId is required (it's the PK) --
        # missing it is a row-level error and skipped.
        upsert_start = time.perf_counter()
        param_rows = []
        for model in models:
            mapped = _map_model_record(model)
            if mapped.get("ModelId") is None:
                total_errors += 1
                logging.error(
                    "loadPoleModels: skipping record with missing ModelId: %s",
                    mapped,
                )
                continue
            param_rows.append(_build_row(mapped, sp_exec_id))

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
                    "loadPoleModels: chunk of %d failed to bulk-merge (%s); retrying row-by-row.",
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
                            "loadPoleModels: failed to upsert %s: %s",
                            row[0],  # ModelId is the first positional param
                            row_error,
                        )

        conn.commit()
        logging.info(
            "loadPoleModels: upsert phase took %.1fs for %d record(s).",
            time.perf_counter() - upsert_start,
            len(models),
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
        logging.error("loadPoleModels: run failed: %s", ex)
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
