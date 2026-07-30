from shared.api_utils import clamp_limit, json_safe
from shared.sql_client import get_connection

# LightStatus values counted as "working" for this rollup, per an
# explicit business rule: DayLight means "not expected to be lit right
# now, nothing wrong", Working means "confirmed lit correctly at night"
# -- both are "the light is fine" states. Only 'Not Working' (online,
# night, both lamps dark) is the genuine fault. See
# pole_vitals_loader.py's module docstring for the full per-reading
# classification this is built on.
_WORKING_LIGHT_STATUSES = ("Working", "DayLight")

# Which PoleVitals period type drives this rollup's classification --
# Hour, not Day/Week/Month, since this is meant to answer "what's each
# pole's status right now", the finest-grained/most-current signal
# available. Deliberately a single named constant, not buried in the SQL
# string, so this choice is easy to find and change later if a different
# period type turns out to be wanted instead.
_STATUS_PERIOD_TYPE = "Hour"

# How far back to roll up each pole's Hour-period PoleVitals rows when
# computing its current status -- not just the single most recent Hour
# row, but every Hour row within this window, averaged/aggregated
# together. A longer, steadier signal than any one Hour bucket alone,
# less prone to a single noisy or transient reading swinging the result.
# Interpolated directly into both SQL templates below (not just left as
# documentation) so the constant and the SQL can't drift apart.
_RECENT_HOURS_WINDOW = 6

# One row per Project (with its Customer attached), aggregating over
# every Pole belonging to that project and each pole's own rolled-up
# LightStatus across the last _RECENT_HOURS_WINDOW hours of Hour-period
# PoleVitals rows.
#
# RecentPoleStats aggregates PoleVitals rows within the window using the
# SAME priority-based logic PoleVitals' own bucket-level aggregation
# uses (see pole_vitals_loader.py's module docstring): if ANY row in the
# window is 'Not Working', the whole window is 'Not Working'; else if
# ANY row is 'Working', the window is 'Working'; only if neither ever
# occurred does it fall back to 'DayLight'. A row with LightStatus IS
# NULL (unresolved daylight status for that specific hour) doesn't count
# toward either MAX() check, matching the same "excluded, not guessed"
# treatment used at the single-bucket level.
#
# LEFT JOIN Poles->RecentPoleStats (not INNER): a pole with zero Hour
# PoleVitals rows in the window (installed, but no telemetry processed
# for it yet, or none recent enough) must still count toward
# TotalLights -- COUNT(*) in PoleWithStatus counts every pole
# regardless, while the SUM(CASE...) expressions only count LightStatus
# values that are actually present, so an unclassified pole contributes
# to neither WorkingCount nor TotalFaults -- it's counted separately, as
# NoTelemetryCount.
#
# LEFT JOIN Projects->ProjectAgg (not INNER): a project with zero poles
# must still appear, with every count column at 0, rather than being
# silently dropped from the result entirely.
_FETCH_SQL_TEMPLATE = """
;WITH RecentPoleStats AS (
    SELECT
        LocationId,
        CASE
            WHEN MAX(CASE WHEN LightStatus = 'Not Working' THEN 1 ELSE 0 END) = 1 THEN 'Not Working'
            WHEN MAX(CASE WHEN LightStatus = 'Working' THEN 1 ELSE 0 END) = 1 THEN 'Working'
            ELSE 'DayLight'
        END AS LightStatus
    FROM PoleVitals
    WHERE PeriodType = ?
      AND PeriodStart >= DATEADD(HOUR, -{hours_window}, SYSDATETIMEOFFSET())
    GROUP BY LocationId
),
PoleWithStatus AS (
    SELECT
        p.Id AS PoleId,
        p.ProjectId,
        rps.LightStatus
    FROM Poles p
    LEFT JOIN RecentPoleStats rps ON p.LocationId = rps.LocationId
),
ProjectAgg AS (
    SELECT
        ProjectId,
        COUNT(*) AS TotalLights,
        SUM(CASE WHEN LightStatus IN ('Working', 'DayLight') THEN 1 ELSE 0 END) AS WorkingCount,
        SUM(CASE WHEN LightStatus = 'Not Working' THEN 1 ELSE 0 END) AS TotalFaults,
        SUM(CASE WHEN LightStatus IS NULL THEN 1 ELSE 0 END) AS NoTelemetryCount
    FROM PoleWithStatus
    GROUP BY ProjectId
)
SELECT
    c.Id AS CustomerId,
    c.Name AS CustomerName,
    proj.Id AS ProjectId,
    proj.Name AS ProjectName,
    ISNULL(pa.TotalLights, 0) AS TotalLights,
    ISNULL(pa.WorkingCount, 0) AS WorkingCount,
    ISNULL(pa.TotalFaults, 0) AS TotalFaults,
    ISNULL(pa.NoTelemetryCount, 0) AS NoTelemetryCount
FROM Customers c
LEFT JOIN Projects proj ON proj.CustomerId = c.Id
LEFT JOIN ProjectAgg pa ON pa.ProjectId = proj.Id
{where_clause}
ORDER BY c.Name, proj.Name
"""

