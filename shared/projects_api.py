from shared.api_utils import clamp_limit, json_safe
from shared.data_scope import UNRESTRICTED, DataScope, project_filter
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
    ("LeadsunProject", "leadsunProject"),
    ("Active", "active"),
    ("AirTableCreatedDateTime", "createdAt"),
    ("StickerNumbers", "stickerNumbers"),
]


def get_projects(
    project_id: str = None,
    customer_id: str = None,
    limit: int = None,
    active: bool = None,
    scope: DataScope = UNRESTRICTED,
) -> list:
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
    active: if given (True/False), filters to only projects whose
    Active column matches. Applies to both the customer_id-filtered list
    and the fully unfiltered list; ignored when project_id is given, same
    reasoning as limit -- a lookup by its own Id is a single specific
    resource, not a filtered list.

    scope: row-level restriction derived from the caller (see
    shared/data_scope.py), ANDed into the SQL with every other filter --
    so a project outside the caller's scope is never returned, including
    by a direct project_id lookup (which then finds nothing, and the HTTP
    layer answers 404).
    """
    columns_sql = ", ".join(col for col, _ in _COLUMN_TO_JSON_KEY)

    # Same query shapes as before this was scoped: project_id is a
    # single-resource lookup (no TOP, limit/active ignored), optionally
    # also checked against customer_id; otherwise customer_id/active
    # filters with TOP (limit), newest EffectiveDate first when filtered by
    # customer, by Name otherwise. Scope conditions are ANDed on and add no
    # parameters when the caller is unrestricted.
    conditions, params = [], []
    if project_id:
        conditions.append("Id = ?")
        params.append(project_id)
    if customer_id:
        conditions.append("CustomerId = ?")
        params.append(customer_id)
    if active is not None and not project_id:
        conditions.append("Active = ?")
        params.append(1 if active else 0)
    scope_conditions, scope_params = project_filter(scope)
    conditions += scope_conditions
    params += scope_params
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    conn = get_connection()
    cursor = conn.cursor()
    try:
        if project_id:
            cursor.execute(
                f"SELECT {columns_sql} FROM Projects{where}",
                *params,
            )
        else:
            order_by = "EffectiveDate DESC" if customer_id else "Name"
            cursor.execute(
                f"SELECT TOP (?) {columns_sql} FROM Projects{where} ORDER BY {order_by}",
                clamp_limit(limit),
                *params,
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
