from shared.api_utils import clamp_limit, json_safe
from shared.sql_client import get_connection

# Columns returned to API consumers, mapped to camelCase JSON keys
# (typical REST/JS convention, not the PascalCase SQL column names
# directly). SP_ExecId is deliberately excluded -- internal ETL
# batch-tracking metadata, not something a consuming website needs or
# should see.
_COLUMN_TO_JSON_KEY = [
    ("Id", "id"),
    ("Name", "name"),
    ("ProjectNames", "projectNames"),
    ("ProjectIds", "projectIds"),
    ("Address", "address"),
    ("City", "city"),
    ("State", "state"),
    ("Zip", "zip"),
    ("Phone", "phone"),
    ("AirTableCreatedDateTime", "createdAt"),
]


def get_customers(customer_id: str = None, limit: int = None) -> list:
    """
    Queries Customers and returns a list of JSON-serializable dicts
    (camelCase keys -- see _COLUMN_TO_JSON_KEY).

    customer_id: if given, filters to just that one Id (still returns a
    list -- 0 or 1 elements; the HTTP layer decides how to shape that
    into a single-object-or-404 response, this function's contract stays
    simple and uniform).
    limit: max rows returned when customer_id isn't given. Defaults to
    DEFAULT_LIMIT, capped at MAX_LIMIT regardless of what's requested
    (see shared/api_utils.py). Ignored when customer_id is given (a
    single-Id lookup is already bounded to at most one row).
    """
    columns_sql = ", ".join(col for col, _ in _COLUMN_TO_JSON_KEY)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if customer_id:
            cursor.execute(
                f"SELECT {columns_sql} FROM Customers WHERE Id = ?",
                customer_id,
            )
        else:
            cursor.execute(
                f"SELECT TOP (?) {columns_sql} FROM Customers ORDER BY Name",
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