# A SEPARATE query from _FETCH_SQL_TEMPLATE above, purely additive: one
# row per individual Pole, for attaching a "poles" list to each project
# dict. Deliberately NOT merged into the same query as the aggregates --
# mixing detail rows and aggregate rows in one T-SQL result set is
# awkward without FOR JSON/STRING_AGG tricks that would complicate the
# already-tested aggregation query for no real benefit. Reuses the exact
# same {where_clause} text as the aggregate query (both alias Projects as
# "proj" and Customers as "c"), so both queries stay scoped identically
# to the same customer(s)/project without duplicating the filter logic.
#
# RecentPoleStats here computes MORE than the aggregate query's version
# above: alongside the same priority-based LightStatus rollup, it also
# averages the three per-reading percentage metrics (Battery/Panel/Light)
# and rolls up IsOnline as "was any reading in the window online" (MAX),
# matching PoleVitals' own bucket-level IsOnline semantics. CAST(...AS
# BIT) on IsOnline matters, not decorative: MAX(CASE WHEN...) produces a
# plain INT (0/1), and without the cast pyodbc would hand that back as a
# Python int, so isOnline would serialize as 1/0 in the JSON output
# instead of true/false -- the CAST keeps pyodbc's native BIT->bool
# conversion in play, same as reading a real BIT column directly would.
#
# OUTER APPLY (not a JOIN/CTE) for each pole's single most recent
# PoleTelemetry row (LastUpload, BatteryVoltage1, BatteryVoltage2) --
# genuinely different access pattern than RecentPoleStats above (a
# window aggregation over PoleVitals, a comparatively small, precomputed
# table), and different again from pole_vitals_loader.py's own PoleTelemetry
# queries (which scan a multi-day lookback window across ALL poles at
# once). Here we want exactly one raw row per pole, and PoleTelemetry's
# own PRIMARY KEY is (LocationId, LastUpload) -- LocationId leading means
# `TOP 1 ... WHERE LocationId = @x ORDER BY LastUpload DESC` seeks
# directly into that one pole's rows in the clustered index, rather than
# scanning the table -- OUTER APPLY driven per-pole from Poles (a small,
# bounded table, unlike PoleTelemetry's own multi-million-row scale) is
# the natural way to express "correlated per-row TOP-1 lookup" in T-SQL.
# OUTER, not CROSS: a pole with no LocationId, or one with zero matching
# PoleTelemetry rows, must still appear in the result (with these three
# columns NULL) rather than being dropped entirely -- same "still
# appears, just unclassified" philosophy used throughout this query.
#
# Plain INNER JOINs for Poles->Projects->Customers, unlike the aggregate
# query's LEFT JOINs: no phantom-row handling is needed here
# specifically, since a project or customer with zero matching poles
# simply returns zero rows for this query -- the aggregate query already
# correctly reports totalLights=0 etc. for that case, and an empty
# "poles" list falls out naturally when grouping these rows in Python (a
# project that isn't a key in the resulting dict just gets [] when
# looked up).
# The "full" RecentPoleStats CTE -- LightStatus (priority-based rollup),
# IsOnline (any-online-in-window), and the three averaged percentage
# metrics, all from the same GROUP BY LocationId aggregation pass over
# PoleVitals. Factored out into its own constant (rather than inlined
# directly into _POLE_DETAILS_SQL_TEMPLATE below) specifically so
# shared/poles_api.py's lighter "summary" query (see that module) can
# reuse the exact same CASE/MAX logic without a second, independently
# -maintained copy that could quietly drift out of sync with this one.
# _FETCH_SQL_TEMPLATE above intentionally does NOT use this -- it only
# ever needs LightStatus for its own aggregate counts, not the other
# four columns, so duplicating just the LightStatus CASE expression
# there (not the whole CTE) keeps that query from computing three
# unused AVG()s for no reason.
_RECENT_POLE_STATS_CTE = """RecentPoleStats AS (
    SELECT
        LocationId,
        ROUND(AVG(AvgBatteryPercentage), 2) AS BatteryPercentage,
        ROUND(AVG(AvgPanelPercentage), 2) AS PanelPercentage,
        ROUND(AVG(AvgLightPercentage), 2) AS LightPercentage,
        CAST(MAX(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END) AS BIT) AS IsOnline,
        CASE
            WHEN MAX(CASE WHEN LightStatus = 'Not Working' THEN 1 ELSE 0 END) = 1 THEN 'Not Working'
            WHEN MAX(CASE WHEN LightStatus = 'Working' THEN 1 ELSE 0 END) = 1 THEN 'Working'
            ELSE 'DayLight'
        END AS LightStatus
    FROM PoleVitals
    WHERE PeriodType = ?
      AND PeriodStart >= DATEADD(HOUR, -{hours_window}, SYSDATETIMEOFFSET())
    GROUP BY LocationId
)"""

