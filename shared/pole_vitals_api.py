from shared.api_utils import clamp_limit, json_safe
from shared.sql_client import get_connection

# Which PoleVitals period type drives this rollup's classification --
# Last48Hours specifically, not Hour/Day: it's a single, continuously
# updated row per pole (see pole_vitals_loader.py's own module docstring
# for why that period type is structured that way), so reading it
# directly IS "what's each pole's status right now" -- no window
# aggregation needed here at all, unlike the Hour-based rolling-window
# design this replaced.
_STATUS_PERIOD_TYPE = "Last48Hours"

# One row per Project (with its Customer attached), aggregating over
# every Pole belonging to that project and each pole's own Last48Hours
# PoleVitals row.
#
# Population/rollup design (replaces the earlier LightStatus-based
# workingPercentage/optimisticWorkingPercentage/totalNonTelemetryAvailable
# entirely):
#   totalLights (population) = poles that are IsOnline, PLUS poles that
#     are NOT online but DO have an open issue (IsOpenIssueFault) -- a
#     pole that's neither online nor known to have an issue is excluded
#     from the population entirely, not counted as "not working". This
#     is a deliberate redefinition: such a pole (never reported, or gone
#     silent with nothing filed against it) is treated as outside the
#     currently-relevant fleet, not as a broken one.
#   connectedLights = poles that are IsOnline (a strict subset of
#     totalLights above).
#   totalFaults = poles WITHIN the population above whose IsPoleFault is
#     true -- a pole excluded from the population can't be a "fault"
#     either, by construction.
#   percentWorking = (totalLights - totalFaults) / totalLights * 100 --
#     computed in Python (_percent_working()), not SQL, same reasoning
#     as everywhere else numeric rollups are computed here.
#
# "IsOnline = 1 OR IsOpenIssueFault = 1" needs no explicit NULL-handling:
# a pole with no Last48Hours row at all gets NULL for both columns via
# the LEFT JOIN below, and "NULL = 1" is UNKNOWN (not TRUE) in T-SQL, so
# it naturally falls through to "not in the population" without an
# ISNULL() guard.
#
# LEFT JOIN Poles->RecentPoleStats (not INNER): a pole with no
# Last48Hours row yet (installed, but no telemetry processed for it, or
# none recent enough to be in the rolling window) must still be
# considered -- it just won't satisfy the population condition above
# unless it has an open issue.
#
# LEFT JOIN Projects->ProjectAgg (not INNER): a project with zero poles
# must still appear, with every count column at 0, rather than being
# silently dropped from the result entirely.
_FETCH_SQL_TEMPLATE = """
;WITH RecentPoleStats AS (
    SELECT LocationId, IsOnline, IsOpenIssueFault, IsPoleFault
    FROM PoleVitals
    WHERE PeriodType = ?
),
PoleWithStatus AS (
    SELECT
        p.Id AS PoleId,
        p.ProjectId,
        rps.IsOnline,
        rps.IsOpenIssueFault,
        rps.IsPoleFault
    FROM Poles p
    LEFT JOIN RecentPoleStats rps ON p.LocationId = rps.LocationId
),
ProjectAgg AS (
    SELECT
        ProjectId,
        SUM(CASE WHEN IsOnline = 1 OR IsOpenIssueFault = 1 THEN 1 ELSE 0 END) AS TotalLights,
        SUM(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END) AS ConnectedLights,
        SUM(
            CASE WHEN (IsOnline = 1 OR IsOpenIssueFault = 1) AND IsPoleFault = 1 THEN 1 ELSE 0 END
        ) AS TotalFaults
    FROM PoleWithStatus
    GROUP BY ProjectId
)
SELECT
    c.Id AS CustomerId,
    c.Name AS CustomerName,
    proj.Id AS ProjectId,
    proj.Name AS ProjectName,
    ISNULL(pa.TotalLights, 0) AS TotalLights,
    ISNULL(pa.ConnectedLights, 0) AS ConnectedLights,
    ISNULL(pa.TotalFaults, 0) AS TotalFaults
FROM Customers c
LEFT JOIN Projects proj ON proj.CustomerId = c.Id
LEFT JOIN ProjectAgg pa ON pa.ProjectId = proj.Id
{where_clause}
ORDER BY c.Name, proj.Name
"""

