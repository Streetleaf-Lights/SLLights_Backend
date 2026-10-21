"""
Read-only Poles query logic for getPoles.

Deliberately reuses shared/pole_vitals_api.py's per-pole SQL and
field-mapping directly, rather than duplicating it -- every pole here
carries the exact same fields as a pole entry inside getPoleVitals's
"poles" list, by explicit request, so a single shared implementation is
the only way these two endpoints can't silently drift apart from each
other over time. If pole_vitals_api.py's per-pole shape changes later
(a new field, a different source, etc.), getPoles' shape changes with
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

from shared.api_utils import (
    clamp_limit,
    compute_pole_local_sunset,
    compute_reporting_staleness_label,
    json_safe,
)
from shared.pole_vitals_api import (
    _POLE_DETAILS_SQL_TEMPLATE,
    _ROLLUP_PERIOD_TYPE,
    _compute_pole_vitals_status_fields,
    _pole_row_to_dict,
)
from shared.data_scope import UNRESTRICTED, DataScope, project_filter
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


# A SECOND query, alongside pole_vitals_api.py's own -- a direct LEFT
# JOIN against PoleVitals (WHERE PeriodType = ?), same as
# _POLE_DETAILS_SQL_TEMPLATE there. Its own OUTER APPLY was ORIGINALLY
# much leaner than that one -- just LastUpload, deliberately skipping
# batteryVoltage1/2, lampPower1/2, batteryElecCurrent1/2,
# solarBoardVoltage/solarBoardElecCurrent entirely, for the same
# performance reasoning still explained below. That's since widened,
# per explicit request, to also fetch LampPower1/2,
# BatteryElecCurrent1/2, SolarBoardVoltage/SolarBoardElecCurrent, and
# IsDaylightForPanelFault -- the raw inputs
# api_utils.compute_pole_status_labels() needs to compute
# lightStatusText/panelStatusText/panelIdleReason/batteryStatusText
# for summary mode too (see _summary_row_to_dict()'s own comment for
# why electricCurrentAverage specifically is NOT one of the four
# exposed here, even though its own inputs are now being fetched
# anyway). batteryVoltage1/batteryVoltage2 remain the only genuine
# holdouts -- still no calculated field of any kind depends on them,
# so there's still no reason to pay for fetching them in this mode.
#
# That OUTER APPLY still runs once per pole (a correlated TOP-1
# lookup) -- each individual seek is cheap (PoleTelemetry's own
# clustered index is (PoleId, LastUpload), PoleId leading),
# and doing it ~14,000 times in one query execution is a real,
# structural cost regardless of how many columns each seek pulls
# back -- but a wider row is still more expensive to materialize and
# transfer than the original single-column one was, an accepted
# tradeoff for having these four fields available at summary scale
# too, not a free change. lastUpdate earns its own place here
# regardless, same reasoning as before: the web frontend needs it to
# distinguish "Disconnected" (was online, has gone quiet recently)
# from "Unknown" (no recent-enough reading to say either way) -- a
# distinction that can't be made from IsOnline alone.
#
# ptz.Latitude/Longitude/IanaTimeZone (from the PoleTimeZones join
# already present here for WindowsTimeZone's own sake) are ALSO now
# selected -- the inputs api_utils.compute_pole_local_sunset() needs
# for sunsetTime. Unlike the OUTER APPLY widening above, this one adds
# no new join and no new per-pole cost of any real significance: these
# three columns come from a LEFT JOIN this query already performs.
_POLE_SUMMARY_SQL_TEMPLATE = """
SELECT
    proj.Id AS ProjectId,
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.VendorPoleId AS VendorPoleId,
    p.ControllerId AS ControllerId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    p.Active AS Active,
    latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
    latest_pt.LampPower1 AS LampPower1,
    latest_pt.LampPower2 AS LampPower2,
    latest_pt.BatteryElecCurrent1 AS BatteryElecCurrent1,
    latest_pt.BatteryElecCurrent2 AS BatteryElecCurrent2,
    latest_pt.SolarBoardVoltage AS SolarBoardVoltage,
    latest_pt.SolarBoardElecCurrent AS SolarBoardElecCurrent,
    latest_pt.IsDaylightForPanelFault AS IsDaylightForPanelFault,
    ptz.Latitude AS TimeZoneLatitude,
    ptz.Longitude AS TimeZoneLongitude,
    ptz.IanaTimeZone AS IanaTimeZone,
    rps.IsOnline AS IsOnline,
    rps.IsLedFault AS IsLedFault,
    rps.IsBatteryFault AS IsBatteryFault,
    rps.IsPanelFault AS IsPanelFault,
    -- Same as pole_vitals_api.py's own _POLE_DETAILS_SQL_TEMPLATE:
    -- reads directly from PoleIssues, decoupled from the Last48Hours
    -- join and therefore from telemetry recency entirely -- always TRUE
    -- or FALSE, never NULL, regardless of whether rps itself has a
    -- matching row.
    CASE WHEN EXISTS (
        SELECT 1 FROM PoleIssues poi WHERE poi.PoleId = p.Id AND poi.Status = 'Open'
    ) THEN CAST(1 AS BIT) ELSE CAST(0 AS BIT) END AS IsOpenIssueFault,
    rps.IsPoleFault AS IsPoleFault,
    rps.AvgBatteryPercentage AS BatteryPercentage,
    rps.AvgPanelPercentage AS PanelPercentage,
    rps.AvgLightPercentage AS LightPercentage,
    c.Id AS CustomerId
