import os
import json
import logging
import time
from datetime import datetime, timedelta, timezone

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
    "IsOpenIssueFault",  # NOT from Leadsun -- see _fetch_location_ids_with_open_issues()
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
_LOADER_OWNED_FIELDS = {"Source", "SP_ExecId", "IsOpenIssueFault"}

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


def _build_row(mapped: dict, sp_exec_id, is_open_issue_fault: bool) -> tuple:
    """Assembles the final param tuple in _ALL_COLUMNS order."""
    values = dict(mapped)
    values["Source"] = SOURCE_NAME
    values["SP_ExecId"] = sp_exec_id
    values["IsOpenIssueFault"] = is_open_issue_fault
    return tuple(values.get(col) for col in _ALL_COLUMNS)


def _fetch_location_ids_with_open_issues(cursor) -> set:
    """
    Every LocationId whose pole has at least one row in PoleOpenIssues --
    PoleOpenIssues.PoleId matches Poles.Id, not LocationId directly, so
    this needs the join through Poles. Fetched once per
    load_pole_telemetry() run (a single, cheap query -- PoleOpenIssues
    only ever holds currently-open issues, not the full issue history,
    so this stays small) rather than a per-row lookup, then checked via
    simple set membership when mapping each lamp record below.
    """
    cursor.execute(
        """
        SELECT DISTINCT p.LocationId
        FROM Poles p
        JOIN PoleOpenIssues poi ON poi.PoleId = p.Id
        WHERE p.LocationId IS NOT NULL
        """
    )
    return {row[0] for row in cursor.fetchall()}


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
    IsOpenIssueFault       BIT NULL,
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
        open_issue_location_ids = _fetch_location_ids_with_open_issues(cursor)
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
            is_open_issue_fault = mapped["LocationId"] in open_issue_location_ids
            param_rows.append(_build_row(mapped, sp_exec_id, is_open_issue_fault))

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


# One-off backfill for a real production bug: IsOpenIssueFault was
# written incorrectly for every PoleTelemetry row ingested before
# PoleOpenIssues.PoleId got fixed to source from Airtable's
# "PoleRecordID" field instead of "PoleId" (see
# pole_open_issues_loader.py's own comments on _map_record_to_issue for
# the full history) -- "PoleId" links to a synced/mirror table, not the
# real Poles table, so the JOIN this value depends on
# (_fetch_location_ids_with_open_issues() above) never matched
# correctly, meaning IsOpenIssueFault has likely been 0/False for
# essentially every pole regardless of whether it actually had an open
# issue, since this loader was first built.
#
# load_pole_telemetry() itself needs NO fix -- _fetch_location_ids_with_
# open_issues() already re-queries PoleOpenIssues/Poles fresh on every
# single run, so any NEW telemetry ingested after PoleOpenIssues.PoleId
# is corrected (i.e. after loadPoleOpenIssues runs again with that fix
# deployed) will automatically get the right IsOpenIssueFault value with
# no further action needed. This backfill exists ONLY for EXISTING rows,
# already ingested with the wrong value baked in, which nothing else
# would ever revisit.
#
# Deliberately scoped the SAME way as
# pole_vitals_loader.py's own backfill_last_48_hours_of_hour_for_all_
# poles() -- each pole's own last 48 hours, ending at that SAME pole's
# own latest reading, not a global cutoff relative to "now". This
# matters for consistency, not just symmetry: that Hour-vitals backfill
# reads ITS OWN 48-hour-per-pole window from PoleTelemetry.
# IsOpenIssueFault -- if this correction used a different, narrower
# scope (e.g. a plain "last 48 hours from now", which would miss an
# already-offline pole's own relevant window entirely), the Hour-vitals
# backfill would end up re-aggregating the SAME stale, uncorrected
# values for exactly the poles that backfill was built to help.
#
# PoleOpenIssues only ever holds CURRENTLY open issues, not a historical
# log of when each issue opened/closed -- there is no way to reconstruct
# whether a GIVEN past reading's pole genuinely had an open issue AT
# THAT EXACT MOMENT. This backfill applies TODAY's known open-issue
# state to each pole's own recent window as the best available
# correction, not a claim of full historical accuracy -- a real,
# accepted limitation of PoleOpenIssues' own data model, not an
# oversight here.
_BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL = """
;WITH LocationIdsWithOpenIssues AS (
    SELECT DISTINCT p.LocationId
    FROM Poles p
    JOIN PoleOpenIssues poi ON poi.PoleId = p.Id
    WHERE p.LocationId IS NOT NULL
),
MaxReadingPerPole AS (
    SELECT
        t.LocationId,
        MAX(t.LastUpload) AS MaxLastUpload
    FROM PoleTelemetry t
    WHERE t.LastUpload <> ?  -- exclude the missing-LastUpload sentinel (see _MISSING_LAST_UPLOAD_SENTINEL above)
    GROUP BY t.LocationId
)
UPDATE t
SET t.IsOpenIssueFault = CASE WHEN loi.LocationId IS NOT NULL THEN 1 ELSE 0 END
FROM PoleTelemetry t
JOIN MaxReadingPerPole mr ON t.LocationId = mr.LocationId
LEFT JOIN LocationIdsWithOpenIssues loi ON t.LocationId = loi.LocationId
WHERE t.LastUpload > DATEADD(HOUR, -48, mr.MaxLastUpload)
  AND t.LastUpload <= mr.MaxLastUpload
  AND ISNULL(t.IsOpenIssueFault, 0) <> CASE WHEN loi.LocationId IS NOT NULL THEN 1 ELSE 0 END;
"""