_POLE_DETAILS_SQL_TEMPLATE = (
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
    latest_pt.LastUpload AS LastUpload,
    latest_pt.BatteryVoltage1 AS BatteryVoltage1,
    latest_pt.BatteryVoltage2 AS BatteryVoltage2,
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
OUTER APPLY (
    SELECT TOP 1 pt.LastUpload, pt.BatteryVoltage1, pt.BatteryVoltage2
    FROM PoleTelemetry pt
    WHERE pt.LocationId = p.LocationId
    ORDER BY pt.LastUpload DESC
) AS latest_pt
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""
)


def _working_percentage(working_count: int, total_lights: int) -> float:
    """
    0 when total_lights is 0 (nothing to be a percentage OF), not a
    divide-by-zero error and not None -- a plain 0.0 is a safer default
    for a numeric field a consuming website will likely render directly
    (e.g. into a progress bar) than a null it may not expect.
    """
    if total_lights == 0:
        return 0.0
    return round((working_count / total_lights) * 100, 2)


def _pole_row_to_dict(row) -> dict:
    """Converts one row from _POLE_DETAILS_SQL_TEMPLATE into its own
    dict for a project's "poles" list.

    lightStatus is None (JSON null) for a pole with no Hour PoleVitals
    rows in the recent window -- deliberately not a made-up string like
    "No Telemetry", since that's not a real LightStatus value
    (CK_PoleVitals_LightStatus only allows 'Working', 'DayLight', 'Not
    Working') and null more accurately mirrors what's actually in the
    database: nothing, not a fourth status. isOnline is the same pole's
    rolled-up PoleVitals.IsOnline across that same window (was any
    reading in it online) -- also None for an unclassified pole, for the
    same reason. The three avg*Percentage fields are each simply
    averaged across the window's Hour rows (already NULL-safe -- AVG()
    ignores NULLs, and a pole with zero rows in the window naturally
    gets NULL for all three via the LEFT JOIN).

    installDate/lat/long come straight from Poles -- static install-time
    facts, not derived from any telemetry or vitals aggregation.

    lastUpdate/batteryVoltage1/batteryVoltage2 come from that pole's
    single most recent PoleTelemetry row (via the OUTER APPLY in
    _POLE_DETAILS_SQL_TEMPLATE) -- the raw reading itself, genuinely
    different from avgBatteryPercentage (which is PoleVitals' own
    aggregate of BatteryElecCurrent1/2, a DIFFERENT pair of columns from
    BatteryVoltage1/2). All three are None for a pole with no LocationId
    or no matching PoleTelemetry rows at all -- same "unclassified, not
    a fabricated zero" treatment as everything else here.

    The row's ProjectId (first column) and CustomerId (last column,
    added for shared/poles_api.py's benefit -- see that module's
    docstring) are both deliberately NOT included in this function's own
    output: getPoleVitals nests each pole under its own project (already
    under its own customer), so both are already implied by that
    nesting; only poles_api.py's flat, non-nested getPoles listing needs
    them included explicitly, which it adds itself by reading straight
    from the row rather than through this function."""
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
        light_status,
        is_online,
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
        "lightStatus": json_safe(light_status),
        "isOnline": json_safe(is_online),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
    }


