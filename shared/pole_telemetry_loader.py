import os
import json
import logging
import time
from datetime import datetime, timezone

from shared.leadsun_client import fetch_lamps
from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")

SOURCE_NAME = "Leadsun"
RETENTION_MONTHS = 6

# LastUpload is half of PoleTelemetry's composite PRIMARY KEY (LocationId,
# LastUpload), so it can never be NULL -- but a handful of real records
# come back from Leadsun with lastUpload genuinely null (a device that
# hasn't reported an upload time yet). Rather than drop those records,
# missing LastUpload gets this stable, far-future placeholder instead:
#   - stays a valid NOT NULL value, so the PK/upsert-match still works
#   - it's the SAME value every run for the same record, so a device that
#     keeps reporting null lastUpload gets its one row updated in place
#     each cycle instead of a new row inserted every 10 minutes
#   - being far in the future, it's never "< 6 months ago", so the
#     retention purge naturally never deletes it -- no special-case
#     exclusion needed there
# If a device later starts reporting a real lastUpload, that lands in a
# new row (real timestamp != sentinel) and this sentinel row is orphaned
# harmlessly -- it won't be purged since it never ages past the cutoff,
# but it's also no longer being updated. Acceptable for now; flag if it
# ever needs an explicit cleanup pass.
_MISSING_LAST_UPLOAD_SENTINEL = "9999-12-31 23:59:59.999 +00:00"

# Same reasoning as poles_loader.py: bulk-stage a chunk, then run one
# set-based MERGE against the whole chunk instead of one MERGE execution
# per row. Adopted from day one here rather than starting naive, since the
# tradeoff (bad row fails a whole chunk, with row-by-row fallback) is
# already proven out for Poles.
_UPSERT_BATCH_SIZE = 2000


