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

from shared.api_utils import clamp_limit, compute_pole_status_labels, json_safe
from shared.pole_vitals_api import (
    _POLE_DETAIL_PERIOD_TYPE,
    _POLE_DETAILS_SQL_TEMPLATE,
    _ROLLUP_PERIOD_TYPE,
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
# lightStatusLabel/panelStatusLabel/panelIdleReason/batteryStatusLabel
# for summary mode too (see _summary_row_to_dict()'s own comment for
# why electricCurrentAverage specifically is NOT one of the four
# exposed here, even though its own inputs are now being fetched
# anyway). batteryVoltage1/batteryVoltage2 remain the only genuine
# holdouts -- still no calculated field of any kind depends on them,
# so there's still no reason to pay for fetching them in this mode.
#
# That OUTER APPLY still runs once per pole (a correlated TOP-1
# lookup) -- each individual seek is cheap (PoleTelemetry's own
# clustered index is (LocationId, LastUpload), LocationId leading),
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
_POLE_SUMMARY_SQL_TEMPLATE = """
SELECT
    proj.Id AS ProjectId,
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.LocationId AS LocationId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
    latest_pt.LampPower1 AS LampPower1,
    latest_pt.LampPower2 AS LampPower2,
    latest_pt.BatteryElecCurrent1 AS BatteryElecCurrent1,
    latest_pt.BatteryElecCurrent2 AS BatteryElecCurrent2,
    latest_pt.SolarBoardVoltage AS SolarBoardVoltage,
    latest_pt.SolarBoardElecCurrent AS SolarBoardElecCurrent,
    latest_pt.IsDaylightForPanelFault AS IsDaylightForPanelFault,
    rps_online.IsOnline AS IsOnline,
    rps.IsLedFault AS IsLedFault,
    rps.IsBatteryFault AS IsBatteryFault,
    rps.IsPanelFault AS IsPanelFault,
    rps.IsOpenIssueFault AS IsOpenIssueFault,
    rps.IsPoleFault AS IsPoleFault,
    rps.AvgBatteryPercentage AS BatteryPercentage,
    rps.AvgPanelPercentage AS PanelPercentage,
    rps.AvgLightPercentage AS LightPercentage,
    c.Id AS CustomerId
FROM Poles p
JOIN Projects proj ON p.ProjectId = proj.Id
JOIN Customers c ON proj.CustomerId = c.Id
LEFT JOIN PoleVitals rps ON p.LocationId = rps.LocationId AND rps.PeriodType = ?
-- Second join, same reasoning as pole_vitals_api.py's own
-- _POLE_DETAILS_SQL_TEMPLATE: IsOnline specifically reverts to
-- Last48Hours (_ROLLUP_PERIOD_TYPE) while every other field above stays
-- on LastKnown48Hours (_POLE_DETAIL_PERIOD_TYPE) -- a silent pole's
-- LastKnown48Hours.IsOnline would misleadingly reflect its own LAST
-- KNOWN state, not whether it's online RIGHT NOW.
LEFT JOIN PoleVitals rps_online ON p.LocationId = rps_online.LocationId AND rps_online.PeriodType = ?
LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId
OUTER APPLY (
    -- TOP 1 ... ORDER BY LastUpload DESC. OUTER (not CROSS): a pole with
    -- no LocationId, or zero matching PoleTelemetry rows at all, must
    -- still appear in results (with every column below NULL), not
    -- disappear from the summary entirely.
    SELECT TOP 1
        pt.LastUpload, pt.LampPower1, pt.LampPower2,
        pt.BatteryElecCurrent1, pt.BatteryElecCurrent2,
        pt.SolarBoardVoltage, pt.SolarBoardElecCurrent, pt.IsDaylightForPanelFault
    FROM PoleTelemetry pt
    WHERE pt.LocationId = p.LocationId
    ORDER BY pt.LastUpload DESC
) AS latest_pt
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""


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

    lightStatusLabel/panelStatusLabel/panelIdleReason/batteryStatusLabel:
    computed via the exact same shared api_utils.compute_pole_status_
    labels() pole_vitals_api.py's own _pole_row_to_dict() uses -- see
    that function's own docstring for the full logic/reasoning. Four of
    its five keys are included here, per explicit request;
    electricCurrentAverage is popped back out before merging into this
    dict below, rather than this shared function ever being asked to
    return only four keys for this one caller -- keeping
    compute_pole_status_labels()'s own contract simple and total, with
    "which of the five to actually expose" left entirely up to each
    caller.
    """
    (
        project_id,
        pole_id,
        pole_number,
        location_id,
        install_date,
        lat,
        long_,
        last_update,
        lamp_power_1,
        lamp_power_2,
        battery_elec_current_1,
        battery_elec_current_2,
        solar_board_voltage,
        solar_board_elec_current,
        is_daylight_for_panel_fault,
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

    status_labels = compute_pole_status_labels(
        has_telemetry=last_update is not None,
        lamp_power_1=lamp_power_1,
        lamp_power_2=lamp_power_2,
        battery_elec_current_1=battery_elec_current_1,
        battery_elec_current_2=battery_elec_current_2,
        solar_board_voltage=solar_board_voltage,
        solar_board_elec_current=solar_board_elec_current,
        is_daylight_for_panel_fault=is_daylight_for_panel_fault,
    )
    status_labels.pop("electricCurrentAverage", None)

    return {
        "id": json_safe(pole_id),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(location_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
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
        "projectId": json_safe(project_id),
        "customerId": json_safe(customer_id),
        **status_labels,
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
    lat, long, lastUpdate, batteryVoltage1, batteryVoltage2, isOnline,
    isLedFault, isBatteryFault, isPanelFault, isOpenIssueFault,
    isPoleFault, avgBatteryPercentage, avgPanelPercentage,
    avgLightPercentage, lightStatusLabel, panelStatusLabel,
    panelIdleReason, batteryStatusLabel, electricCurrentAverage -- see
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
    lightStatusLabel/panelStatusLabel/panelIdleReason/
    batteryStatusLabel -- ARE included here too, per explicit request,
    even though electricCurrentAverage's own underlying inputs are now
    fetched regardless) -- but still includes lastUpdate, via that same
    template's own PoleTelemetry lookup (see
    _POLE_SUMMARY_SQL_TEMPLATE's own comment for why lastUpdate earns
    that cost while the two voltage fields don't: the web frontend needs
    lastUpdate to distinguish "Disconnected" from "Unknown", a
    distinction IsOnline alone can't make). Built for a "give me every
    pole" consumer (e.g. a map rendering all ~14K poles at once) that
    needs location, status, staleness, and now these four status labels
    too, to plot/color a pin, not full per-pole telemetry detail --
    that fuller detail (including electricCurrentAverage) is still
    available cheaply for one specific pole via pole_id, which always
    uses the full query regardless of this flag. Also raises the
    unfiltered case's limit ceiling to _SUMMARY_MAX_LIMIT instead of
    api_utils.MAX_LIMIT, since summary mode's whole reason to exist is
    making "every pole in one call" practical.
    """
    if pole_id or project_id or customer_id:
        # Build up whichever of the three conditions were actually
        # given, combined with AND -- e.g. poleId+projectId together
        # means "this pole, AND verify it belongs to this project",
        # not "poleId wins, projectId is silently ignored".
        conditions = []
        # Both templates below now have TWO period-type placeholders,
        # not one -- rps (LastKnown48Hours, first) for most fields,
        # rps_online (Last48Hours, second) for IsOnline specifically --
        # see _POLE_DETAILS_SQL_TEMPLATE's own comment on rps_online (and
        # _POLE_SUMMARY_SQL_TEMPLATE's matching one) for why that one
        # field reverts to the rollup's own period type. Both templates
        # share this exact same two-parameter prefix, so this single
        # params list serves either one.
        params = [_POLE_DETAIL_PERIOD_TYPE, _ROLLUP_PERIOD_TYPE]
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
        params = (_POLE_DETAIL_PERIOD_TYPE, _ROLLUP_PERIOD_TYPE, row_limit)

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