FROM Poles p
JOIN Projects proj ON p.ProjectId = proj.Id
JOIN Customers c ON proj.CustomerId = c.Id
LEFT JOIN PoleVitals rps ON COALESCE(p.VendorPoleId, p.ProvisionedPoleId) = rps.PoleId AND rps.PeriodType = ?
LEFT JOIN PoleTimeZones ptz ON
    (p.VendorPoleId IS NOT NULL AND p.VendorPoleId = ptz.VendorPoleId)
    OR (p.VendorPoleId IS NULL AND p.ProvisionedPoleId IS NOT NULL AND p.ProvisionedPoleId = ptz.ProvisionedPoleId)
OUTER APPLY (
    -- TOP 1 ... ORDER BY LastUpload DESC. OUTER (not CROSS): a pole with
    -- no PoleId, or zero matching PoleTelemetry rows at all, must
    -- still appear in results (with every column below NULL), not
    -- disappear from the summary entirely.
    SELECT TOP 1
        pt.LastUpload, pt.LampPower1, pt.LampPower2,
        pt.BatteryElecCurrent1, pt.BatteryElecCurrent2,
        pt.SolarBoardVoltage, pt.SolarBoardElecCurrent, pt.IsDaylightForPanelFault
    FROM PoleTelemetry pt
    WHERE pt.PoleId = COALESCE(p.VendorPoleId, p.ProvisionedPoleId)
    ORDER BY pt.LastUpload DESC
) AS latest_pt
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""


