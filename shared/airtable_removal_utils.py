"""
Shared logic for flagging records that no longer appear in Airtable's own
feed -- used identically by customers_loader.py, projects_loader.py, and
poles_loader.py, per explicit request, after each one has finished its own
upsert phase (see each loader's own load_X() for exactly where this is
called).

Writes to a column named Active, not something Airtable-specific like
"IsRemovedFromAirtable" -- per explicit request, so the same column
reads sensibly if a future source OTHER than Airtable ever needs to
reconcile against this same table. Active = 1 means "present in the
most recent successful feed"; Active = 0 means "no longer present".
This function's own logic is still specifically about reconciling
against Airtable's own fetch (see its own docstring), even though the
column it writes doesn't say so by name -- a genuinely different future
source would need its own, separate reconciliation function, not a
generic one this module happens to already have.

Deliberately NOT expressed as a MERGE ... WHEN NOT MATCHED BY SOURCE clause
tacked onto any of those loaders' own existing upsert statements, even
though that's SQL Server's own native mechanism for exactly this kind of
"target rows absent from source" detection. Neither loader's own upsert
shape has a single MERGE that ever sees the COMPLETE current Airtable
fetch all at once: Customers/Projects process one record at a time (a
"source" of one row), and Poles' own staging table only ever holds one
_UPSERT_BATCH_SIZE-sized chunk at a time, never the full fetch. Using
WHEN NOT MATCHED BY SOURCE against either of those would incorrectly
treat "not in THIS row/chunk" as "not in Airtable at all" -- flagging
every record outside the current row/chunk as removed, on every single
row/chunk, not just the ones genuinely absent from the full run.

This module's own function is called ONCE, after a loader's full fetch +
upsert phase has completed, with the complete list of Airtable ids seen
in that run -- the only point at which "genuinely absent from this run's
fetch" can be evaluated correctly.
"""

# Table-specific staging table name (not just one shared #CurrentAirtableIds
# name across all three loaders) -- not required for correctness (temp
# tables are already scoped to the current session/connection, so there's
# no actual collision risk between loaders even with a shared name), but
# keeps things unambiguous if anyone inspects a session's own temp objects
# mid-run, e.g. while debugging a hung or slow loader.
_CREATE_CURRENT_IDS_STAGING_SQL_TEMPLATE = """
IF OBJECT_ID('tempdb..#{staging_table_name}') IS NOT NULL DROP TABLE #{staging_table_name};
CREATE TABLE #{staging_table_name} (Id VARCHAR(50) NOT NULL PRIMARY KEY);
"""

_INSERT_CURRENT_ID_SQL_TEMPLATE = "INSERT INTO #{staging_table_name} (Id) VALUES (?)"

# LEFT JOIN, not NOT EXISTS/NOT IN: a row with no match in the staging
# table (c.Id IS NULL) wasn't in this run's Airtable fetch at all -> 0
# (inactive). A row that DOES match -> 1 (active, whether it was already
# 1 or is being restored from a previous removal). The WHERE clause is a
# pure efficiency guard -- limits the UPDATE to rows whose value would
# actually change, so a normal run (where the vast majority of records
# are unchanged, still-present rows) doesn't rewrite every single row in
# the table just to set the same value it already had.
_FLAG_REMOVED_SQL_TEMPLATE = """
UPDATE t
SET t.Active = CASE WHEN c.Id IS NULL THEN 0 ELSE 1 END
FROM {table_name} t
LEFT JOIN #{staging_table_name} c ON c.Id = t.Id
WHERE ISNULL(t.Active, -1) <> CASE WHEN c.Id IS NULL THEN 0 ELSE 1 END
"""


