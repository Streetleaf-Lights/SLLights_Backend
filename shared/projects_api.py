from shared.api_utils import clamp_limit, json_safe
from shared.sql_client import get_connection

# Columns returned to API consumers, mapped to camelCase JSON keys --
# same reasoning as customers_api.py: typical REST/JS convention, not the
# PascalCase SQL column names directly. SP_ExecId deliberately excluded --
# internal ETL batch-tracking metadata, not something a consuming website
# needs or should see.
_COLUMN_TO_JSON_KEY = [
    ("Id", "id"),
    ("Name", "name"),
    ("PoleNumbers", "poleNumbers"),
    ("PoleIds", "poleIds"),
    ("CustomerId", "customerId"),
    ("PolesUnderContract", "polesUnderContract"),
    ("EffectiveDate", "effectiveDate"),
    ("InstallDates", "installDates"),
    ("AirTableCreatedDateTime", "createdAt"),
]


def get_projects(project_id: str = None, customer_id: str = None, limit: int = None) -> list:
    """
    Queries Projects and returns a list of JSON-serializable dicts
    (camelCase keys -- see _COLUMN_TO_JSON_KEY). Same shape/contract as
    customers_api.get_customers() -- see that module for the reasoning
    behind it.

    project_id: if given, filters to just that one Id (still returns a
    list -- 0 or 1 elements; the HTTP layer decides how to shape that
    into a single-object-or-404 response, this function's contract stays
    simple and uniform). If customer_id is ALSO given, both conditions
    apply (WHERE Id = ? AND CustomerId = ?) -- useful for verifying a
    project actually belongs to a given customer, not just looking it up
    by Id alone.
    customer_id: if given without project_id, filters to all projects for
    that customer -- a genuine list query (0, many, or all of that
    customer's projects), ordered by EffectiveDate descending (newest
    first) and still subject to limit. NOT single-object-or-404 semantics
    like project_id. This is a deliberately different sort from the
    unfiltered case below (by Name) -- only the customer_id-filtered list
    was asked to sort by EffectiveDate. SQL Server's default NULL
    handling sorts NULLs last for DESC, so a project with no
    EffectiveDate set yet appears at the bottom rather than mixed in
    ahead of ones with a known date.
    limit: max rows returned when project_id isn't given. Defaults to
    DEFAULT_LIMIT, capped at MAX_LIMIT regardless of what's requested
    (see shared/api_utils.py). Ignored when project_id is given (a
    single-Id lookup is already bounded to at most one row).
    """
    columns_sql = ", ".join(col for col, _ in _COLUMN_TO_JSON_KEY)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if project_id and customer_id:
            cursor.execute(
                f"SELECT {columns_sql} FROM Projects WHERE Id = ? AND CustomerId = ?",
                project_id,
                customer_id,
            )
        elif project_id:
            cursor.execute(
                f"SELECT {columns_sql} FROM Projects WHERE Id = ?",
                project_id,
            )
        elif customer_id:
            cursor.execute(
                f"SELECT TOP (?) {columns_sql} FROM Projects WHERE CustomerId = ? ORDER BY EffectiveDate DESC",
                clamp_limit(limit),
                customer_id,
            )
        else:
            cursor.execute(
                f"SELECT TOP (?) {columns_sql} FROM Projects ORDER BY Name",
                clamp_limit(limit),
            )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    json_keys = [key for _, key in _COLUMN_TO_JSON_KEY]
    return [
        {key: json_safe(value) for key, value in zip(json_keys, row)}
        for row in rows
    ]