def _pole_row_to_dict_with_parents(row) -> dict:
    """
    pole_vitals_api._pole_row_to_dict() deliberately discards ProjectId
    (row[0], the first column) and CustomerId (row[-2], second-to-last --
    IssuesRaw is the last column)
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
    pole_dict["customerId"] = json_safe(row[-2])  # CustomerId; row[-1] is IssuesRaw
    return pole_dict


def _summary_row_to_dict(row) -> dict:
    """
    Converts one row from _POLE_SUMMARY_SQL_TEMPLATE. Same field set as
    _pole_row_to_dict_with_parents() above, minus batteryVoltage1/
    batteryVoltage2 (see _POLE_SUMMARY_SQL_TEMPLATE's own comment for
    why those two specifically remain excluded) and minus
    electricCurrentAverage specifically (see below for why that one,
    alone among the five calculated fields, is deliberately dropped
    here even though its own inputs are now being fetched anyway).
    lastUpdate is INCLUDED here, via that same template's own OUTER
    APPLY, since the web frontend needs it to distinguish
    "Disconnected" from "Unknown" -- a distinction IsOnline alone can't
    make. A standalone function rather than a wrapper around
    pole_vitals_api._pole_row_to_dict() since the row shape here is
    genuinely different (missing batteryVoltage1/2), not just a subset
    of the exact same shape.

    lightStatusText/panelStatusText/panelIdleReason/batteryStatusText/
    connectedText/overallStatusText: computed via the exact same
    shared pole_vitals_api._compute_pole_vitals_status_fields() helper
    pole_vitals_api.py's own _pole_row_to_dict() uses -- reused directly
    here (rather than reimplementing the same staleness-override logic a
    second time) for the same "one shared implementation, no silent
    drift" reasoning this whole module's own header comment already
    applies to the SQL templates. This means a silent pole (lastUpdate
    null or >48h old) gets the exact same treatment here as it does in
    getPoleVitals: isLedFault/isBatteryFault/isPanelFault/isPoleFault/
    lampPower1/2/batteryElecCurrent1/2/solarBoardVoltage/
    solarBoardElecCurrent all null, lightStatusText/panelStatusText/
    batteryStatusText/overallStatusText showing "Not Reporting"/"Not
    Reporting 48H" as appropriate, and connectedText reflecting
    isOnline/lastUpdate per its own independent rules.

    electricCurrentAverage is popped back out before merging into this
    dict below, rather than that shared helper ever being asked to
    return fewer keys for this one caller -- keeping its own contract
    simple and total, with "which fields to actually expose" left
    entirely up to each caller. (isOpenIssueFault is untouched by any of
    this -- see _POLE_SUMMARY_SQL_TEMPLATE's own comment: it's read
    directly from PoleIssues, independent of lastUpdate/telemetry
    staleness entirely, same as pole_vitals_api.py's own per-pole detail
    query.)
    """
    (
        project_id,
        pole_id,
        pole_number,
        vendor_pole_id,
        controller_id,
        install_date,
        lat,
        long_,
        active,
        last_update,
        lamp_power_1,
        lamp_power_2,
        battery_elec_current_1,
        battery_elec_current_2,
        solar_board_voltage,
        solar_board_elec_current,
        is_daylight_for_panel_fault,
        timezone_latitude,
        timezone_longitude,
        iana_timezone,
        is_online,
        is_led_fault,
        is_battery_fault,
        is_panel_fault,
        is_open_issue_fault,
        is_pole_fault,
        battery_percentage,
        panel_percentage,
        light_percentage,
        customer_id,
    ) = row

    # Same staleness override as pole_vitals_api._pole_row_to_dict()
    # applies to its own four fault fields and raw sensor fields --
    # replicated here (rather than assuming _compute_pole_vitals_
    # status_fields() below does it) since that helper only computes
    # the LABEL fields (lightStatusText/panelStatusText/
    # batteryStatusText/connectedText/overallStatusText/
    # electricCurrentAverage/panelIdleReason) -- isLedFault/
    # isBatteryFault/isPanelFault/isPoleFault and the raw
    # lampPower/batteryElecCurrent/solarBoard readings are this
    # function's own separate local variables, nulled directly here.
    # isOpenIssueFault is untouched, same reasoning as
    # pole_vitals_api.py's own version: sourced independently from
    # PoleIssues (see _POLE_SUMMARY_SQL_TEMPLATE's own comment), not
    # from PoleTelemetry, so telemetry staleness says nothing about it.
    if compute_reporting_staleness_label(last_update) is not None:
        is_led_fault = None
        is_battery_fault = None
        is_panel_fault = None
        is_pole_fault = None
        lamp_power_1 = None
        lamp_power_2 = None
        battery_elec_current_1 = None
        battery_elec_current_2 = None
        solar_board_voltage = None
        solar_board_elec_current = None

    status_fields = _compute_pole_vitals_status_fields(
        last_update=last_update,
        is_online=is_online,
        is_pole_fault=is_pole_fault,
        lamp_power_1=lamp_power_1,
        lamp_power_2=lamp_power_2,
        battery_elec_current_1=battery_elec_current_1,
        battery_elec_current_2=battery_elec_current_2,
        solar_board_voltage=solar_board_voltage,
        solar_board_elec_current=solar_board_elec_current,
        is_daylight_for_panel_fault=is_daylight_for_panel_fault,
    )
    status_fields.pop("electricCurrentAverage", None)

    sunset_time = json_safe(
        compute_pole_local_sunset(timezone_latitude, timezone_longitude, iana_timezone)
    )

    return {
        "id": json_safe(pole_id),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(vendor_pole_id),
        "controllerId": json_safe(controller_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
        "active": json_safe(active),
        "lastUpdate": json_safe(last_update),
        "isOnline": json_safe(is_online),
        "isLedFault": json_safe(is_led_fault),
        "isBatteryFault": json_safe(is_battery_fault),
        "isPanelFault": json_safe(is_panel_fault),
        "isOpenIssueFault": json_safe(is_open_issue_fault),
        "isPoleFault": json_safe(is_pole_fault),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
        "sunsetTime": sunset_time,
        "projectId": json_safe(project_id),
        "customerId": json_safe(customer_id),
        **status_fields,
    }


# --------------------------------------------------------------------------
# Paginated listing -- get_poles_page()
#
# get_poles() above builds a full row (latest-telemetry lookup, timezone
# join, per-pole sunset calculation in Python) for EVERY pole it returns --
# which is the cost when a caller only wants a screenful. get_poles_page()
# instead (1) counts the poles that match the filters with a cheap query
# that touches none of that, and (2) picks just the requested page's pole
# Ids in a subquery (OFFSET/FETCH) and only then runs the same expensive
# per-pole query -- for at most page_size rows. The rows it returns come
# out of the same SQL templates and row mappers as get_poles(), so an item
# here is identical to the same pole's entry in the unpaginated list --
# though in a different ORDER (see _PAGED_ORDER_BY).
# --------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000

# EXACTLY the rollup's own "fault" definition (pole_vitals_api.py's
# _FETCH_SQL_TEMPLATE, ProjectAgg.TotalFaults), applied to the same
# Last48Hours PoleVitals row -- so faults=1's `total` reconciles with the
# TotalFaults a dashboard already shows for the same customer/project.
_FAULT_PREDICATE = "(sv.IsOnline = 1 OR sv.IsOpenIssueFault = 1) AND sv.IsPoleFault = 1"

# Paginated results are ordered by PoleNumber across the whole list. The
# page's pole Ids are chosen with this order (inside the OFFSET/FETCH
# subquery) AND the final per-pole query is re-ordered to match, so the
# rows within a page come back in the same order that decided what is ON
# the page. The id is the tie-breaker that makes the order total.
_PAGE_SUBQUERY_ORDER_BY = "ORDER BY sp.PoleNumber, sp.Id"
_PAGED_ORDER_BY = "ORDER BY p.PoleNumber, p.Id"
# What BOTH shared templates (_POLE_SUMMARY_SQL_TEMPLATE here and
# pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE) end with. get_poles() and
# getPoleVitals keep that project-first order; only the paginated path
# swaps it, and only on its own copy of the formatted SQL.
_TEMPLATE_ORDER_BY = "ORDER BY proj.Id, p.PoleNumber"


def _like_contains(text: str) -> str:
    """
    Turns user-typed search text into a LIKE pattern meaning "contains this
    text, literally": %, _ and [ (LIKE's own wildcards) are escaped with a
    backslash, as is the backslash itself, so searching "10_5" matches
    "10_5" only -- not "1085". Pair with  LIKE ? ESCAPE '\\'.
    """
    escaped = (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")
    )
    return f"%{escaped}%"


def _page_pole_filter(project_id, customer_id, active, faults, scope, pole_number=None):
    """
    The FROM + WHERE text and parameters that decide WHICH poles are in a
    paginated result (aliases: sp = Poles, spr = Projects, sc = Customers,
    sv = PoleVitals). Used verbatim by BOTH the count query and the page
    query, so `total` can never disagree with the items it pages through.

    Joins Projects and Customers exactly like the main query does (inner),
    so a pole the main query would drop (no project, or a project whose
    customer doesn't exist) is not counted or paged either -- otherwise a
    page could come back short of page_size while `total` still counted it.

    Returns (from_sql, where_sql, params) with params in the order their
    `?` placeholders appear in from_sql + where_sql.
    """
    from_sql = (
        "FROM Poles sp "
        "JOIN Projects spr ON sp.ProjectId = spr.Id "
        "JOIN Customers sc ON spr.CustomerId = sc.Id"
    )
    params = []
    if faults:
        from_sql += (
            " JOIN PoleVitals sv ON COALESCE(sp.VendorPoleId, sp.ProvisionedPoleId) = sv.PoleId"
            " AND sv.PeriodType = ?"
        )
        params.append(_ROLLUP_PERIOD_TYPE)

    conditions = []
    if project_id:
        conditions.append("spr.Id = ?")
        params.append(project_id)
    if customer_id:
        conditions.append("sc.Id = ?")
        params.append(customer_id)
    if pole_number:
        # Case sensitivity follows the database collation (Azure SQL's
        # default is case-insensitive). No index can serve a leading-
        # wildcard match, but this runs over ~14K narrow Poles rows in the
        # Id-only subquery, never over the expensive per-pole joins.
        conditions.append("sp.PoleNumber LIKE ? ESCAPE '\\'")
        params.append(_like_contains(pole_number))
    if active is not None:
        conditions.append("sp.Active = ?")
        params.append(1 if active else 0)
    scope_conditions, scope_params = project_filter(scope, alias="spr")
    conditions += scope_conditions
    params += scope_params
    if faults:
        conditions.append(_FAULT_PREDICATE)

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return from_sql, where_sql, params


def get_poles_page(
    page: int = 1,
    page_size: int = None,
    project_id: str = None,
    customer_id: str = None,
    pole_number: str = None,
    summary: bool = False,
    active: bool = None,
    faults: bool = False,
    scope: DataScope = UNRESTRICTED,
) -> dict:
    """
    One page of poles: {"total", "page", "pageSize", "items"}.

    total is the number of poles matching the filters across ALL pages.
    page is 1-based; page_size defaults to DEFAULT_PAGE_SIZE and is clamped
    to [1, MAX_PAGE_SIZE] (the clamped value is what comes back as
    "pageSize"). A page past the end returns an empty items list with the
    real total. Poles are ordered by PoleNumber across the whole result
    (NOT grouped by project, unlike get_poles()), with the pole Id as a
    tie-breaker so paging never repeats or skips a pole. PoleNumber sorts
    as text under the database collation (so "...-1000" precedes "...-999").

    pole_number: a search -- only poles whose PoleNumber CONTAINS this text
    (see _like_contains for how wildcards in the text are handled).
    faults: only poles the rollup counts as faults (see _FAULT_PREDICATE).
    summary: use the lighter per-pole query, as get_poles(summary=True).
    scope: row-level restriction from the caller (shared/data_scope.py),
    applied to the count and the page alike.
    """
    page = max(1, int(page))
    page_size = DEFAULT_PAGE_SIZE if page_size is None else max(1, min(int(page_size), MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    from_sql, where_sql, filter_params = _page_pole_filter(
        project_id, customer_id, active, faults, scope, pole_number
    )
    sql_template = _POLE_SUMMARY_SQL_TEMPLATE if summary else _POLE_DETAILS_SQL_TEMPLATE
    row_to_dict = _summary_row_to_dict if summary else _pole_row_to_dict_with_parents

    # ONE string for "which poles", used by both queries -- identical by
    # construction, not by two f-strings happening to agree.
    which_poles = f"{from_sql} {where_sql}".rstrip()

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) {which_poles}", *filter_params)
        total = cursor.fetchone()[0]

        rows = []
        # Nothing to fetch when the page starts past the last match -- skip
        # the expensive per-pole query entirely.
        if offset < total:
            where_clause = (
                f"WHERE p.Id IN (SELECT sp.Id {which_poles} "
                f"{_PAGE_SUBQUERY_ORDER_BY} "
                "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY)"
            )
            page_sql = sql_template.format(where_clause=where_clause).rstrip()
            # Swap the template's project-first order for PoleNumber order.
            # Fail LOUDLY if a template ever stops ending this way -- a
            # silent mismatch would put a page's rows in a different order
            # than the one that chose them.
            if not page_sql.endswith(_TEMPLATE_ORDER_BY):
                raise RuntimeError(
                    "get_poles_page expected the SQL template to end with "
                    f"{_TEMPLATE_ORDER_BY!r}; update _TEMPLATE_ORDER_BY"
                )
            page_sql = page_sql[: -len(_TEMPLATE_ORDER_BY)] + _PAGED_ORDER_BY
            cursor.execute(
                page_sql,
                _ROLLUP_PERIOD_TYPE,  # the template's own PoleVitals join comes first
                *filter_params,
                offset,
                page_size,
            )
            rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    return {
        "total": total,
        "page": page,
        "pageSize": page_size,
        "items": [row_to_dict(row) for row in rows],
    }


def get_poles(
    pole_id: str = None,
    project_id: str = None,
    customer_id: str = None,
    limit: int = None,
    summary: bool = False,
    active: bool = None,
    scope: DataScope = UNRESTRICTED,
):
    """
    scope: row-level restriction derived from the caller (see
    shared/data_scope.py). Applied in the SQL, ANDed with every other
    filter, and -- in the unfiltered list case -- inside the TOP (limit)
    subquery, so the limit counts only poles the caller may see. A pole
    outside the caller's scope is never returned, even by a direct
    pole_id lookup (which then returns None, i.e. 404).

    Returns Poles, each with the exact same fields a pole carries inside
    getPoleVitals's "poles" list (id, poleNumber, locationId, installDate,
    lat, long, lastUpdate, batteryVoltage1, batteryVoltage2, isOnline,
    isLedFault, isBatteryFault, isPanelFault, isOpenIssueFault,
    isPoleFault, avgBatteryPercentage, avgPanelPercentage,
    avgLightPercentage, lightStatusText, panelStatusText,
    panelIdleReason, batteryStatusText, electricCurrentAverage,
    sunsetTime -- see
    pole_vitals_api.get_pole_vitals()'s own docstring for what each of
    these means and where it comes from), plus two additions beyond
    that literal field set: projectId and customerId, needed so a
    flat, unfiltered pole list has some way to trace each pole back to
    its project and customer.

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
    that skips the two heaviest per-pole telemetry fields --
    batteryVoltage1 and batteryVoltage2 are simply absent from each
    returned pole, and so is electricCurrentAverage specifically (one
    of the five calculated fields above; the other four --
    lightStatusText/panelStatusText/panelIdleReason/
    batteryStatusText -- ARE included here too, per explicit request,
    even though electricCurrentAverage's own underlying inputs are now
    fetched regardless) -- but still includes lastUpdate, via that same
    template's own PoleTelemetry lookup (see
    _POLE_SUMMARY_SQL_TEMPLATE's own comment for why lastUpdate earns
    that cost while the two voltage fields don't: the web frontend needs
    lastUpdate to distinguish "Disconnected" from "Unknown", a
    distinction IsOnline alone can't make). sunsetTime IS also included
    here -- unlike the status labels above, it never needed the
    cost/benefit tradeoff those did: it's a pure Python calculation over
    PoleTimeZones' own Latitude/Longitude/IanaTimeZone columns, already
    joined into this query for the WindowsTimeZone-based lastUpdate
    conversion, not a new expensive PoleTelemetry column to fetch.
    Built for a "give me every pole" consumer (e.g. a map rendering all
    ~14K poles at once) that needs location, status, staleness, and now
    these four status labels plus sunsetTime too, to plot/color a pin,
    not full per-pole telemetry detail -- that fuller detail (including
    electricCurrentAverage) is still available cheaply for one specific
    pole via pole_id, which always uses the full query regardless of
    this flag. Also raises the unfiltered case's limit ceiling to
    _SUMMARY_MAX_LIMIT instead of api_utils.MAX_LIMIT, since summary
    mode's whole reason to exist is
    making "every pole in one call" practical.
    active: if given (True/False), filters to only poles whose Active
    column matches. Applies to the project_id-filtered list, the
    customer_id-filtered list, and the fully unfiltered list; ignored
    when pole_id is given, same reasoning as limit -- a lookup by its
    own Id is a single specific resource, not a filtered list.
    """
    if pole_id or project_id or customer_id:
        # Build up whichever of the three conditions were actually
        # given, combined with AND -- e.g. poleId+projectId together
        # means "this pole, AND verify it belongs to this project",
        # not "poleId wins, projectId is silently ignored".
        conditions = []
        # Both templates now have a SINGLE period-type placeholder --
        # rps, bound to _ROLLUP_PERIOD_TYPE, used for isOnline AND every
        # other detail field alike (a separate LastKnown48Hours-driven
        # rps_online join used to exist here; removed entirely by
        # explicit request/correction -- a silent pole now gets null for
        # every one of these fields uniformly, not a last-known
        # fallback). Both templates share this exact same
        # one-parameter prefix, so this single params list serves either
        # one.
        params = [_ROLLUP_PERIOD_TYPE]
        if pole_id:
            conditions.append("p.Id = ?")
            params.append(pole_id)
        if project_id:
            conditions.append("proj.Id = ?")
            params.append(project_id)
        if customer_id:
            conditions.append("c.Id = ?")
            params.append(customer_id)
        # Ignored when pole_id is given -- a lookup by its own Id is a
        # single specific resource, not a filtered list; see docstring.
        if active is not None and not pole_id:
            conditions.append("p.Active = ?")
            params.append(1 if active else 0)
        # Scope: the query already joins Projects (proj) and Customers (c).
        if scope.customer_id is not None:
            conditions.append("c.Id = ?")
            params.append(scope.customer_id)
        if scope.scheduled_projects_only:
            conditions.append("proj.StickerNumbers IS NOT NULL")
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
        row_limit = _clamp_summary_limit(limit) if summary else clamp_limit(limit)
        # Scope goes INSIDE the TOP subquery (so the limit counts only
        # in-scope poles). The subquery joins Projects as `spr`; a
        # customer-scoped caller is checked on spr.CustomerId.
        sub_conditions, sub_params = project_filter(scope, alias="spr")
        if sub_conditions:
            where_clause = (
                "WHERE p.Id IN (SELECT TOP (?) sp.Id FROM Poles sp "
                "JOIN Projects spr ON sp.ProjectId = spr.Id "
                "WHERE " + " AND ".join(sub_conditions) + " ORDER BY sp.PoleNumber)"
            )
            params = [_ROLLUP_PERIOD_TYPE, row_limit, *sub_params]
        else:
            where_clause = "WHERE p.Id IN (SELECT TOP (?) Id FROM Poles ORDER BY PoleNumber)"
            params = [_ROLLUP_PERIOD_TYPE, row_limit]
        if active is not None:
            where_clause += " AND p.Active = ?"
            params.append(1 if active else 0)
        params = tuple(params)

    sql_template = _POLE_SUMMARY_SQL_TEMPLATE if summary else _POLE_DETAILS_SQL_TEMPLATE
    row_to_dict = _summary_row_to_dict if summary else _pole_row_to_dict_with_parents

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            sql_template.format(where_clause=where_clause),
            *params,
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if pole_id:
        return row_to_dict(rows[0]) if rows else None

    return [row_to_dict(row) for row in rows]