def _row_to_project_dict(row, poles: list) -> dict:
    _, _, project_id, project_name, total_lights, working_count, total_faults, no_telemetry_count = row
    return {
        "id": json_safe(project_id),
        "name": json_safe(project_name),
        "totalLights": json_safe(total_lights),
        "workingPercentage": _working_percentage(working_count, total_lights),
        "optimisticWorkingPercentage": _working_percentage(
            working_count + no_telemetry_count, total_lights
        ),
        "totalFaults": json_safe(total_faults),
        "totalNonTelemetryAvailable": json_safe(no_telemetry_count),
        "poles": poles,
    }


def _sum_pole_stats(rows) -> tuple:
    """
    Sums TotalLights/WorkingCount/TotalFaults/NoTelemetryCount (columns
    4/5/6/7) across a set of project rows -- used for the customer-level
    rollup, which is a true pole-weighted aggregate (sum of working /
    sum of total across every one of that customer's projects), not an
    average of each project's own already-rounded percentage --
    averaging percentages would give a tiny project equal weight to a
    huge one, misrepresenting the customer's actual overall pole health.

    Callers must exclude any "phantom" no-project row (ProjectId, column
    2, is NULL -- a customer with zero projects) before calling this,
    since such a row has None for these columns, not 0.
    """
    total_lights = sum(row[4] for row in rows)
    working_count = sum(row[5] for row in rows)
    total_faults = sum(row[6] for row in rows)
    no_telemetry_count = sum(row[7] for row in rows)
    return total_lights, working_count, total_faults, no_telemetry_count


def _customer_rollup_fields(rows) -> dict:
    """Returns the five customer-level rollup fields (totalLights,
    workingPercentage, optimisticWorkingPercentage, totalFaults,
    totalNonTelemetryAvailable), computed via _sum_pole_stats() over
    rows -- all 0/0.0 if rows is empty (a customer with no real
    projects)."""
    if not rows:
        return {
            "totalLights": 0,
            "workingPercentage": 0.0,
            "optimisticWorkingPercentage": 0.0,
            "totalFaults": 0,
            "totalNonTelemetryAvailable": 0,
        }
    total_lights, working_count, total_faults, no_telemetry_count = _sum_pole_stats(rows)
    return {
        "totalLights": total_lights,
        "workingPercentage": _working_percentage(working_count, total_lights),
        "optimisticWorkingPercentage": _working_percentage(
            working_count + no_telemetry_count, total_lights
        ),
        "totalFaults": total_faults,
        "totalNonTelemetryAvailable": no_telemetry_count,
    }