def backfill_is_open_issue_fault_for_all_poles() -> None:
    """
    One-off operation: corrects IsOpenIssueFault on EXISTING PoleTelemetry
    rows within each pole's own last 48 hours of activity (ending at that
    SAME pole's own latest reading, regardless of how old it is), using
    the NOW-corrected PoleOpenIssues.PoleId -> Poles.Id join. See
    _BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL's own comment for the full
    reasoning, including why this was needed at all (a real, confirmed
    production bug) and this backfill's own real limitation (it can only
    ever apply TODAY's known open-issue state, since PoleOpenIssues holds
    no history of past open/closed status).

    NOT needed for any telemetry ingested AFTER loadPoleOpenIssues runs
    with the corrected field mapping -- load_pole_telemetry() itself
    already re-resolves this fresh on every single run, so new readings
    get the right value automatically. This is purely for rows already
    written with the wrong value baked in before that point.

    Intended to be run manually, once, as a one-off correction after
    deploying the PoleOpenIssues.PoleId fix (and after running
    loadPoleOpenIssues at least once with that fix in place) -- NOT part
    of the normal, scheduled loadLeadsunData cycle. See
    scripts/backfill_is_open_issue_fault.py for how to invoke it.

    Run this BEFORE re-running
    pole_vitals_loader.backfill_last_48_hours_of_hour_for_all_poles() --
    that backfill only ever reads whatever IsOpenIssueFault is ALREADY
    stored on PoleTelemetry and aggregates it into PoleVitals; it cannot
    fix a wrong per-reading value itself. Running it before this one
    would just re-aggregate the same, still-incorrect values.
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
            "backfillIsOpenIssueFault",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. The single, set-based UPDATE covering every pole's own
        # 48-hour window at once.
        cursor.execute(_BACKFILL_IS_OPEN_ISSUE_FAULT_PER_POLE_SQL, _MISSING_LAST_UPLOAD_SENTINEL)
        total_success = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
        conn.commit()
        logging.info(
            "backfillIsOpenIssueFault: %d PoleTelemetry row(s) corrected across every pole's own "
            "last 48 hours of activity.",
            total_success,
        )

        # 3. Close out the SP_Execution row with final counts
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
        logging.error("backfillIsOpenIssueFault: run failed: %s", ex)
        if sp_exec_id:
            # Fresh connection for recording the failure -- same fix,
            # same reasoning, as pole_vitals_loader.py's own backfill
            # functions (the exception that got us here might BE a
            # connection-level failure, in which case reusing the same
            # connection/cursor to record it would just raise a SECOND
            # time, masking the original, more useful error).
            try:
                recovery_conn = get_connection()
                recovery_cursor = recovery_conn.cursor()
                try:
                    recovery_cursor.execute(
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
                    recovery_conn.commit()
                finally:
                    recovery_cursor.close()
                    recovery_conn.close()
            except Exception as recording_error:
                logging.error(
                    "backfillIsOpenIssueFault: additionally failed to record this run's failure "
                    "in SP_Execution (Id=%s): %s -- that row will be left with EndDateTime still "
                    "NULL. The ORIGINAL failure (%s) is what's actually raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()


def _aggregate_telemetry_by_leadsun_project(telemetry_rows) -> dict:
    """
    Groups PoleTelemetry rows into the nested structure explicitly
    requested for Projects.LeadsunProject's own "groups"/"products"
    shape -- one entry per distinct LeadsunProjectId, each holding that
    project's own ProjectName/UserName (both confirmed constant within a
    project -- validated directly against a real 11,837-record Leadsun
    /lamps response: 176 distinct projects, 0 with more than one distinct
    ProjectName or UserName across their own records) plus a list of
    distinct GroupId groups, each holding a list of that group's own
    distinct products.

    Field mapping, deliberately NOT a 1:1 rename of PoleTelemetry's own
    column names -- this disambiguates two genuinely different Leadsun
    identifiers that are easy to confuse with each other:
      ProductId        <- PoleTelemetry.LeadsunId (Leadsun's own raw "id"
                           field -- a plain integer, e.g. 10358)
      ProductName      <- PoleTelemetry.LocationId (Leadsun's own raw
                           "productName" field -- e.g. "12009-1000")
      ControllerCode   <- PoleTelemetry.ControllerCode (unchanged name)
      ProvidedProductId <- PoleTelemetry.ProductId (Leadsun's own raw
                           "productId" field -- a separate, ALPHANUMERIC
                           value, e.g. "AE3SAP7323113143", confirmed via
                           a real /lamps response -- genuinely NOT the
                           same identifier as ProductId/LeadsunId above,
                           despite the similar name)

    Keyed by LeadsunProjectId CAST TO STRING (via str()) -- matching how
    Projects.LeadsunProject's own "ProjectId" is stored (a JSON STRING,
    from Airtable, via json.dumps() in projects_loader.py), even though
    PoleTelemetry.LeadsunProjectId itself is a plain INT column. This is
    the join key between the two independently-sourced systems, so both
    sides need to agree on ONE common representation to match correctly
    -- string was chosen since Airtable's own side is the naturally
    string-shaped one (a text field), not because either representation
    is inherently more "correct".

    A group is identified by its own GroupId; a product is included
    exactly once per (project, group) it actually appears under in this
    telemetry snapshot -- NOT deduplicated further than that (e.g. two
    different rows for the very same pole, differing only in a field
    this function doesn't read, would still only contribute ONE product
    entry here, since GroupId/GatewayCode/ProductId/ControllerCode/
    LeadsunId/LocationId together are expected to already be that pole's
    own stable identity; if a genuinely different reading arrived for
    the exact same pole later in this same batch, the SECOND one's own
    values would silently replace the first's, since products are keyed
    by ProductId internally before being flattened into a plain list at
    the end).

    Each project also gets two summary counts, and each of its groups
    gets one:
      ProjectEntry["totalGateways"] = the number of distinct groups
        under that project -- "gateways" and "groups" are the same
        concept here (each group is identified by its own GatewayCode,
        per Leadsun's own data model), so this is just len(groups), not
        a separately-tracked count.
      ProjectEntry["totalPoles"]    = the number of distinct poles
        (products) across ALL of that project's groups combined.
      GroupEntry["totalPoles"]      = the number of distinct poles
        (products) within that ONE group alone.
    All three are derived counts, computed once at the very end after
    every row's been placed -- not maintained incrementally as rows
    stream in, since a product can still get overwritten/deduplicated
    mid-loop (see the note above), so counting early could overcount
    before the real, final product list settles.
    """
    projects: dict = {}

    for row in telemetry_rows:
        (
            leadsun_project_id,
            leadsun_project_name,
            user_name,
            group_id,
            group_name,
            gateway_code,
            leadsun_id,
            location_id,
            controller_code,
            product_id,
        ) = row

        if leadsun_project_id is None or group_id is None:
            # Can't place this reading into the nested structure at all
            # without knowing which project and group it belongs to --
            # skipped, not an error (a pole legitimately not yet
            # assigned to a Leadsun project/group shouldn't block every
            # OTHER pole's own aggregation).
            continue

        project_key = str(leadsun_project_id)
        project_entry = projects.setdefault(
            project_key,
            {"ProjectName": leadsun_project_name, "UserName": user_name, "groups": {}},
        )

        group_entry = project_entry["groups"].setdefault(
            group_id,
            {
                "GroupId": group_id,
                "GroupName": group_name,
                "GatewayCode": gateway_code,
                "products": {},
            },
        )

        group_entry["products"][leadsun_id] = {
            "ProductId": leadsun_id,
            "ProductName": location_id,
            "ControllerCode": controller_code,
            "ProvidedProductId": product_id,
        }

    # Flatten the internal, dedup-friendly dicts (keyed by GroupId/
    # ProductId) into the plain lists the requested JSON shape actually
    # needs -- the keying above only ever existed to make "have I
    # already seen this group/product" a cheap lookup while building
    # this structure, not part of the final, serialized shape itself.
    # totalGateways/totalPoles (project-level) and totalPoles
    # (group-level) are computed here too, from those same final,
    # deduplicated group/product dicts -- see this function's own
    # docstring for why that has to happen now, not incrementally
    # during the loop above.
    result = {}
    for project_key, project_entry in projects.items():
        groups = [
            {
                "GroupId": group_entry["GroupId"],
                "GroupName": group_entry["GroupName"],
                "GatewayCode": group_entry["GatewayCode"],
                "totalPoles": len(group_entry["products"]),
                "products": list(group_entry["products"].values()),
            }
            for group_entry in project_entry["groups"].values()
        ]
        result[project_key] = {
            "ProjectName": project_entry["ProjectName"],
            "UserName": project_entry["UserName"],
            "totalGateways": len(groups),
            "totalPoles": sum(group["totalPoles"] for group in groups),
            "groups": groups,
        }
    return result


_FETCH_PROJECT_LEADSUN_IDS_SQL = """
SELECT Id, JSON_VALUE(LeadsunProject, '$.ProjectId') AS LeadsunProjectIdValue
FROM Projects
WHERE JSON_VALUE(LeadsunProject, '$.ProjectId') IS NOT NULL
"""

_FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL = """
;WITH RecentTelemetry AS (
    SELECT
        LeadsunProjectId, LeadsunProjectName, UserName, GroupId, GroupName,
        GatewayCode, LeadsunId, LocationId, ControllerCode, ProductId,
        ROW_NUMBER() OVER (PARTITION BY LocationId ORDER BY LastUpload DESC) AS rn
    FROM PoleTelemetry
    WHERE LeadsunProjectId IS NOT NULL
      AND LastUpload >= ?
)
SELECT LeadsunProjectId, LeadsunProjectName, UserName, GroupId, GroupName,
       GatewayCode, LeadsunId, LocationId, ControllerCode, ProductId
FROM RecentTelemetry
WHERE rn = 1
"""

# How far back to look for "currently reporting" telemetry when building
# each Project's own groups/products -- NOT a scan of PoleTelemetry's
# entire 6-month retention window, which is what an earlier, buggy
# version of this query did (WHERE LeadsunProjectId IS NOT NULL alone,
# with no time bound at all -- LeadsunProjectId doesn't change over a
# pole's own history, so that matched EVERY historical row for EVERY
# pole ever recorded, not just its current state; a real production
# incident -- update_leadsun_project_details() ran immediately after
# load_pole_telemetry() finished, then failed itself after a long,
# unexplained delay, root-caused to exactly this unbounded scan).
#
# This function runs every 30 minutes, immediately after
# load_pole_telemetry() has just refreshed every currently-reporting
# pole's own row -- so a pole that's genuinely still active will always
# have a LastUpload well within this window. 3 hours (matching this
# project's own established "recent lookback" convention elsewhere,
# e.g. pole_vitals_loader.py's own _DEFAULT_LOOKBACK["Hour"]) gives
# comfortable headroom for a late/delayed reading without ever
# approaching a full-table scan. A pole that hasn't reported at all
# within this window simply drops out of its project's own groups/
# products until it reports again -- an accepted tradeoff, not an
# oversight: the whole point of this structure is to reflect what's
# CURRENTLY active, not a pole's own full historical presence.
_PROJECT_DETAILS_LOOKBACK = timedelta(hours=3)

_UPDATE_PROJECT_LEADSUN_PROJECT_SQL = """
UPDATE Projects SET LeadsunProject = ? WHERE Id = ?
"""


def update_leadsun_project_details() -> None:
    """
    Enriches every Project's own LeadsunProject JSON with the full
    ProjectName/UserName/groups/products structure, aggregated fresh
    from whatever PoleTelemetry currently holds -- run this AFTER
    load_pole_telemetry() has refreshed that table (see
    function_app.py's own loadLeadsunData/loadLeadsunDataManual, where
    this is wired in immediately after it), not on its own separate
    schedule -- there would be nothing new to aggregate otherwise.

    Deliberately does NOT touch a Project whose own "ProjectId" has no
    matching PoleTelemetry rows at all (e.g. Airtable hasn't been given
    a Leadsun ProjectID for it yet, or none of its poles have reported
    telemetry yet) -- that Project's own LeadsunProject value (whatever
    it currently is, even just the bare {"ProjectId": ...} shape
    projects_loader.py itself writes) is left completely alone, not
    cleared out or reset. Only a Project that DOES have at least one
    matching telemetry reading gets its own groups/products rebuilt,
    fresh, from this run's own data -- overwriting whatever groups/
    products it had from a PREVIOUS run of this same function, but never
    touching the "ProjectId" key that projects_loader.py itself owns.

    See _aggregate_telemetry_by_leadsun_project()'s own docstring for the
    full field-mapping/structure reasoning.
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
            "updateLeadsunProjectDetails",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        # 2. Which Projects even have a Leadsun ProjectID recorded at all
        cursor.execute(_FETCH_PROJECT_LEADSUN_IDS_SQL)
        project_id_by_leadsun_project_id = {
            leadsun_project_id_value: project_id
            for project_id, leadsun_project_id_value in cursor.fetchall()
        }

        # 3. Only RECENT telemetry (see _PROJECT_DETAILS_LOOKBACK's own
        # comment for why this must be bounded, not a scan of this
        # table's entire 6-month retention window). _to_dto_string(),
        # not the raw datetime object -- same fix, same reasoning, as
        # this project's other DATETIMEOFFSET bindings: pyodbc silently
        # converts a timezone-aware Python datetime to UTC on bind
        # unless it's already an explicit offset string.
        cutoff = _to_dto_string(_now_eastern() - _PROJECT_DETAILS_LOOKBACK)
        cursor.execute(_FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL, cutoff)
        telemetry_rows = cursor.fetchall()

        # 4. Group in Python (see that function's own docstring for the
        # full field-mapping reasoning)
        aggregated_by_leadsun_project_id = _aggregate_telemetry_by_leadsun_project(
            telemetry_rows
        )

        # 5. Update only the Projects that actually have matching
        # telemetry -- see this function's own docstring for why a
        # Project with none is deliberately left untouched, not cleared.
        for leadsun_project_id_str, aggregated in aggregated_by_leadsun_project_id.items():
            project_id = project_id_by_leadsun_project_id.get(leadsun_project_id_str)
            if project_id is None:
                continue

            leadsun_project_json = json.dumps(
                {
                    "ProjectId": leadsun_project_id_str,
                    "ProjectName": aggregated["ProjectName"],
                    "UserName": aggregated["UserName"],
                    "totalGateways": aggregated["totalGateways"],
                    "totalPoles": aggregated["totalPoles"],
                    "groups": aggregated["groups"],
                }
            )
            cursor.execute(_UPDATE_PROJECT_LEADSUN_PROJECT_SQL, leadsun_project_json, project_id)
            total_success += 1
        conn.commit()

        logging.info(
            "updateLeadsunProjectDetails: %d project(s) updated with fresh "
            "groups/products from %d telemetry reading(s).",
            total_success,
            len(telemetry_rows),
        )

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
            1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("updateLeadsunProjectDetails: run failed: %s", ex)
        if sp_exec_id:
            # Fresh connection for recording the failure -- same fix,
            # same reasoning, as this project's other loaders (a
            # connection-level failure mid-run can leave the ORIGINAL
            # conn/cursor unusable for anything further, including
            # recording that same failure).
            try:
                recovery_conn = get_connection()
                recovery_cursor = recovery_conn.cursor()
                try:
                    recovery_cursor.execute(
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
                    recovery_conn.commit()
                finally:
                    recovery_cursor.close()
                    recovery_conn.close()
            except Exception as recording_error:
                logging.error(
                    "updateLeadsunProjectDetails: additionally failed to record this run's "
                    "failure in SP_Execution (Id=%s): %s -- that row will be left with "
                    "EndDateTime still NULL. The ORIGINAL failure (%s) is what's actually "
                    "raised below, not this one.",
                    sp_exec_id,
                    recording_error,
                    ex,
                )
        raise
    finally:
        cursor.close()
        conn.close()
