"""
Read-only Poles query logic for getPoles.

Deliberately reuses shared/pole_vitals_api.py's per-pole SQL and
field-mapping directly, rather than duplicating it -- every pole here
carries the exact same fields as a pole entry inside getPoleVitals's
"poles" list, by explicit request, so a single shared implementation is
the only way these two endpoints can't silently drift apart from each
other over time. If pole_vitals_api.py's per-pole shape changes later
(a new field, a different window, etc.), getPoles' shape changes with
it automatically -- that's the explicit intent, not an accident of
implementation reuse.

The leading-underscore names imported below are pole_vitals_api.py's own
internal implementation details, not a published public API of that
module. Reached into directly here anyway, rather than duplicated --
this project treats "two copies of the same SQL slowly drifting apart"
as a bigger real risk than "one internal module reaching into another's
underscore-prefixed names" for code this tightly coupled and this
unlikely to be used by anything outside this codebase.
"""

from shared.api_utils import clamp_limit, json_safe
from shared.pole_vitals_api import (
    _POLE_DETAILS_SQL_TEMPLATE,
    _RECENT_HOURS_WINDOW,
    _RECENT_POLE_STATS_CTE,
    _STATUS_PERIOD_TYPE,
    _pole_row_to_dict,
)
from shared.sql_client import get_connection

# How high `limit` can go specifically in summary mode -- deliberately
# much higher than api_utils.MAX_LIMIT (1000), since summary mode exists
# specifically to make "give me every pole" (~14K and growing) practical
# to request in one call, unlike the default full-detail mode's cap.
_SUMMARY_MAX_LIMIT = 20000


def _clamp_summary_limit(limit) -> int:
    """Same shape as api_utils.clamp_limit(), but against
    _SUMMARY_MAX_LIMIT instead of api_utils.MAX_LIMIT -- summary mode's
    entire reason to exist is letting a caller request every pole in one
    shot, so reusing the default 1000-row cap here would defeat the
    purpose."""
    if not limit:
        return _SUMMARY_MAX_LIMIT
    return max(1, min(int(limit), _SUMMARY_MAX_LIMIT))