def _chunked(items, size):
    """Splits a list into consecutive chunks of at most `size` items each."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _capitalize_key(key: str) -> str:
    """
    camelCase/lowerCamel -> PascalCase, matching this project's column
    naming convention (Id, Name, PoleNumber, ...). Only the first letter is
    uppercased -- Python's str.capitalize() would incorrectly lowercase the
    rest of the string too (e.g. "lastUpload".capitalize() -> "Lastupload",
    not "LastUpload").
    """
    return key[0].upper() + key[1:] if key else key


# Renames applied after generic capitalization:
#   - "productName" -> LocationId is the explicit rename this table was
#     built around.
#   - "id"/"projectId"/"projectName" are Leadsun's OWN internal ids, not
#     ours. Left as-is they'd read as "ProjectId"/"Id" -- which in every
#     other table in this project means "the row's Airtable-sourced primary
#     key" / "a link to our Projects table". Neither is true here, so
#     they're prefixed to avoid that exact confusion.
_KEY_RENAMES = {
    "ProductName": "LocationId",
    "Id": "LeadsunId",
    "ProjectId": "LeadsunProjectId",
    "ProjectName": "LeadsunProjectName",
}

# Confirmed against a real Leadsun /lamps response. Order here is the
# single source of truth for column order everywhere below (DDL, staging
# table, INSERT/UPDATE column lists, and the Python param tuple) -- change
# it here, nowhere else needs to move in lockstep.
#
# ExtraFieldsJson is a safety net, not part of the confirmed API shape: any
# key Leadsun sends that ISN'T one of the columns below (e.g. a field added
# in a future firmware/API update) gets captured there instead of silently
# dropped.
_ALL_COLUMNS = [
    "LocationId",  # PK part 1 -- from productName
    "LastUpload",  # PK part 2
    "Source",
    "SP_ExecId",
    "BatteryVoltage1",
    "BatteryVoltage2",
    "BatteryElecCurrent1",
    "BatteryElecCurrent2",
    "LampPower1",
    "LampPower2",
    "SolarBoardVoltage",
    "SolarBoardElecCurrent",
    "DcInVoltage",
    "BatteryOutElecCurrent",
    "BatteryTemperature1",
    "BatteryTemperature2",
    "McuTemperature",
    "EnvTemperature",
    "LightingState",
    "DcInState",
    "DcOutState",
    "SolarBoardState",
    "Battery1State",
    "Battery2State",
    "Lamp1State",
    "Lamp2State",
    "ControllerCode",
    "ProductId",
    "CreateTime",
    "SolarBoardDcStatus",
    "LampBatteryStatus",
    "UserName",
    "LeadsunId",
    "GroupId",
    "GroupName",
    "GatewayCode",
    "LeadsunProjectId",
    "LeadsunProjectName",
    "ModelId",
    "IsOnline",
    "TimeoutFlag",
    "Longitude",
    "Latitude",
    "ControlModelCode",
    "ControlModelName",
    "ExtraFieldsJson",
]

_PK_COLUMNS = ["LocationId", "LastUpload"]
_NON_KEY_COLUMNS = [c for c in _ALL_COLUMNS if c not in _PK_COLUMNS]
# SP_ExecId is always refreshed regardless of whether anything else
# changed (same convention as Customers/Projects/Poles), so it's excluded
# from the "did anything actually change" diff check but still appears in
# the UPDATE SET list above.
_DIFF_CHECK_COLUMNS = [c for c in _NON_KEY_COLUMNS if c != "SP_ExecId"]

# Fields Leadsun sends that aren't part of PoleTelemetry's stored columns at
# all (they're added by this loader, not read from the API).
_LOADER_OWNED_FIELDS = {"Source", "SP_ExecId"}

_API_DATA_COLUMNS = [
    c for c in _ALL_COLUMNS if c not in _LOADER_OWNED_FIELDS and c != "ExtraFieldsJson"
]


def _parse_iso_datetime(value):
    """
    Parses LastUpload/CreateTime, e.g. "2026-07-15T12:35:30.000+00:00"
    (confirmed against a real Leadsun response). Returns a
    DATETIMEOFFSET-ready string, or None if value is missing/null/
    unparseable.
    """
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _to_dto_string(dt)
    except (ValueError, TypeError):
        return None


def _map_lamp_record(record: dict) -> dict:
    """
    Maps one raw Leadsun lamp record into PoleTelemetry's shape: every
    confirmed field gets its own typed column (see _ALL_COLUMNS); anything
    NOT in that list is captured in ExtraFieldsJson instead of dropped.
    String values are trimmed (Leadsun sends at least one field --
    lightingState -- with stray trailing whitespace in practice).

    Returns a dict keyed by _API_DATA_COLUMNS (everything except Source/
    SP_ExecId, which the caller adds -- they aren't sourced from the API).
    """
    capitalized = {}
    for raw_key, value in record.items():
        key = _capitalize_key(raw_key)
        key = _KEY_RENAMES.get(key, key)
        if isinstance(value, str):
            value = value.strip()
        capitalized[key] = value

    # CreateTime has no PK/NOT NULL constraint, so it can stay genuinely
    # None when missing or unparseable -- no sentinel needed there.
    if "CreateTime" in capitalized:
        capitalized["CreateTime"] = _parse_iso_datetime(capitalized["CreateTime"])

    # LastUpload is different: it's part of the primary key, so it can
    # never be None going into the row. A value that's missing/null gets
    # the sentinel (a legitimate, expected case -- see
    # _MISSING_LAST_UPLOAD_SENTINEL above). A value that's PRESENT but
    # fails to parse is left as None on purpose -- that's a real parsing
    # problem (unexpected format), not a legitimately-missing timestamp,
    # and load_pole_telemetry() still treats it as a row-level error rather
    # than silently sentineling over what might be a bug.
    raw_last_upload = capitalized.get("LastUpload")
    if raw_last_upload in (None, ""):
        capitalized["LastUpload"] = _MISSING_LAST_UPLOAD_SENTINEL
    else:
        capitalized["LastUpload"] = _parse_iso_datetime(raw_last_upload)

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


_STAGING_TABLE_SQL = f"""
IF OBJECT_ID('tempdb..#PoleTelemetryStaging') IS NOT NULL DROP TABLE #PoleTelemetryStaging;
CREATE TABLE #PoleTelemetryStaging (
    LocationId  NVARCHAR(100)     NULL,
    LastUpload  DATETIMEOFFSET(3) NULL,
    Source      VARCHAR(50)       NULL,
    SP_ExecId   INT               NULL,
    BatteryVoltage1        FLOAT NULL,
    BatteryVoltage2        FLOAT NULL,
    BatteryElecCurrent1    FLOAT NULL,
    BatteryElecCurrent2    FLOAT NULL,
    LampPower1             FLOAT NULL,
    LampPower2             FLOAT NULL,
    SolarBoardVoltage      FLOAT NULL,
    SolarBoardElecCurrent  FLOAT NULL,
    DcInVoltage            FLOAT NULL,
    BatteryOutElecCurrent  FLOAT NULL,
    BatteryTemperature1    FLOAT NULL,
    BatteryTemperature2    FLOAT NULL,
    McuTemperature         FLOAT NULL,
    EnvTemperature         FLOAT NULL,
    LightingState          NVARCHAR(50) NULL,
    DcInState              INT NULL,
    DcOutState             INT NULL,
    SolarBoardState        INT NULL,
    Battery1State          INT NULL,
    Battery2State          INT NULL,
    Lamp1State             INT NULL,
    Lamp2State             INT NULL,
    ControllerCode         NVARCHAR(50) NULL,
    ProductId              NVARCHAR(50) NULL,
    CreateTime             DATETIMEOFFSET(3) NULL,
    SolarBoardDcStatus     VARCHAR(20) NULL,
    LampBatteryStatus      VARCHAR(20) NULL,
    UserName               NVARCHAR(100) NULL,
    LeadsunId              INT NULL,
    GroupId                INT NULL,
    GroupName              NVARCHAR(200) NULL,
    GatewayCode            NVARCHAR(50) NULL,
    LeadsunProjectId       INT NULL,
    LeadsunProjectName     NVARCHAR(200) NULL,
    ModelId                INT NULL,
    IsOnline               BIT NULL,
    TimeoutFlag            INT NULL,
    Longitude              FLOAT NULL,
    Latitude               FLOAT NULL,
    ControlModelCode       VARCHAR(50) NULL,
    ControlModelName       NVARCHAR(100) NULL,
    ExtraFieldsJson        NVARCHAR(MAX) NULL
);
"""

_STAGING_INSERT_SQL = (
    f"INSERT INTO #PoleTelemetryStaging ({_sql_column_list(_ALL_COLUMNS)})\n"
    f"VALUES ({_sql_placeholder_list(_ALL_COLUMNS)})"
)

# Diff-checked via INTERSECT (NULL-safe across the mixed column types
# here -- floats, ints, strings, datetimes, a bit).
_MERGE_FROM_STAGING_SQL = f"""
MERGE PoleTelemetry AS target
USING #PoleTelemetryStaging AS source
ON target.LocationId = source.LocationId AND target.LastUpload = source.LastUpload
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