def get_pole_vitals(customer_id: str = None, project_id: str = None, limit: int = None):
    """
    Returns each Customer's Projects, each annotated with pole-health
    rollup stats (totalLights, workingPercentage,
    optimisticWorkingPercentage, totalFaults, totalNonTelemetryAvailable)
    and a "poles" list (one entry per Pole belonging to that project:
    id, poleNumber, locationId, installDate, lat, long, lastUpdate,
    batteryVoltage1, batteryVoltage2, lightStatus, isOnline,
    avgBatteryPercentage, avgPanelPercentage, avgLightPercentage) --
    computed from every Pole belonging to that project and each pole's
    own Hour-period PoleVitals rows from the last _RECENT_HOURS_WINDOW
    hours (not just the single most recent one) -- see
    _WORKING_LIGHT_STATUSES, _STATUS_PERIOD_TYPE, and
    _RECENT_HOURS_WINDOW above for the three business-rule choices this
    is built on. lightStatus/isOnline are rolled up across that window
    using the same priority-based aggregation PoleVitals' own
    bucket-level aggregation uses (Not Working beats Working beats the
    DayLight default; IsOnline is "was any reading in the window
    online") -- not just read from a single Hour bucket. The three
    avg*Percentage fields are a plain average of that same window's Hour
    rows. totalNonTelemetryAvailable is a pole with zero Hour PoleVitals
    rows in the window (LightStatus IS NULL after the LEFT JOIN) --
    counted separately, not folded into totalFaults or workingPercentage,
    since "we don't know" is a genuinely different state from "confirmed
    broken"; the same pole shows up in "poles" with lightStatus: null,
    isOnline: null, and all three avg*Percentage fields null.
    installDate/lat/long come straight from Poles -- static, unrelated to
    any telemetry or vitals data (present even for a pole with neither).
    lastUpdate/batteryVoltage1/batteryVoltage2 come from that pole's own
    single most recent PoleTelemetry row (an OUTER APPLY, not the
    PoleVitals-based rollup above) -- the raw reading itself, distinct
    from avgBatteryPercentage (PoleVitals' own aggregate of a DIFFERENT
    pair of PoleTelemetry columns, BatteryElecCurrent1/2, not
    BatteryVoltage1/2). All three are None for a pole with no LocationId
    or no matching PoleTelemetry rows at all.
    optimisticWorkingPercentage is the same percentage computed as if
    every one of those unclassified poles WERE working ((workingCount +
    noTelemetryCount) / totalLights) -- the best-case reading, alongside
    workingPercentage's more conservative one (which excludes them from
    the numerator entirely). The Customer itself ALSO carries the same
    five rollup fields (but NOT a "poles" list of its own -- poles only
    ever appear nested under their own project), summed across all of
    that customer's own projects -- a true pole-weighted aggregate for
    both percentages (sum of working, or sum of working plus
    non-telemetry, / sum of total), not an average of each project's own
    percentage (see _sum_pole_stats()'s docstring for why that
    distinction matters).

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
    that one customer (with its own totalLights/workingPercentage/
    totalFaults, and a nested "projects" list, one entry per project --
    including projects with zero poles, AND an empty list with all
    rollup fields at 0/0.0 if the customer itself has zero projects), or
    None if that customer doesn't exist. NOT a list -- a customerId
    always identifies at most one customer, unlike projects_api.py's
    customer_id filter (which returns that customer's many projects as a
    genuine list).
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
            _FETCH_SQL_TEMPLATE.format(where_clause=where_clause, hours_window=_RECENT_HOURS_WINDOW),
            *params,
        )
        rows = cursor.fetchall()

        # Same where_clause/params as above, reused as-is (see
        # _POLE_DETAILS_SQL_TEMPLATE's own comment for why this is a
        # separate query rather than merged into the one above).
        cursor.execute(
            _POLE_DETAILS_SQL_TEMPLATE.format(where_clause=where_clause, hours_window=_RECENT_HOURS_WINDOW),
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
# get_pole_vitals() above: that one rolls up the last _RECENT_HOURS_WINDOW
# hours of 'Hour'-period PoleVitals rows into one steady current-status
# signal. This one returns a pole's FULL history of PoleVitals rows for a
# CALLER-CHOSEN period type, each read directly -- no rollup, no averaging
# across rows, no priority-based LightStatus aggregation across a window.
# Different use case: "show me every one of this pole's own Hour buckets
# (or Day buckets), as-is", not "give me a steadier, less noise-prone
# current-status summary".

# Valid PoleVitals period types -- Week/Month were removed from
# load_pole_vitals() entirely (see that loader's own module docstring and
# the README for that history), so only these two remain.
_VALID_PERIOD_TYPES = ("Hour", "Day")

# How many history rows to return by default/at most when a caller
# doesn't specify limit -- PoleVitals has no retention/cleanup of its
# own (unlike PoleTelemetry), so it grows by one row per pole per Hour
# (or Day) forever; "all of it, no bound at all" would eventually mean
# thousands of rows for a pole with a long install history. Reuses
# api_utils.clamp_limit()'s existing DEFAULT_LIMIT/MAX_LIMIT, same as
# every other list-returning endpoint in this project, rather than
# inventing a separate cap just for this one.

# A pole's static facts -- id, poleNumber, locationId, installDate, lat,
# long, lastUpdate -- are properties of the POLE, not of any individual
# PoleVitals bucket, so they're fetched once here rather than repeated
# on every history entry (which would be wasteful once this can return
# many rows). Only ONE OUTER APPLY now (PoleTelemetry, for lastUpdate) --
# batteryVoltage1/batteryVoltage2 were dropped from this endpoint
# entirely per explicit request, so there's nothing else needing that
# join. OUTER, not CROSS: a pole with no PoleTelemetry row yet must
# still be returned (with lastUpdate null), not dropped.
_POLE_INFO_FOR_HISTORY_SQL_TEMPLATE = """
SELECT
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.LocationId AS LocationId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    latest_pt.LastUpload AS LastUpload
FROM Poles p
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
# no CTE/aggregation machinery (unlike _RECENT_POLE_STATS_CTE, built for
# rolling multiple rows into one). PeriodStart/PeriodEnd are included
# specifically so each entry can actually be told apart from the others
# -- without them, an array of otherwise-identical-shaped percentage
# values would have no way to say which hour/day each one belongs to.
# Ordered most-recent-first, matching every other "give me a list"
# endpoint in this project, so a TOP(?)-bounded result still returns the
# most current data rather than an arbitrary/oldest slice.
_POLE_VITALS_HISTORY_SQL_TEMPLATE = """
SELECT TOP (?)
    pv.PeriodStart AS PeriodStart,
    pv.PeriodEnd AS PeriodEnd,
    pv.LightStatus AS LightStatus,
    pv.IsOnline AS IsOnline,
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
    given bucket doesn't have -- though in practice every PoleVitals row
    that exists at all already has every one of these columns populated
    by load_pole_vitals(); null here would mean something upstream
    changed, not an expected/normal case the way it is for a pole with
    no PoleVitals rows at all yet."""
    (
        period_start,
        period_end,
        light_status,
        is_online,
        battery_percentage,
        panel_percentage,
        light_percentage,
    ) = row
    return {
        "periodStart": json_safe(period_start),
        "periodEnd": json_safe(period_end),
        "lightStatus": json_safe(light_status),
        "isOnline": json_safe(is_online),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
    }