def flag_records_removed_from_airtable(cursor, table_name: str, current_ids: list) -> int:
    """
    After a table has been fully upserted from a fresh, complete Airtable
    fetch, marks every EXISTING row whose own Id is NOT among current_ids
    as Active = 0 (no longer present in Airtable's own feed), and marks
    (or re-marks) any row whose Id IS present as Active = 1 -- covering
    the case where a previously-inactive record has since reappeared in
    Airtable (e.g. an accidental deletion, since restored, or a record
    that was temporarily filtered out of whatever Airtable view/base the
    loader's own fetch reads from).

    A NEW record from this same run's own upsert phase is never touched
    here: it was just inserted with Active's own column default (1), and
    its own Id is by definition present in current_ids (it came from
    that same Airtable fetch), so the LEFT JOIN above always finds a
    match for it and leaves it at 1.

    SAFETY GUARD: if current_ids is empty, this function does NOTHING
    and returns 0, rather than marking every existing row inactive. A
    genuinely empty Airtable table producing zero fetched records on a
    given run is far less likely than a transient fetch problem (a
    partial/failed API response, a misconfigured table/view, a timeout
    that returned early) -- treating "fetched nothing this run" as
    "Airtable now has nothing at all" would risk mass-marking every
    single existing record inactive from a single bad run, which is a
    far more damaging failure mode than simply skipping this one step
    for that run and trying again next time. A table that's genuinely,
    permanently emptied out in Airtable would need its own explicit,
    deliberate handling, not this function inferring that from an empty
    result.

    table_name: the real, trusted, project-internal table name (e.g.
    "Customers") -- interpolated directly into the SQL text rather than
    bound as a parameter, since SQL Server has no way to bind a table
    name as a query parameter. Never pass anything here that isn't a
    hardcoded literal already known to be one of this project's own real
    table names -- this offers no protection against a table_name that
    came from anything resembling user input.

    current_ids: every Id actually returned by this run's own Airtable
    fetch, regardless of whether that fetch's own upsert phase processed
    them row-by-row or in staging-table chunks -- this function only
    cares about the complete, final set, not how it was assembled.
    Bulk-inserted into a dedicated temp table via executemany(), rather
    than embedding potentially thousands of ids directly into a single
    SQL IN (...) clause -- both pyodbc and SQL Server itself handle that
    poorly at this project's own real scale (Poles alone can be close to
    14,000 records).

    Returns the number of rows whose Active value actually changed as a
    result of this call (newly marked inactive, plus any newly
    reactivated) -- useful for logging/observability (a suspiciously
    large number here could indicate a partial/failed Airtable fetch
    rather than genuine deletions), not a value any loader's own
    SP_Execution success/error counts otherwise need.
    """
    if not current_ids:
        return 0

    staging_table_name = f"CurrentAirtableIds_{table_name}"

    cursor.execute(
        _CREATE_CURRENT_IDS_STAGING_SQL_TEMPLATE.format(staging_table_name=staging_table_name)
    )

    # Explicitly OFF for this function's own bulk insert, regardless of
    # whatever the caller's own cursor happened to have it set to --
    # poles_loader.py's own load_poles() sets cursor.fast_executemany =
    # True near the top of that function, for ITS OWN, DIFFERENT bulk
    # insert into #PolesStaging, and that's a cursor-level setting, not
    # scoped to any one statement or table -- this same cursor object is
    # what gets passed in here. fast_executemany's own array-binding
    # path appears not to tolerate being pointed at a DIFFERENT temp
    # table than whatever it was last prepared against within the same
    # session -- this surfaced in production as "Result set index
    # cannot be less than 0 or greater than the number of result sets
    # (Parameter 'resultSetIndex')" during a real loadPoles run, once
    # this function started actually executing at Poles' own ~14,000-id
    # scale. Restored to whatever it was before, immediately after,
    # rather than left OFF -- this function has no business silently
    # changing a cursor-wide setting for whatever the CALLER does next
    # with that same cursor after this function returns.
    original_fast_executemany = getattr(cursor, "fast_executemany", False)
    cursor.fast_executemany = False
    try:
        cursor.executemany(
            _INSERT_CURRENT_ID_SQL_TEMPLATE.format(staging_table_name=staging_table_name),
            [(current_id,) for current_id in current_ids],
        )
    finally:
        cursor.fast_executemany = original_fast_executemany

    cursor.execute(
        _FLAG_REMOVED_SQL_TEMPLATE.format(
            table_name=table_name, staging_table_name=staging_table_name
        )
    )
    return cursor.rowcount