_TRUNCATE_STAGING_SQL = "TRUNCATE TABLE #PoleTelemetryStaging"

# Single-row fallback, used only if a chunk's bulk staging+merge fails.
_ROW_UPSERT_SQL = f"""
MERGE PoleTelemetry AS target
USING (
    SELECT {_sql_source_select_list(_ALL_COLUMNS)}
) AS source
ON target.LocationId = source.LocationId AND target.LastUpload = source.LastUpload
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

# SYSDATETIMEOFFSET() matches LastUpload's DATETIMEOFFSET type; SQL Server
# compares datetimeoffset values by actual UTC instant, so this is correct
# regardless of what offset a given LastUpload was stored with.
_RETENTION_PURGE_SQL = f"""
DELETE FROM PoleTelemetry WHERE LastUpload < DATEADD(MONTH, -{RETENTION_MONTHS}, SYSDATETIMEOFFSET())
"""


def load_pole_telemetry() -> None:
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
            "loadPoleTelemetry",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Pull every lamp record from Leadsun before doing any DB writes
        fetch_start = time.perf_counter()
        lamps = fetch_lamps()
        fetch_seconds = time.perf_counter() - fetch_start
        logging.info(
            "loadPoleTelemetry: fetched %d record(s) in %.1fs.",
            len(lamps),
            fetch_seconds,
        )

        # 3. Map + upsert in chunks (stage a chunk, one set-based MERGE,
        # truncate, repeat). Records missing LocationId or a parseable
        # LastUpload are counted as row-level errors and skipped -- both
        # are part of PoleTelemetry's primary key, so neither can be NULL.
        upsert_start = time.perf_counter()
        param_rows = []
        for lamp in lamps:
            mapped = _map_lamp_record(lamp)
            if mapped["LocationId"] is None or mapped["LastUpload"] is None:
                total_errors += 1
                logging.error(
                    "loadPoleTelemetry: skipping record with missing LocationId/LastUpload: %s",
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
                    "loadPoleTelemetry: chunk of %d failed to bulk-merge (%s); retrying row-by-row.",
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
                            "loadPoleTelemetry: failed to upsert %s: %s",
                            row[0],  # LocationId is the first positional param
                            row_error,
                        )

        conn.commit()
        logging.info(
            "loadPoleTelemetry: upsert phase took %.1fs for %d record(s).",
            time.perf_counter() - upsert_start,
            len(lamps),
        )

        # 4. Retention: drop anything older than RETENTION_MONTHS based on
        # LastUpload. Runs every invocation (every 10 minutes) rather than
        # on a separate schedule, since it's a cheap indexed DELETE.
        cursor.execute(_RETENTION_PURGE_SQL)
        purged_count = cursor.rowcount
        conn.commit()
        logging.info(
            "loadPoleTelemetry: purged %d record(s) older than %d months.",
            purged_count,
            RETENTION_MONTHS,
        )

        # 5. Close out the SP_Execution row with final counts
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
        logging.error("loadPoleTelemetry: run failed: %s", ex)
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
