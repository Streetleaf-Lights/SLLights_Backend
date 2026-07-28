from shared.api_utils import clamp_limit, json_safe
from shared.sql_client import get_connection

# Columns returned to API consumers, mapped to camelCase JSON keys --
# same _COLUMN_TO_JSON_KEY pattern as customers_api.py/projects_api.py,
# adapted for the join below (table-qualified on the SQL side; rows are
# read back positionally via zip(), not by column name, so a name like
# "c.Name" colliding with "u.Name" in the raw result set doesn't matter).
#
# PasswordHash, ResetToken, and ResetTokenExpiresAt are deliberately
# excluded -- authentication-sensitive fields that have no place in a
# read-only API response, regardless of who's asking or how convenient
# it might seem. This is a hard line, not a default that happens to be
# unset -- these three columns should never be added to this list.
_COLUMN_TO_JSON_KEY = [
    ("u.Id", "id"),
    ("u.Name", "name"),
    ("u.Email", "email"),
    ("u.Role", "role"),
    ("u.Status", "status"),
    ("u.CustomerId", "customerId"),
    ("c.Name", "customerName"),
]

# LEFT JOIN, not INNER: CustomerId is nullable on Users (a "Streetleaf
# Admin" isn't scoped to one customer -- see sql/Users/Create tbl
# Users.sql's own notes), so a customer-less user must still appear here,
# just with customerName NULL, rather than being silently dropped by an
# INNER JOIN. Matches the exact join already sketched out in
# sql/Users/Select tbl Users joined with Customers.sql.
_FROM_CLAUSE = "FROM Users u LEFT JOIN Customers c ON u.CustomerId = c.Id"


def get_users(user_id: str = None, customer_id: str = None, limit: int = None) -> list:
    """
    Queries Users (joined with Customers for customerName) and returns a
    list of JSON-serializable dicts (camelCase keys -- see
    _COLUMN_TO_JSON_KEY).

    user_id: if given, filters to just that one Id (still returns a
    list -- 0 or 1 elements; the HTTP layer decides how to shape that
    into a single-object-or-404 response, this function's contract stays
    simple and uniform, same convention as customers_api.get_customers()/
    projects_api.get_projects()).
    customer_id: if given, filters to users belonging to that customer.
    Can be combined with user_id (verifies that user belongs to that
    customer -- built as an AND from the start, not an if/elif chain
    where one could silently override the other).
    limit: max rows returned when neither id is given. Defaults to
    DEFAULT_LIMIT, capped at MAX_LIMIT regardless of what's requested
    (see shared/api_utils.py). Ignored when either id is given.
    """
    columns_sql = ", ".join(col for col, _ in _COLUMN_TO_JSON_KEY)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if user_id or customer_id:
            conditions = []
            params = []
            if user_id:
                conditions.append("u.Id = ?")
                params.append(user_id)
            if customer_id:
                conditions.append("u.CustomerId = ?")
                params.append(customer_id)
            where_clause = "WHERE " + " AND ".join(conditions)
            cursor.execute(
                f"SELECT {columns_sql} {_FROM_CLAUSE} {where_clause}",
                *params,
            )
        else:
            cursor.execute(
                f"SELECT TOP (?) {columns_sql} {_FROM_CLAUSE} ORDER BY u.Name",
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