# A THIRD query, alongside pole_vitals_api.py's two -- reuses that
# module's exact same _RECENT_POLE_STATS_CTE (LightStatus/IsOnline/the
# three avg*Percentage fields, all from one aggregation pass over
# PoleVitals), but deliberately DROPS the OUTER APPLY into PoleTelemetry
# that _POLE_DETAILS_SQL_TEMPLATE has. That OUTER APPLY runs once per
# pole (a correlated TOP-1 lookup) -- each individual seek is cheap
# (PoleTelemetry's own clustered index is (LocationId, LastUpload),
# LocationId leading), but doing it ~14,000 times in one query execution
# is a real, structural cost that a "give me every pole" consumer (e.g.
# a map rendering all poles at once, which only needs location/status to
# place and color a pin) doesn't actually need to pay for --
# lastUpdate/batteryVoltage1/batteryVoltage2 are detail-view fields, not
# needed for that. A consumer that wants that detail for one specific
# pole (e.g. after a user clicks a pin) can still get it cheaply via
# ?poleId=X, which uses the full _POLE_DETAILS_SQL_TEMPLATE and pays that
# per-row cost for exactly one row, not thousands.
_POLE_SUMMARY_SQL_TEMPLATE = (
    """
;WITH """
    + _RECENT_POLE_STATS_CTE
    + """
SELECT
    proj.Id AS ProjectId,
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.LocationId AS LocationId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    rps.LightStatus AS LightStatus,
    rps.IsOnline AS IsOnline,
    rps.BatteryPercentage AS BatteryPercentage,
    rps.PanelPercentage AS PanelPercentage,
    rps.LightPercentage AS LightPercentage,
    c.Id AS CustomerId
FROM Poles p
JOIN Projects proj ON p.ProjectId = proj.Id
JOIN Customers c ON proj.CustomerId = c.Id
LEFT JOIN RecentPoleStats rps ON p.LocationId = rps.LocationId
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""
)


def _pole_row_to_dict_with_parents(row) -> dict:
    """
    pole_vitals_api._pole_row_to_dict() deliberately discards ProjectId
    (row[0], the first column) and CustomerId (row[-1], the last column)
    -- in that module both are used only for GROUPING poles under their
    already-known parent project/customer dicts, not included in the
    per-pole dict itself, since nesting under a project (itself under a
    customer) already implies both there. getPoles returns a flat list,
    not nested under either, so it needs both included explicitly --
    otherwise the unfiltered ("every pole") case would have no way to
    tell which project or customer a given pole belongs to.
    """
    pole_dict = _pole_row_to_dict(row)
    pole_dict["projectId"] = json_safe(row[0])
    pole_dict["customerId"] = json_safe(row[-1])
    return pole_dict


def _summary_row_to_dict(row) -> dict:
    """
    Converts one row from _POLE_SUMMARY_SQL_TEMPLATE. Same field set as
    _pole_row_to_dict_with_parents() above, minus lastUpdate/
    batteryVoltage1/batteryVoltage2 -- omitting exactly those three is
    the whole point of this lighter query (see
    _POLE_SUMMARY_SQL_TEMPLATE's own comment for why). A standalone
    function rather than a wrapper around pole_vitals_api._pole_row_to_dict()
    since the row shape here is genuinely different (three fewer
    columns), not just missing a couple of fields from the same shape.
    """
    (
        project_id,
        pole_id,
        pole_number,
        location_id,
        install_date,
        lat,
        long_,
        light_status,
        is_online,
        battery_percentage,
        panel_percentage,
        light_percentage,
        customer_id,
    ) = row
    return {
        "id": json_safe(pole_id),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(location_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
        "lightStatus": json_safe(light_status),
        "isOnline": json_safe(is_online),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
        "projectId": json_safe(project_id),
        "customerId": json_safe(customer_id),
    }


def get_poles(
    pole_id: str = None,
    project_id: str = None,
    customer_id: str = None,
    limit: int = None,
    summary: bool = False,
):
    """
    Returns Poles, each with the exact same fields a pole carries inside
    getPoleVitals's "poles" list (id, poleNumber, locationId, installDate,
    lat, long, lastUpdate, batteryVoltage1, batteryVoltage2, lightStatus,
    isOnline, avgBatteryPercentage, avgPanelPercentage,
    avgLightPercentage -- see pole_vitals_api.get_pole_vitals()'s own
    docstring for what each of these means and where it comes from),
    plus two additions beyond that literal field set: projectId and
    customerId, needed so a flat, unfiltered pole list has some way to
    trace each pole back to its project and customer.

    pole_id: if given, returns a SINGLE FLAT dict for that one pole, or
    None if not found -- matching customers_api.py/projects_api.py's
    single-entity-lookup contract, just returned as None here rather
    than an empty list, since the HTTP layer decides single-object-or-404
    shaping either way.
    project_id: if given WITHOUT pole_id, returns a LIST of every pole
    belonging to that project (empty list, not None, if the project has
    zero poles or doesn't exist -- matching projects_api.get_projects()'s
    customer_id filter, a collection filter, not a single-entity lookup).
    customer_id: if given WITHOUT pole_id, returns a LIST of every pole
    belonging to any of that customer's projects. Can be combined with
    project_id to also verify the project belongs to that customer, same
    as projects_api.get_projects().
    limit: max number of poles returned when none of the above ids are
    given -- the top-level entity in the unfiltered case. Ignored
    whenever pole_id, project_id, or customer_id is given. Defaults to
    DEFAULT_LIMIT, capped at MAX_LIMIT (see shared/api_utils.py) -- or at
    _SUMMARY_MAX_LIMIT instead, if summary=True.
    summary: if True, uses a lighter query (_POLE_SUMMARY_SQL_TEMPLATE)
    that skips each pole's most recent PoleTelemetry lookup -- lastUpdate,
    batteryVoltage1, and batteryVoltage2 are simply absent from each
    returned pole, everything else is the same. Built for a "give me
    every pole" consumer (e.g. a map rendering all ~14K poles at once)
    that only needs location and status to plot/color a pin, not
    per-pole telemetry detail -- that detail is still available cheaply
    for one specific pole via pole_id, which always uses the full query
    regardless of this flag. Also raises the unfiltered case's limit
    ceiling to _SUMMARY_MAX_LIMIT instead of api_utils.MAX_LIMIT, since
    summary mode's whole reason to exist is making "every pole in one
    call" practical.
    """
    if pole_id or project_id or customer_id:
        # Build up whichever of the three conditions were actually
        # given, combined with AND -- e.g. poleId+projectId together
        # means "this pole, AND verify it belongs to this project",
        # not "poleId wins, projectId is silently ignored".
        conditions = []
        params = [_STATUS_PERIOD_TYPE]
        if pole_id:
            conditions.append("p.Id = ?")
            params.append(pole_id)
        if project_id:
            conditions.append("proj.Id = ?")
            params.append(project_id)
        if customer_id:
            conditions.append("c.Id = ?")
            params.append(customer_id)
        where_clause = "WHERE " + " AND ".join(conditions)
        params = tuple(params)
    else:
        # limit applies to POLES, the top-level entity in this
        # unfiltered case -- a subquery on Poles' own Id, mirroring the
        # same "limit via a subquery, not a bare TOP() on the joined
        # query" pattern pole_vitals_api.py uses for ITS OWN unfiltered
        # case, even though there's no grouping concern to protect
        # against here (each pole is exactly one row in this query,
        # unlike Customer->Project). Kept consistent anyway, rather than
        # simplified, so this doesn't silently become wrong if the query
        # shape ever changes to produce more than one row per pole again.
        where_clause = "WHERE p.Id IN (SELECT TOP (?) Id FROM Poles ORDER BY PoleNumber)"
        row_limit = _clamp_summary_limit(limit) if summary else clamp_limit(limit)
        params = (_STATUS_PERIOD_TYPE, row_limit)

    sql_template = _POLE_SUMMARY_SQL_TEMPLATE if summary else _POLE_DETAILS_SQL_TEMPLATE
    row_to_dict = _summary_row_to_dict if summary else _pole_row_to_dict_with_parents

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            sql_template.format(where_clause=where_clause, hours_window=_RECENT_HOURS_WINDOW),
            *params,
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if pole_id:
        return row_to_dict(rows[0]) if rows else None

    return [row_to_dict(row) for row in rows]