# A SEPARATE query from _FETCH_SQL_TEMPLATE above, purely additive: one
# row per individual Pole, for attaching a "poles" list to each project
# dict. Deliberately NOT merged into the same query as the aggregates --
# see this module's earlier history for why (mixing detail rows and
# aggregate rows in one T-SQL result set is awkward without FOR JSON/
# STRING_AGG tricks). Reuses the exact same {where_clause} text as the
# aggregate query, so both queries stay scoped identically.
#
# RecentPoleStats here is now a plain, unaggregated SELECT (no GROUP BY
# at all) -- Last48Hours is structurally always 0-or-1 rows per
# LocationId (see pole_vitals_loader.py's _LAST_48_HOURS_MERGE_SQL), so
# there's nothing to aggregate across the way the old Hour-window design
# needed to.
#
# CAST(...AS BIT) on every fault/IsOnline column matters, not decorative:
# PoleVitals.IsOnline/IsLedFault/etc. are already BIT columns, so
# pyodbc's normal BIT->bool conversion already applies without an
# explicit cast here -- unlike the old design's MAX(CASE WHEN...)
# aggregation, which produced a plain INT and needed the cast. Kept
# implicit (no CAST at all) for exactly that reason: there's no
# aggregation happening anymore to strip the native BIT type away.
#
# lastUpdate is converted to the POLE'S OWN local time (via PoleTimeZones,
# falling back to Eastern for an unresolved location) -- not left as
# UTC. AT TIME ZONE on an already-DATETIMEOFFSET value converts its
# displayed offset while preserving the same absolute instant, the same
# operation pole_vitals_loader.py uses extensively for bucketing.
#
# OUTER APPLY (not a JOIN/CTE) for each pole's single most recent
# PoleTelemetry row (LastUpload, BatteryVoltage1, BatteryVoltage2) --
# PoleTelemetry's own PRIMARY KEY is (LocationId, LastUpload), so
# `TOP 1 ... WHERE LocationId = @x ORDER BY LastUpload DESC` seeks
# directly into that one pole's rows rather than scanning the table.
# OUTER, not CROSS: a pole with no LocationId, or zero matching
# PoleTelemetry rows, must still appear (with these columns NULL).
#
# Plain INNER JOINs for Poles->Projects->Customers: a project/customer
# with zero matching poles simply returns zero rows for this query -- the
# aggregate query already correctly reports totalLights=0 etc. for that
# case, and an empty "poles" list falls out naturally in Python.
_POLE_DETAILS_SQL_TEMPLATE = """
SELECT
    proj.Id AS ProjectId,
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.LocationId AS LocationId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
    latest_pt.BatteryVoltage1 AS BatteryVoltage1,
    latest_pt.BatteryVoltage2 AS BatteryVoltage2,
    rps.IsOnline AS IsOnline,
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
LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId
OUTER APPLY (
    SELECT TOP 1 pt.LastUpload, pt.BatteryVoltage1, pt.BatteryVoltage2
    FROM PoleTelemetry pt
    WHERE pt.LocationId = p.LocationId
    ORDER BY pt.LastUpload DESC
) AS latest_pt
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""


def _percent_working(total_lights: int, total_faults: int) -> float:
    """
    0 when total_lights is 0 (nothing to be a percentage OF), not a
    divide-by-zero error and not None -- a plain 0.0 is a safer default
    for a numeric field a consuming website will likely render directly
    (e.g. into a progress bar) than a null it may not expect.
    """
    if total_lights == 0:
        return 0.0
    return round(((total_lights - total_faults) / total_lights) * 100, 2)


def _pole_row_to_dict(row) -> dict:
    """Converts one row from _POLE_DETAILS_SQL_TEMPLATE into its own
    dict for a project's "poles" list.

    isOnline/isLedFault/isBatteryFault/isPanelFault/isOpenIssueFault/
    isPoleFault, and the three avg*Percentage fields, all come directly
    from that pole's own Last48Hours PoleVitals row -- None (JSON null)
    for a pole with no such row yet (installed, but no telemetry
    processed for it, or none recent enough), not a fabricated value.

    installDate/lat/long come straight from Poles -- static install-time
    facts, not derived from any telemetry or vitals aggregation.

    lastUpdate/batteryVoltage1/batteryVoltage2 come from that pole's
    single most recent PoleTelemetry row (via the OUTER APPLY in
    _POLE_DETAILS_SQL_TEMPLATE) -- the raw reading itself, genuinely
    different from avgBatteryPercentage (PoleVitals' own aggregate of a
    DIFFERENT pair of PoleTelemetry columns, BatteryElecCurrent1/2, not
    BatteryVoltage1/2). lastUpdate reflects the POLE'S OWN local time
    (via PoleTimeZones), not UTC -- see _POLE_DETAILS_SQL_TEMPLATE's own
    comment. All three are None for a pole with no LocationId or no
    matching PoleTelemetry rows at all.

    The row's ProjectId (first column) and CustomerId (last column,
    added for shared/poles_api.py's benefit) are both deliberately NOT
    included in this function's own output: getPoleVitals nests each
    pole under its own project (already under its own customer), so both
    are already implied by that nesting; only poles_api.py's flat,
    non-nested getPoles listing needs them included explicitly."""
    (
        _,
        pole_id,
        pole_number,
        location_id,
        install_date,
        lat,
        long_,
        last_update,
        battery_voltage_1,
        battery_voltage_2,
        is_online,
        is_led_fault,
        is_battery_fault,
        is_panel_fault,
        is_open_issue_fault,
        is_pole_fault,
        battery_percentage,
        panel_percentage,
        light_percentage,
        _,  # CustomerId -- see docstring above for why this is discarded here
    ) = row
    return {
        "id": json_safe(pole_id),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(location_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
        "lastUpdate": json_safe(last_update),
        "batteryVoltage1": json_safe(battery_voltage_1),
        "batteryVoltage2": json_safe(battery_voltage_2),
        "isOnline": json_safe(is_online),
        "isLedFault": json_safe(is_led_fault),
        "isBatteryFault": json_safe(is_battery_fault),
        "isPanelFault": json_safe(is_panel_fault),
        "isOpenIssueFault": json_safe(is_open_issue_fault),
        "isPoleFault": json_safe(is_pole_fault),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
    }


def _row_to_project_dict(row, poles: list) -> dict:
    _, _, project_id, project_name, total_lights, connected_lights, total_faults = row
    return {
        "id": json_safe(project_id),
        "name": json_safe(project_name),
        "totalLights": json_safe(total_lights),
        "connectedLights": json_safe(connected_lights),
        "totalFaults": json_safe(total_faults),
        "percentWorking": _percent_working(total_lights, total_faults),
        "poles": poles,
    }


def _sum_pole_stats(rows) -> tuple:
    """
    Sums TotalLights/ConnectedLights/TotalFaults (columns 4/5/6) across a
    set of project rows -- used for the customer-level rollup, which is
    a true pole-weighted aggregate (sum of faults / sum of total across
    every one of that customer's projects), not an average of each
    project's own already-rounded percentage -- averaging percentages
    would give a tiny project equal weight to a huge one, misrepresenting
    the customer's actual overall pole health.

    Callers must exclude any "phantom" no-project row (ProjectId, column
    2, is NULL -- a customer with zero projects) before calling this,
    since such a row has None for these columns, not 0.
    """
    total_lights = sum(row[4] for row in rows)
    connected_lights = sum(row[5] for row in rows)
    total_faults = sum(row[6] for row in rows)
    return total_lights, connected_lights, total_faults


def _customer_rollup_fields(rows) -> dict:
    """Returns the four customer-level rollup fields (totalLights,
    connectedLights, totalFaults, percentWorking), computed via
    _sum_pole_stats() over rows -- all 0/0.0 if rows is empty (a customer
    with no real projects)."""
    if not rows:
        return {
            "totalLights": 0,
            "connectedLights": 0,
            "totalFaults": 0,
            "percentWorking": 0.0,
        }
    total_lights, connected_lights, total_faults = _sum_pole_stats(rows)
    return {
        "totalLights": total_lights,
        "connectedLights": connected_lights,
        "totalFaults": total_faults,
        "percentWorking": _percent_working(total_lights, total_faults),
    }


def get_pole_vitals(customer_id: str = None, project_id: str = None, limit: int = None):
    """
    Returns each Customer's Projects, each annotated with pole-health
    rollup stats (totalLights, connectedLights, totalFaults,
    percentWorking) and a "poles" list (one entry per Pole belonging to
    that project: id, poleNumber, locationId, installDate, lat, long,
    lastUpdate, batteryVoltage1, batteryVoltage2, isOnline, isLedFault,
    isBatteryFault, isPanelFault, isOpenIssueFault, isPoleFault,
    avgBatteryPercentage, avgPanelPercentage, avgLightPercentage) --
    computed from every Pole belonging to that project and each pole's
    own Last48Hours PoleVitals row (a single, continuously-updated
    rolling-window row per pole -- see pole_vitals_loader.py's own module
    docstring for why that period type is structured that way; no
    window-aggregation happens at this API layer at all anymore).

    Rollup design: totalLights (population) counts poles that are
    IsOnline, PLUS poles that aren't online but DO have an open issue
    (IsOpenIssueFault) -- a pole that's neither online nor known to have
    an issue is excluded from the population entirely, not counted as
    broken. connectedLights is just the IsOnline poles. totalFaults is
    poles WITHIN that population whose IsPoleFault is true. percentWorking
    is (totalLights - totalFaults) / totalLights * 100. See
    _FETCH_SQL_TEMPLATE's own comment for the full reasoning.

    installDate/lat/long come straight from Poles -- static, unrelated to
    any telemetry or vitals data (present even for a pole with neither).
    lastUpdate/batteryVoltage1/batteryVoltage2 come from that pole's own
    single most recent PoleTelemetry row (an OUTER APPLY, not the
    PoleVitals-based fields above) -- the raw reading itself, distinct
    from avgBatteryPercentage (PoleVitals' own aggregate of a DIFFERENT
    pair of PoleTelemetry columns, BatteryElecCurrent1/2, not
    BatteryVoltage1/2). lastUpdate reflects the pole's own local time
    zone (via PoleTimeZones), not UTC. All three are None for a pole with
    no LocationId or no matching PoleTelemetry rows at all.

    The Customer itself ALSO carries the same four rollup fields (but NOT
    a "poles" list of its own -- poles only ever appear nested under
    their own project), summed across all of that customer's own projects
    -- a true pole-weighted aggregate (see _sum_pole_stats()'s docstring
    for why that distinction matters), not an average of each project's
    own percentage.

    project_id: if given, returns a SINGLE FLAT dict for that one project
    (customerId/customerName included directly on it for context, not
    nested) or None if not found -- matching customers_api.py/
    projects_api.py's single-entity-lookup contract, just returned as
    None here rather than an empty list, since the HTTP layer decides
    single-object-or-404 shaping either way. If customer_id is ALSO
    given, both conditions apply (verifies the project belongs to that
    customer, same as projects_api.get_projects()). Does NOT include the
    project's customer's own rollup totals -- this is a single-project
    view, not a customer view.
    customer_id: if given WITHOUT project_id, returns a SINGLE dict for
    that one customer (with its own rollup totals, and a nested
    "projects" list, one entry per project -- including projects with
    zero poles, AND an empty list with all rollup fields at 0/0.0 if the
    customer itself has zero projects), or None if that customer doesn't
    exist. NOT a list -- a customerId always identifies at most one
    customer, unlike projects_api.py's customer_id filter.
    limit: max number of CUSTOMERS returned when neither id is given --
    the top-level entity in the unfiltered case. Each returned customer
    still includes ALL of their projects (limit doesn't truncate
    projects within a customer). Defaults to DEFAULT_LIMIT, capped at
    MAX_LIMIT (see shared/api_utils.py). Ignored when either id is given.
    """
    if project_id and customer_id:
        where_clause = "WHERE proj.Id = ? AND c.Id = ?"
        params = (_STATUS_PERIOD_TYPE, project_id, customer_id)
    elif project_id:
        where_clause = "WHERE proj.Id = ?"
        params = (_STATUS_PERIOD_TYPE, project_id)
    elif customer_id:
        where_clause = "WHERE c.Id = ?"
        params = (_STATUS_PERIOD_TYPE, customer_id)
    else:
        # limit applies to CUSTOMERS, the top-level entity here -- can't
        # TOP() the raw query directly (that would truncate PROJECT rows,
        # silently dropping some of one customer's projects rather than
        # dropping whole customers), so this filters to the first N
        # distinct customer Ids first via a subquery, then fetches every
        # project row for those.
        where_clause = "WHERE c.Id IN (SELECT TOP (?) Id FROM Customers ORDER BY Name)"
        params = (_STATUS_PERIOD_TYPE, clamp_limit(limit))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            _FETCH_SQL_TEMPLATE.format(where_clause=where_clause),
            *params,
        )
        rows = cursor.fetchall()

        # Same where_clause/params as above, reused as-is (see
        # _POLE_DETAILS_SQL_TEMPLATE's own comment for why this is a
        # separate query rather than merged into the one above).
        cursor.execute(
            _POLE_DETAILS_SQL_TEMPLATE.format(where_clause=where_clause),
            *params,
        )
        pole_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    poles_by_project_id = {}
    for prow in pole_rows:
        poles_by_project_id.setdefault(prow[0], []).append(_pole_row_to_dict(prow))

    if project_id:
        if not rows:
            return None
        row = rows[0]
        project_dict = _row_to_project_dict(row, poles_by_project_id.get(row[2], []))
        project_dict["customerId"] = json_safe(row[0])
        project_dict["customerName"] = json_safe(row[1])
        return project_dict

    if customer_id:
        if not rows:
            return None
        first_row = rows[0]
        # first_row[2] is ProjectId -- NULL when this customer has zero
        # projects (the LEFT JOIN Projects still produces one "phantom"
        # row for them, with every project column NULL, so there's
        # something to source the customer's own id/name from even
        # with no real project to report).
        has_projects = first_row[2] is not None
        real_rows = rows if has_projects else []
        return {
            "id": json_safe(first_row[0]),
            "name": json_safe(first_row[1]),
            **_customer_rollup_fields(real_rows),
            "projects": [
                _row_to_project_dict(row, poles_by_project_id.get(row[2], []))
                for row in real_rows
            ],
        }

    # Collect each customer's real project rows (excluding any "phantom"
    # no-project row) first, then build each customer's dict in one pass
    # -- needed since the rollup fields require ALL of a customer's rows
    # summed together, not something that can be filled in incrementally
    # as each row is seen.
    customer_names = {}
    customer_rows = {}
    customer_order = []
    for row in rows:
        row_customer_id = row[0]
        if row_customer_id not in customer_rows:
            customer_names[row_customer_id] = row[1]
            customer_rows[row_customer_id] = []
            customer_order.append(row_customer_id)
        if row[2] is not None:  # row[2] is ProjectId -- NULL for the phantom row
            customer_rows[row_customer_id].append(row)

    return [
        {
            "id": json_safe(cid),
            "name": json_safe(customer_names[cid]),
            **_customer_rollup_fields(customer_rows[cid]),
            "projects": [
                _row_to_project_dict(row, poles_by_project_id.get(row[2], []))
                for row in customer_rows[cid]
            ],
        }
        for cid in customer_order
    ]


# --------------------------------------------------------------------------
# get_pole_vitals_by_period() -- a genuinely different kind of query from
# get_pole_vitals() above: that one reads each pole's single Last48Hours
# PoleVitals row directly (no window/aggregation at all -- it's already a
# single row per pole). This one returns a pole's FULL HISTORY of
# PoleVitals rows for a CALLER-CHOSEN period type (Hour or Day -- genuine
# historical buckets, unlike Last48Hours), each read directly, exactly as
# stored.

# Valid PoleVitals period types for THIS function specifically --
# Last48Hours is deliberately excluded: it's a single current-state row,
# not a history to page through, so "give me its history" doesn't apply
# to it the way it does for Hour/Day.
_VALID_PERIOD_TYPES = ("Hour", "Day")

# A pole's static facts -- id, poleNumber, locationId, installDate, lat,
# long, lastUpdate -- are properties of the POLE, not of any individual
# PoleVitals bucket, so they're fetched once here rather than repeated
# on every history entry (which would be wasteful once this can return
# many rows). Only ONE OUTER APPLY (PoleTelemetry, for lastUpdate) --
# batteryVoltage1/batteryVoltage2 were dropped from this endpoint
# entirely per earlier explicit request. lastUpdate reflects the pole's
# own local time zone (via PoleTimeZones), same as get_pole_vitals()'s
# per-pole lastUpdate -- not UTC. OUTER, not CROSS: a pole with no
# PoleTelemetry row yet must still be returned (with lastUpdate null).
_POLE_INFO_FOR_HISTORY_SQL_TEMPLATE = """
SELECT
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.LocationId AS LocationId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload
FROM Poles p
LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId
OUTER APPLY (
    SELECT TOP 1 pt.LastUpload
    FROM PoleTelemetry pt
    WHERE pt.LocationId = p.LocationId
    ORDER BY pt.LastUpload DESC
) AS latest_pt
WHERE p.Id = ?
"""

# The actual history: every PoleVitals row for this pole's LocationId and
# the caller-specified period type, each returned exactly as stored --
# no aggregation. PeriodStart/PeriodEnd are included specifically so each
# entry can actually be told apart from the others. Ordered
# most-recent-first, so a TOP(?)-bounded result still returns the most
# current data rather than an arbitrary/oldest slice.
_POLE_VITALS_HISTORY_SQL_TEMPLATE = """
SELECT TOP (?)
    pv.PeriodStart AS PeriodStart,
    pv.PeriodEnd AS PeriodEnd,
    pv.IsOnline AS IsOnline,
    pv.IsLedFault AS IsLedFault,
    pv.IsBatteryFault AS IsBatteryFault,
    pv.IsPanelFault AS IsPanelFault,
    pv.IsOpenIssueFault AS IsOpenIssueFault,
    pv.IsPoleFault AS IsPoleFault,
    pv.AvgBatteryPercentage AS AvgBatteryPercentage,
    pv.AvgPanelPercentage AS AvgPanelPercentage,
    pv.AvgLightPercentage AS AvgLightPercentage
FROM PoleVitals pv
JOIN Poles p ON p.LocationId = pv.LocationId
WHERE p.Id = ? AND pv.PeriodType = ?
ORDER BY pv.PeriodStart DESC
"""


def _pole_vitals_history_row_to_dict(row) -> dict:
    """Converts one row from _POLE_VITALS_HISTORY_SQL_TEMPLATE -- one
    entry in the "vitals" array. Same null-handling convention as the
    rest of this module: null (never a fabricated value) for anything a
    given bucket doesn't have."""
    (
        period_start,
        period_end,
        is_online,
        is_led_fault,
        is_battery_fault,
        is_panel_fault,
        is_open_issue_fault,
        is_pole_fault,
        battery_percentage,
        panel_percentage,
        light_percentage,
    ) = row
    return {
        "periodStart": json_safe(period_start),
        "periodEnd": json_safe(period_end),
        "isOnline": json_safe(is_online),
        "isLedFault": json_safe(is_led_fault),
        "isBatteryFault": json_safe(is_battery_fault),
        "isPanelFault": json_safe(is_panel_fault),
        "isOpenIssueFault": json_safe(is_open_issue_fault),
        "isPoleFault": json_safe(is_pole_fault),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
    }


def get_pole_vitals_by_period(pole_id: str, period_type: str, limit: int = None):
    """
    Returns a single pole's static info (id, poleNumber, locationId,
    installDate, lat, long, lastUpdate) plus its full history of
    PoleVitals rows for the given period_type, each entry as its own
    dict in a "vitals" list (periodStart, periodEnd, isOnline,
    isLedFault, isBatteryFault, isPanelFault, isOpenIssueFault,
    isPoleFault, avgBatteryPercentage, avgPanelPercentage,
    avgLightPercentage). Deliberately NO rollup/aggregation across
    entries -- each one is a direct read of one PoleVitals row.

    period_type: must be 'Hour' or 'Day' -- Last48Hours is excluded (see
    _VALID_PERIOD_TYPES' own comment for why: it's a single current-state
    row, not a history to page through). Raises ValueError for anything
    else; the HTTP layer maps that to a 400.

    limit: max number of history entries returned, most-recent-first.
    Defaults to DEFAULT_LIMIT, capped at MAX_LIMIT (see
    shared/api_utils.py).

    Returns None if no Pole exists with that id. If the pole exists but
    has no PoleTelemetry row yet, lastUpdate comes back null. If it has
    no PoleVitals rows of the requested period_type yet, "vitals" comes
    back as an empty list -- not an error, and not a 404.
    """
    if period_type not in _VALID_PERIOD_TYPES:
        raise ValueError(f"periodType must be one of: {', '.join(_VALID_PERIOD_TYPES)}")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(_POLE_INFO_FOR_HISTORY_SQL_TEMPLATE, pole_id)
        pole_row = cursor.fetchone()
        if pole_row is None:
            return None

        cursor.execute(
            _POLE_VITALS_HISTORY_SQL_TEMPLATE,
            clamp_limit(limit),
            pole_id,
            period_type,
        )
        vitals_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    pole_id_, pole_number, location_id, install_date, lat, long_, last_update = pole_row
    return {
        "id": json_safe(pole_id_),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(location_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
        "lastUpdate": json_safe(last_update),
        "vitals": [_pole_vitals_history_row_to_dict(row) for row in vitals_rows],
    }