def get_pole_vitals_by_period(pole_id: str, period_type: str, limit: int = None):
    """
    Returns a single pole's static info (id, poleNumber, locationId,
    installDate, lat, long, lastUpdate) plus its full history of
    PoleVitals rows for the given period_type, each entry as its own
    dict in a "vitals" list (periodStart, periodEnd, lightStatus,
    isOnline, avgBatteryPercentage, avgPanelPercentage,
    avgLightPercentage). Deliberately NO rollup/aggregation across
    entries -- each one is a direct read of one PoleVitals row, unlike
    get_pole_vitals()'s 6-hour-window rollup (which uses the same
    priority logic PoleVitals' own bucket-level aggregation does).
    batteryVoltage1/batteryVoltage2 are not included at all -- dropped
    per explicit request, along with the PoleTelemetry join that would
    otherwise be needed to get them.

    period_type: must be 'Hour' or 'Day' -- Week/Month were removed from
    PoleVitals entirely (see pole_vitals_loader.py's own module docstring
    and the README for that history). Raises ValueError for anything
    else; the HTTP layer maps that to a 400.

    limit: max number of history entries returned, most-recent-first.
    Defaults to DEFAULT_LIMIT, capped at MAX_LIMIT (see
    shared/api_utils.py) -- PoleVitals has no retention/cleanup of its
    own, so an actually-unbounded "every row that has ever existed"
    isn't offered here.

    Returns None if no Pole exists with that id. If the pole exists but
    has no PoleTelemetry row yet, lastUpdate comes back null. If it has
    no PoleVitals rows of the requested period_type yet, "vitals" comes
    back as an empty list -- not an error, and not a 404 (the pole
    itself was found; it just has no history yet for this period type).
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
