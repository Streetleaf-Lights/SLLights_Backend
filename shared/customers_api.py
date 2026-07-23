from shared.sql_client import get_connection

MAX_LIMIT = 1000

# No-limit-specified means "everything, up to MAX_LIMIT" -- a business's
# customer roster is very unlikely to need pagination at all, so an
# arbitrarily low default (this used to be 100) just meant the endpoint
# silently truncated real results for anyone who didn't know to pass
# ?limit= explicitly. DEFAULT_LIMIT intentionally equals MAX_LIMIT now,
# rather than being some smaller number.
DEFAULT_LIMIT = MAX_LIMIT

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


def _json_safe(value):
    """
    pyodbc can return types (datetime, Decimal, etc.) that aren't
    natively JSON-serializable via json.dumps(). Converts anything that
    isn't already a safe type to a plain string; passes everything else
    through unchanged.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clamp_limit(limit) -> int:
    """Keeps limit within [1, MAX_LIMIT], defaulting to DEFAULT_LIMIT for
    None/invalid input -- a caller can't request an unbounded result set
    no matter what they pass."""
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def get_customers(customer_id: str = None, limit: int = None) -> list:
    """
    Queries Customers and returns a list of JSON-serializable dicts
    (camelCase keys -- see _COLUMN_TO_JSON_KEY).

    customer_id: if given, filters to just that one Id (still returns a
    list -- 0 or 1 elements; the HTTP layer decides how to shape that
    into a single-object-or-404 response, this function's contract stays
    simple and uniform).
    limit: max rows returned when customer_id isn't given. Defaults to
    DEFAULT_LIMIT, capped at MAX_LIMIT regardless of what's requested.
    Ignored when customer_id is given (a single-Id lookup is already
    bounded to at most one row).
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
                _clamp_limit(limit),
            )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    json_keys = [key for _, key in _COLUMN_TO_JSON_KEY]
    return [
        {key: _json_safe(value) for key, value in zip(json_keys, row)}
        for row in rows
    ]
