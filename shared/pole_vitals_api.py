from shared.api_utils import (
    clamp_limit,
    compute_pole_local_sunset,
    compute_pole_status_labels,
    compute_pole_connectivity_labels,
    compute_reporting_staleness_label,
    json_safe,
)
from shared.sql_client import get_connection

# Which PoleVitals period type drives BOTH the rollup classification
# (totalLights/connectedLights/totalFaults/percentWorking) AND every
# per-pole detail field (isOnline/isPoleFault/isPanelFault/isLedFault/
# isBatteryFault/isOpenIssueFault/avgBatteryPercentage/avgPanelPercentage/
# avgLightPercentage) -- Last48Hours specifically, not Hour: it's a
# single, continuously updated row per pole (see pole_vitals_loader.py's
# own module docstring for why that period type is structured that way),
# so reading it directly IS "what's each pole's status right now" -- no
# window aggregation needed here at all, unlike the Hour-based
# rolling-window design this replaced.
#
# A pole with no CURRENT Last48Hours row (gone silent -- no telemetry at
# all within the rolling 48-hour window) gets NULL for every one of the
# per-pole detail fields above, via the LEFT JOIN below -- genuinely
# null, not a fallback to whatever that pole's own last-known values used
# to be. A separate period type (LastKnown48Hours) used to persist a
# silent pole's last-known state specifically to avoid that null; it was
# removed entirely by explicit request/correction -- null is now the
# correct, intended signal for "this pole isn't currently reporting",
# for every per-pole field, including the ones that previously fell back
# to stale last-known values.
_ROLLUP_PERIOD_TYPE = "Last48Hours"

# One row per Project (with its Customer attached), aggregating over
# every Pole belonging to that project and each pole's own Last48Hours
# PoleVitals row.
#
# Population/rollup design (replaces the earlier LightStatus-based
# workingPercentage/optimisticWorkingPercentage/totalNonTelemetryAvailable
# entirely):
#   totalLights (population) = EVERY pole belonging to the project,
#     full stop -- no IsOnline/IsOpenIssueFault filtering at all. This
#     was previously a narrower definition (IsOnline poles, PLUS poles
#     that are NOT online but DO have an open issue -- a pole neither
#     online nor known to have an issue was excluded from the
#     population entirely); changed to simply mean "every pole", by
#     explicit request.
#   connectedLights = poles that are IsOnline. Unchanged by the above --
#     no longer a strict subset of totalLights by construction the way
#     it used to be (a pole with IsOnline=0 no longer implies it's
#     excluded from totalLights the way it once did, since totalLights
#     doesn't exclude anything anymore), though every IsOnline pole is
#     still, naturally, also counted in totalLights.
#   totalFaults = poles satisfying the OLD population definition above
#     (IsOnline OR IsOpenIssueFault) whose IsPoleFault is also true --
#     DELIBERATELY NOT updated to match totalLights' own new, broader
#     "every pole" scope, by explicit request. This means totalLights
#     and totalFaults are now computed over two DIFFERENT populations,
#     not one shared one -- worth being explicit about, since it's easy
#     to assume otherwise from the variable names alone. A pole that's
#     neither online nor has an open issue can never contribute to
#     totalFaults, regardless of its own IsPoleFault value, exactly as
#     before this change.
#   percentWorking = (connectedLights - totalFaults) / connectedLights *
#     100 -- per explicit correction (an earlier version divided by
#     totalLights instead). Computed in Python (_percent_working()),
#     not SQL, same reasoning as everywhere else numeric rollups are
#     computed here. Basing this on connectedLights rather than
#     totalLights means a pole that isn't even connected no longer
#     drags this percentage down purely for being disconnected -- that
#     fact is already reflected separately, in connectedLights itself.
#     Since totalFaults' own population is scoped differently from
#     connectedLights' own population (see above), total_faults is not
#     strictly guaranteed to be <= connectedLights in every possible
#     case -- see _percent_working()'s own docstring for why this
#     formula doesn't clamp or guard against that.
#
# "IsOnline = 1 OR IsOpenIssueFault = 1" (in TotalFaults' own CASE
# below) needs no explicit NULL-handling: a pole with no Last48Hours row
# at all gets NULL for both columns via the LEFT JOIN below, and
# "NULL = 1" is UNKNOWN (not TRUE) in T-SQL, so it naturally falls
# through to "not counted toward TotalFaults" without an ISNULL() guard.
# TotalLights itself no longer needs any such condition at all -- COUNT(*)
# counts every row in PoleWithStatus regardless of which of its columns
# are NULL, which is exactly "every pole" now.
#
# LEFT JOIN Poles->RecentPoleStats (not INNER): a pole with no
# Last48Hours row yet (installed, but no telemetry processed for it, or
# none recent enough to be in the rolling window) must still be
# considered -- it's now unconditionally counted in totalLights either
# way, and still won't satisfy TotalFaults' own population condition
# unless it has an open issue, same as before.
#
# LEFT JOIN Projects->ProjectAgg (not INNER): a project with zero poles
# must still appear, with every count column at 0, rather than being
# silently dropped from the result entirely.
_FETCH_SQL_TEMPLATE = """
WITH RecentPoleStats AS (
    SELECT PoleId, IsOnline, IsOpenIssueFault, IsPoleFault
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
    LEFT JOIN RecentPoleStats rps ON COALESCE(p.VendorPoleId, p.ProvisionedPoleId) = rps.PoleId
),
ProjectAgg AS (
    SELECT
        ProjectId,
        COUNT(*) AS TotalLights,
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
    ISNULL(pa.TotalFaults, 0) AS TotalFaults,
    proj.LeadsunProject AS LeadsunProject
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
# PoleId (see pole_vitals_loader.py's own _LAST_48_HOURS_MERGE_SQL --
# it matches PoleVitals on PoleId+PeriodType alone, no PeriodStart,
# so there's exactly one row per pole, or none for a silent one), so
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
# PoleTelemetry row -- PoleTelemetry's own PRIMARY KEY is (PoleId,
# LastUpload), so `TOP 1 ... WHERE PoleId = @x ORDER BY LastUpload
# DESC` seeks directly into that one pole's rows rather than scanning
# the table. OUTER, not CROSS: a pole with no PoleId, or zero
# matching PoleTelemetry rows, must still appear (with these columns
# NULL).
#
# LampPower1/LampPower2, BatteryElecCurrent1/BatteryElecCurrent2,
# SolarBoardVoltage/SolarBoardElecCurrent are this same latest reading's
# OWN raw values -- genuinely different from the PoleVitals-based
# avg*Percentage fields below (those are period AGGREGATES computed by
# pole_vitals_loader.py over many readings; these are the single most
# recent reading's own numbers, unaggregated). Added to the SAME OUTER
# APPLY as LastUpload/BatteryVoltage1/BatteryVoltage2 above rather than
# a second one, since it's still exactly one row per pole either way --
# no reason to seek into PoleTelemetry twice for the same row.
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
    p.VendorPoleId AS VendorPoleId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    p.Active AS Active,
    latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
    -- ControllerCode/GroupId/ProductId are device-identifying fields --
    -- fixed properties of a pole's own hardware, not a reading that
    -- changes moment to moment the way BatteryVoltage1 etc. below do,
    -- so they'd be identical across every one of a given pole's
    -- PoleTelemetry rows regardless of which specific row happened to
    -- be latest_pt's own TOP 1. UserName is added alongside them for
    -- the same "sourced from this same latest reading, not a separate
    -- lookup" reason, though unlike the other three it's not confirmed
    -- to be identical across a given pole's own history the same way.
    -- Sourced from that latest reading anyway (not a separate lookup)
    -- purely because that's the only place this project already has a
    -- per-pole seek into PoleTelemetry at all -- there's no dedicated
    -- "pole hardware identity" table/column to read these from instead.
    latest_pt.ControllerCode AS ControllerCode,
    latest_pt.GroupId AS GroupId,
    latest_pt.ProductId AS ProductId,
    latest_pt.UserName AS UserName,
    latest_pt.BatteryVoltage1 AS BatteryVoltage1,
    latest_pt.BatteryVoltage2 AS BatteryVoltage2,
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
    -- Reads directly from PoleOpenIssues, NOT rps -- deliberately
    -- decoupled from the Last48Hours join (and therefore from telemetry
    -- recency entirely), per explicit request: whether a pole has an
    -- open issue is a current fact about the POLE, unrelated to whether
    -- it's reported telemetry in the last 48 hours. Always TRUE or
    -- FALSE, never NULL, regardless of whether rps itself has a
    -- matching row -- unlike every other field on this line, which
    -- comes back NULL for a pole with no current Last48Hours row.
    CASE WHEN EXISTS (
        SELECT 1 FROM PoleOpenIssues poi WHERE poi.PoleId = p.Id
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
    SELECT TOP 1
        pt.LastUpload, pt.ControllerCode, pt.GroupId, pt.ProductId, pt.UserName,
        pt.BatteryVoltage1, pt.BatteryVoltage2,
        pt.LampPower1, pt.LampPower2,
        pt.BatteryElecCurrent1, pt.BatteryElecCurrent2,
        pt.SolarBoardVoltage, pt.SolarBoardElecCurrent, pt.IsDaylightForPanelFault
    FROM PoleTelemetry pt
    WHERE pt.PoleId = COALESCE(p.VendorPoleId, p.ProvisionedPoleId)
    ORDER BY pt.LastUpload DESC
) AS latest_pt
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""


def _percent_working(total_connected: int, total_faults: int) -> float:
    """
    0 when total_connected is 0 (nothing to be a percentage OF), not a
    divide-by-zero error and not None -- a plain 0.0 is a safer default
    for a numeric field a consuming website will likely render directly
    (e.g. into a progress bar) than a null it may not expect.

    Based on CONNECTED lights, not total lights, per explicit
    correction (an earlier version divided by totalLights instead) --
    this now answers "of the poles actually connected right now, what
    fraction are fault-free", not "of every pole regardless of
    connectivity, including ones not even reporting". A pole that isn't
    connected at all no longer drags this percentage down just for
    being disconnected -- that's already reflected separately, in
    connectedLights itself.

    Worth knowing: totalFaults' own population is scoped differently
    from connectedLights' own population (a pole can be counted as
    "faulted" under different criteria than "connected" -- see
    totalLights' own history of this exact kind of population
    mismatch), so total_faults is not strictly guaranteed to be <=
    total_connected in every possible case. This formula doesn't clamp
    or guard against that -- a genuine mismatch would surface as a
    percentWorking outside the usual 0-100 range, which is preferable
    to silently hiding a real data inconsistency behind a clamped value.
    """
    if total_connected == 0:
        return 0.0
    return round(((total_connected - total_faults) / total_connected) * 100, 2)


def _pole_row_to_dict(row) -> dict:
    """Converts one row from _POLE_DETAILS_SQL_TEMPLATE into its own
    dict for a project's "poles" list.

    isOnline/isLedFault/isBatteryFault/isPanelFault/isOpenIssueFault/
    isPoleFault, and the three avg*Percentage fields, all come from that
    pole's own CURRENT Last48Hours PoleVitals row (_ROLLUP_PERIOD_TYPE)
    via a single LEFT JOIN -- None (JSON null) for EVERY one of these
    fields when a pole has no current Last48Hours row, whether that's
    because it's genuinely gone silent (no telemetry within the rolling
    48-hour window) or because it's never reported at all. A separate
    period type (LastKnown48Hours) used to persist a silent pole's
    last-known values here instead of null; it was removed entirely by
    explicit request/correction -- null is now the correct, intended
    signal for "not currently reporting", uniformly across every one of
    these fields (isOnline included -- it was already null-for-silent
    even before that removal, since it always read this same
    _ROLLUP_PERIOD_TYPE join; the other fields now match that same
    behavior instead of falling back to stale data).

    installDate/lat/long come straight from Poles -- static install-time
    facts, not derived from any telemetry or vitals aggregation.

    lastUpdate/controllerCode/groupId/productId/userName/batteryVoltage1/
    batteryVoltage2/lampPower1/lampPower2/batteryElecCurrent1/
    batteryElecCurrent2/solarBoardVoltage/solarBoardElecCurrent come from
    that pole's single most recent PoleTelemetry row (via the OUTER
    APPLY in _POLE_DETAILS_SQL_TEMPLATE) -- the raw reading itself,
    genuinely different from avgBatteryPercentage/avgPanelPercentage/
    avgLightPercentage below (PoleVitals' own period AGGREGATES over
    many readings, not this single most recent one). lastUpdate reflects
    the POLE'S OWN local time (via PoleTimeZones), not UTC -- see
    _POLE_DETAILS_SQL_TEMPLATE's own comment. controllerCode/groupId/
    productId are device-identifying fields, not per-reading sensor
    values -- fixed properties of a pole's own hardware that would be
    identical across every one of its PoleTelemetry rows, sourced from
    this same latest reading only because that's the only existing
    per-pole seek into PoleTelemetry, not a separate lookup. userName is
    included for the same "sourced from this same latest reading"
    reason, though it's not confirmed to be identical across a given
    pole's own history the same way those three are. All of these are
    None for a pole with no PoleId or no matching PoleTelemetry rows
    at all.

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
        vendor_pole_id,
        install_date,
        lat,
        long_,
        active,
        last_update,
        controller_code,
        group_id,
        product_id,
        user_name,
        battery_voltage_1,
        battery_voltage_2,
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
        _,  # CustomerId -- see docstring above for why this is discarded here
    ) = row

    # Per explicit request: these four fault-related fields go null
    # whenever lastUpdate is null OR more than 48 hours old -- a fault
    # reading tied to telemetry that old (or nonexistent) isn't
    # trustworthy as a CURRENT fault state, same reasoning as
    # overallStatusLabel/lightStatusLabel/etc.'s own staleness override
    # below. Two fields are deliberately EXCLUDED from this override:
    #   isOnline      -- wasn't named in the request, and connectedLabel's
    #                    own logic already reads is_online directly
    #                    regardless of staleness (see
    #                    api_utils.compute_pole_connectivity_labels()).
    #   isOpenIssueFault -- sourced directly from PoleOpenIssues via its
    #                    own independent EXISTS check in the SQL query
    #                    itself (see _POLE_DETAILS_SQL_TEMPLATE's own
    #                    comment), not from the rps/Last48Hours join at
    #                    all -- always True or False, NEVER null,
    #                    regardless of whether this pole even has a
    #                    current Last48Hours row. Per a later, separate
    #                    explicit request: whether a pole has an open
    #                    issue is a current fact about the POLE, entirely
    #                    unrelated to whether it's reported telemetry in
    #                    the last 48 hours -- so this field doesn't
    #                    merely skip the staleness override below, it's
    #                    structurally incapable of being affected by it.
    #
    # Per a SEPARATE, later explicit request: the raw PoleTelemetry
    # "single most recent reading" fields (batteryVoltage1/2, lampPower1/2,
    # batteryElecCurrent1/2, solarBoardVoltage/solarBoardElecCurrent) ALSO
    # go null under this same condition now -- these come from a
    # genuinely different source than the four fault fields above (the
    # OUTER APPLY's own TOP 1 PoleTelemetry row, not a PoleVitals rollup
    # row), and or a long-silent pole that reading can be weeks old,
    # making its own literal sensor values (e.g. a real "0.0" lamp power
    # reading from three weeks ago) look deceptively like a CURRENT
    # status rather than ancient history -- exactly the kind of
    # misleading signal the staleness overrides elsewhere in this
    # function exist to prevent. lastUpdate itself is deliberately NOT
    # nulled -- it's the one field a caller actually needs in order to
    # know how stale (or utterly absent) this pole's last reading was;
    # nulling it too would remove the only piece of information that
    # explains why everything else came back null.
    is_stale = compute_reporting_staleness_label(last_update) is not None
    if is_stale:
        is_led_fault = None
        is_battery_fault = None
        is_panel_fault = None
        is_pole_fault = None
        battery_voltage_1 = None
        battery_voltage_2 = None
        lamp_power_1 = None
        lamp_power_2 = None
        battery_elec_current_1 = None
        battery_elec_current_2 = None
        solar_board_voltage = None
        solar_board_elec_current = None

    return {
        "id": json_safe(pole_id),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(vendor_pole_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
        "active": json_safe(active),
        "lastUpdate": json_safe(last_update),
        "controllerCode": json_safe(controller_code),
        "groupId": json_safe(group_id),
        "productId": json_safe(product_id),
        "userName": json_safe(user_name),
        "batteryVoltage1": json_safe(battery_voltage_1),
        "batteryVoltage2": json_safe(battery_voltage_2),
        "lampPower1": json_safe(lamp_power_1),
        "lampPower2": json_safe(lamp_power_2),
        "batteryElecCurrent1": json_safe(battery_elec_current_1),
        "batteryElecCurrent2": json_safe(battery_elec_current_2),
        "solarBoardVoltage": json_safe(solar_board_voltage),
        "solarBoardElecCurrent": json_safe(solar_board_elec_current),
        "isOnline": json_safe(is_online),
        "isLedFault": json_safe(is_led_fault),
        "isBatteryFault": json_safe(is_battery_fault),
        "isPanelFault": json_safe(is_panel_fault),
        "isOpenIssueFault": json_safe(is_open_issue_fault),
        "isPoleFault": json_safe(is_pole_fault),
        "avgBatteryPercentage": json_safe(battery_percentage),
        "avgPanelPercentage": json_safe(panel_percentage),
        "avgLightPercentage": json_safe(light_percentage),
        "sunsetTime": json_safe(
            compute_pole_local_sunset(timezone_latitude, timezone_longitude, iana_timezone)
        ),
        **_compute_pole_vitals_status_fields(
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
        ),
    }


def _compute_pole_vitals_status_fields(
    last_update,
    is_online,
    is_pole_fault,
    lamp_power_1,
    lamp_power_2,
    battery_elec_current_1,
    battery_elec_current_2,
    solar_board_voltage,
    solar_board_elec_current,
    is_daylight_for_panel_fault,
) -> dict:
    """
    getPoleVitals-specific layer on top of api_utils.compute_pole_status_
    labels()'s own five fields, per explicit request -- adds
    connectedLabel/overallStatusLabel (see
    api_utils.compute_pole_connectivity_labels()), and overrides
    lightStatusLabel/panelStatusLabel/batteryStatusLabel specifically to
    api_utils.compute_reporting_staleness_label()'s own "Not
    Reporting"/"Not Reporting 48H" whenever that applies -- taking
    priority over whatever compute_pole_status_labels() itself computed
    from this pole's own (possibly stale) telemetry values.

    panelIdleReason is NOT part of this override -- left exactly as
    compute_pole_status_labels() itself returns it (still None when
    there's no telemetry at all; still computed from the actual stale
    reading's own values when telemetry exists but is >48h old), since
    it wasn't named in either the request that introduced this staleness
    override or the later one that added electricCurrentAverage/the raw
    sensor fields to it.

    electricCurrentAverage IS now part of this override (added per a
    later, separate explicit request) -- forced to None whenever
    staleness_label applies, rather than trusting whatever
    compute_pole_status_labels() itself computed. This can't be left to
    that function alone: by the time this is called, _pole_row_to_dict()
    has already nulled the raw battery_elec_current_1/2 inputs for a
    stale pole (see that function's own comment), and
    compute_pole_status_labels() treats a null INDIVIDUAL reading as
    zero for this average (not as "skip it") -- so passing (None, None)
    would silently produce 0.0, not None, without this explicit
    override.
    """
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

    staleness_label = compute_reporting_staleness_label(last_update)
    if staleness_label:
        status_labels["lightStatusLabel"] = staleness_label
        status_labels["panelStatusLabel"] = staleness_label
        status_labels["batteryStatusLabel"] = staleness_label
        status_labels["electricCurrentAverage"] = None

    return {
        **status_labels,
        **compute_pole_connectivity_labels(
            is_online=is_online, is_pole_fault=is_pole_fault, last_update=last_update
        ),
    }


def _row_to_project_dict(row, poles: list) -> dict:
    _, _, project_id, project_name, total_lights, connected_lights, total_faults, leadsun_project = row
    return {
        "id": json_safe(project_id),
        "name": json_safe(project_name),
        "totalLights": json_safe(total_lights),
        "connectedLights": json_safe(connected_lights),
        "totalFaults": json_safe(total_faults),
        "percentWorking": _percent_working(connected_lights, total_faults),
        "leadsunProject": json_safe(leadsun_project),
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
        "percentWorking": _percent_working(connected_lights, total_faults),
    }


def get_pole_vitals(customer_id: str = None, project_id: str = None, limit: int = None):
    """
    Returns each Customer's Projects, each annotated with pole-health
    rollup stats (totalLights, connectedLights, totalFaults,
    percentWorking -- computed from each pole's own Last48Hours
    PoleVitals row, _ROLLUP_PERIOD_TYPE; see that constant's own comment
    for why a silent pole is deliberately NOT counted as currently
    connected here, even though its per-pole fields below still show its
    last-known state), leadsunProject (that Project's own LeadsunProject
    column, passed through AS-IS -- a JSON-encoded STRING, not parsed
    into a nested object, same convention as poleNumbers/poleIds/
    installDates above; always at least {"ProjectId": ...} once Airtable
    has provided one, further enriched with ProjectName/UserName/groups/
    products by pole_telemetry_loader.update_leadsun_project_details()
    once matching PoleTelemetry data exists -- see that function's own
    docstring for the full shape; None for a project Airtable hasn't
    recorded a Leadsun ProjectID for at all yet), and a "poles" list (one entry per
    Pole belonging to that project: id, poleNumber, locationId,
    installDate, lat, long, lastUpdate, controllerCode, groupId,
    productId, userName, batteryVoltage1, batteryVoltage2, lampPower1,
    lampPower2, batteryElecCurrent1, batteryElecCurrent2,
    solarBoardVoltage, solarBoardElecCurrent, isOnline, isLedFault,
    isBatteryFault, isPanelFault, isOpenIssueFault, isPoleFault,
    avgBatteryPercentage, avgPanelPercentage, avgLightPercentage,
    lightStatusLabel, panelStatusLabel, panelIdleReason,
    batteryStatusLabel, electricCurrentAverage (these five calculated
    via api_utils.compute_pole_status_labels() -- see that function's
    own docstring for the full logic), and sunsetTime (today's sunset
    for THIS pole's own location, expressed in that same pole's own
    local time -- via api_utils.compute_pole_local_sunset(), which
    itself relies on PoleTimeZones' own Latitude/Longitude/
    IanaTimeZone; None for a pole with no resolved coordinates, or one
    whose location has no sunset at all on its own current local date,
    e.g. polar day/night at extreme Alaska latitudes -- see that
    function's and shared/daylight_utils.get_sunset()'s own docstrings)
    --
    isLedFault/isBatteryFault/isPanelFault/isOpenIssueFault/isPoleFault,
    isOnline, and the three avg*Percentage fields all come from that
    pole's own CURRENT Last48Hours PoleVitals row (_ROLLUP_PERIOD_TYPE)
    -- the SAME period type driving the rollup stats above. A silent
    pole (no current Last48Hours row) gets NULL for every one of these
    fields, uniformly -- a separate period type (LastKnown48Hours) used
    to persist a silent pole's last-known values instead of null; it was
    removed entirely by explicit request/correction, so null is now the
    correct, intended signal here, consistent with that same pole
    already being excluded from totalLights/connectedLights above for
    the same underlying reason (see _ROLLUP_PERIOD_TYPE's own comment).
    A single, continuously-updated row per pole -- see
    pole_vitals_loader.py's own module docstring for why this period
    type is structured that way; no window-aggregation happens at this
    API layer at all anymore.

    Rollup design: totalLights counts EVERY pole belonging to the
    project, full stop -- no IsOnline/IsOpenIssueFault filtering.
    connectedLights is just the IsOnline poles. totalFaults is
    DELIBERATELY still scoped to the OLD, narrower population (IsOnline
    poles, plus poles that aren't online but DO have an open issue) --
    not updated to match totalLights' own broader "every pole" scope, by
    explicit request, so the two are now computed over different
    populations. percentWorking is (connectedLights - totalFaults) /
    connectedLights * 100, per explicit correction (an earlier version
    divided by totalLights instead). See _FETCH_SQL_TEMPLATE's own
    comment for the full reasoning, including the practical consequence
    of totalLights and totalFaults no longer sharing one population.

    installDate/lat/long come straight from Poles -- static, unrelated to
    any telemetry or vitals data (present even for a pole with neither).
    lastUpdate/batteryVoltage1/batteryVoltage2/lampPower1/lampPower2/
    batteryElecCurrent1/batteryElecCurrent2/solarBoardVoltage/
    solarBoardElecCurrent come from that pole's own single most recent
    PoleTelemetry row (an OUTER APPLY, not the PoleVitals-based fields
    above) -- the raw reading itself, distinct from
    avgBatteryPercentage/avgPanelPercentage/avgLightPercentage
    (PoleVitals' own period AGGREGATES over many readings, not this
    single most recent one). lastUpdate reflects the pole's own local
    time zone (via PoleTimeZones), not UTC. All of these are None for a
    pole with no PoleId or no matching PoleTelemetry rows at all.
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
        params = (_ROLLUP_PERIOD_TYPE, project_id, customer_id)
    elif project_id:
        where_clause = "WHERE proj.Id = ?"
        params = (_ROLLUP_PERIOD_TYPE, project_id)
    elif customer_id:
        where_clause = "WHERE c.Id = ?"
        params = (_ROLLUP_PERIOD_TYPE, customer_id)
    else:
        # limit applies to CUSTOMERS, the top-level entity here -- can't
        # TOP() the raw query directly (that would truncate PROJECT rows,
        # silently dropping some of one customer's projects rather than
        # dropping whole customers), so this filters to the first N
        # distinct customer Ids first via a subquery, then fetches every
        # project row for those.
        where_clause = "WHERE c.Id IN (SELECT TOP (?) Id FROM Customers ORDER BY Name)"
        params = (_ROLLUP_PERIOD_TYPE, clamp_limit(limit))

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            _FETCH_SQL_TEMPLATE.format(where_clause=where_clause),
            *params,
        )
        rows = cursor.fetchall()

        # Same where_clause AND same params as the rollup query above --
        # both queries now read the SAME single PoleVitals period type
        # (_ROLLUP_PERIOD_TYPE), so there's no longer a separate
        # pole_detail_params to bind here. See _POLE_DETAILS_SQL_TEMPLATE's
        # own comment for why this remains a separate query rather than
        # merged into the one above.
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
# get_pole_vitals() above: that one reads each pole's own current-state
# Last48Hours PoleVitals row directly (_ROLLUP_PERIOD_TYPE, used for
# both the rollup stats and every per-pole detail field alike -- see
# that constant's own comment for why -- no window/aggregation at all,
# since it's already a single row per pole, or none for a silent one).
# This one returns a pole's FULL HISTORY of PoleVitals rows for a
# CALLER-CHOSEN period type -- Hour is the ONLY one this endpoint still
# supports (Day was removed from PoleVitals entirely by explicit
# request; Last48Hours was never valid here to begin with -- see below),
# each read directly, exactly as stored.

# Valid PoleVitals period types for THIS function specifically --
# 'Hour' is genuinely the only one now: Last48Hours is deliberately
# excluded (a single current-state row, not a history to page through,
# so "give me its history" doesn't apply to it), and 'Day' was removed
# from PoleVitals altogether. This tuple is still checked explicitly,
# rather than assuming period_type is
# always 'Hour', so a caller passing anything else (a typo, or a
# removed/never-valid value like 'Day' or 'Week') still gets a clear
# ValueError instead of silently querying the wrong thing.
_VALID_PERIOD_TYPES = ("Hour",)

# A pole's static facts -- id, poleNumber, locationId, installDate, lat,
# long, lastUpdate -- are properties of the POLE, not of any individual
# PoleVitals bucket, so they're fetched once here rather than repeated
# on every history entry (which would be wasteful once this can return
# many rows). lastUpdate reflects the pole's own local time zone (via
# PoleTimeZones), same as get_pole_vitals()'s per-pole lastUpdate -- not
# UTC. OUTER, not CROSS: a pole with no PoleTelemetry row yet must still
# be returned (with lastUpdate and the other latest-telemetry fields
# null).
#
# LampPower1/LampPower2, BatteryElecCurrent1/BatteryElecCurrent2,
# SolarBoardVoltage/SolarBoardElecCurrent added to this same OUTER APPLY
# for the same reason as get_pole_vitals()'s own _POLE_DETAILS_SQL_TEMPLATE
# -- one seek into PoleTelemetry already returns this same latest row, no
# reason for a second one. (BatteryVoltage1/BatteryVoltage2 remain
# deliberately excluded from THIS endpoint specifically, per an earlier,
# separate explicit request -- unrelated to this addition, not
# reconsidered here.)
_POLE_INFO_FOR_HISTORY_SQL_TEMPLATE = """
SELECT
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.VendorPoleId AS VendorPoleId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
    latest_pt.LampPower1 AS LampPower1,
    latest_pt.LampPower2 AS LampPower2,
    latest_pt.BatteryElecCurrent1 AS BatteryElecCurrent1,
    latest_pt.BatteryElecCurrent2 AS BatteryElecCurrent2,
    latest_pt.SolarBoardVoltage AS SolarBoardVoltage,
    latest_pt.SolarBoardElecCurrent AS SolarBoardElecCurrent
FROM Poles p
LEFT JOIN PoleTimeZones ptz ON
    (p.VendorPoleId IS NOT NULL AND p.VendorPoleId = ptz.VendorPoleId)
    OR (p.VendorPoleId IS NULL AND p.ProvisionedPoleId IS NOT NULL AND p.ProvisionedPoleId = ptz.ProvisionedPoleId)
OUTER APPLY (
    SELECT TOP 1
        pt.LastUpload, pt.LampPower1, pt.LampPower2,
        pt.BatteryElecCurrent1, pt.BatteryElecCurrent2,
        pt.SolarBoardVoltage, pt.SolarBoardElecCurrent
    FROM PoleTelemetry pt
    WHERE pt.PoleId = COALESCE(p.VendorPoleId, p.ProvisionedPoleId)
    ORDER BY pt.LastUpload DESC
) AS latest_pt
WHERE p.Id = ?
"""

# The Hour-specific variant of the query below -- generates a FULL,
# CONTIGUOUS sequence of `limit` hourly buckets ending at the CURRENT
# hour (SYSDATETIMEOFFSET(), truncated down to its own top-of-hour, in
# THIS POLE'S OWN local timezone -- matching exactly how
# pole_vitals_loader.py's own Hour rollup truncates BucketStart), and
# LEFT JOINs PoleVitals onto that generated sequence rather than
# selecting PoleVitals rows directly. Per explicit request/correction:
# an earlier version of this anchored the window to THIS POLE'S OWN most
# recent PoleTelemetry reading instead of "now", specifically so a pole
# that had gone silent would still show its own last known activity
# rather than an empty list. That reasoning is now explicitly reversed:
# a pole that's gone silent should show REAL gaps -- an actual missing
# hour, with PeriodStart/PeriodEnd populated and every average/fault
# field null -- not a window quietly anchored to whenever it last
# happened to report. "Truly from now" per that request.
#
# Numbers is a classic recursive tally-number generator (0, 1, 2, ...,
# limit-1) -- OPTION (MAXRECURSION 0) is required since limit can reach
# MAX_LIMIT (1000), well past SQL Server's default 100-level recursion
# cap.
#
# Buckets cross-joins those offsets onto PoleContext's own
# CurrentBucketStart to produce exactly `limit` distinct BucketStart
# values, each `limit` counted backward from the current hour -- one row
# per hour, guaranteed contiguous by construction (not dependent on
# PoleVitals actually having a row there).
#
# The final SELECT LEFT JOINs PoleVitals onto those generated buckets
# (matched on PoleId + PeriodType='Hour' + PeriodStart) instead of
# the reverse (selecting PoleVitals and filtering) -- this is what makes
# a genuinely missing hour still produce its own row, with every
# PoleVitals-sourced column coming back NULL via the LEFT JOIN's own
# standard NULL-for-no-match behavior, rather than being silently
# absent from the result set entirely.
#
# PeriodStart/PeriodEnd on the OUTPUT side are computed from the
# generated BucketStart (via the same "AT TIME ZONE TimeZoneName"
# conversion the loader itself uses), NOT read from pv.PeriodStart/
# pv.PeriodEnd -- necessary specifically for the gap case, where
# pv.PeriodStart/pv.PeriodEnd would otherwise come back NULL (no
# PoleVitals row to read them from) even though the bucket's own
# start/end time is perfectly well known from the generated sequence
# itself.
#
# 'Hour' is a hardcoded literal, not a bound parameter, in both the
# BucketStart-truncation logic (matching the loader's own convention)
# and the LEFT JOIN condition -- this template is only ever selected in
# Python when period_type == "Hour", so there's nothing to parameterize.
#
# Day intentionally does NOT get an equivalent generated/gap-filled
# window here (see get_pole_vitals_by_period()'s own docstring) -- it
# keeps using _POLE_VITALS_HISTORY_SQL_TEMPLATE below, a pure row-count
# limit with no time bound or gap-filling at all.
#
# Parameter order (forced by the CTEs coming first, textually, in
# T-SQL): pole_id (for PoleContext's own WHERE p.Id = ?), THEN limit
# ONCE (for the Numbers CTE's own upper bound -- this template no longer
# double-binds limit the way its predecessor did, since there's no
# separate TOP (?) anymore: the Numbers CTE itself is now the only thing
# controlling row count, generating exactly `limit` rows by
# construction).
_POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE = """
WITH PoleContext AS (
    SELECT
        COALESCE(p.VendorPoleId, p.ProvisionedPoleId) AS PoleId,
        ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS TimeZoneName
    FROM Poles p
    LEFT JOIN PoleTimeZones ptz ON
        (p.VendorPoleId IS NOT NULL AND p.VendorPoleId = ptz.VendorPoleId)
        OR (p.VendorPoleId IS NULL AND p.ProvisionedPoleId IS NOT NULL AND p.ProvisionedPoleId = ptz.ProvisionedPoleId)
    WHERE p.Id = ?
),
CurrentBucket AS (
    SELECT
        PoleId,
        TimeZoneName,
        DATEADD(
            HOUR,
            DATEDIFF(
                HOUR, '19000101',
                CAST(SYSDATETIMEOFFSET() AT TIME ZONE TimeZoneName AS DATETIME2(3))
            ),
            '19000101'
        ) AS CurrentBucketStart
    FROM PoleContext
),
Numbers AS (
    SELECT 0 AS n
    UNION ALL
    SELECT n + 1 FROM Numbers WHERE n < ? - 1
),
Buckets AS (
    SELECT
        cb.PoleId,
        cb.TimeZoneName,
        DATEADD(HOUR, -n.n, cb.CurrentBucketStart) AS BucketStart
    FROM CurrentBucket cb
    CROSS JOIN Numbers n
)
SELECT
    b.BucketStart AT TIME ZONE b.TimeZoneName AS PeriodStart,
    DATEADD(HOUR, 1, b.BucketStart) AT TIME ZONE b.TimeZoneName AS PeriodEnd,
    pv.IsOnline AS IsOnline,
    pv.IsLedFault AS IsLedFault,
    pv.IsBatteryFault AS IsBatteryFault,
    pv.IsPanelFault AS IsPanelFault,
    pv.IsOpenIssueFault AS IsOpenIssueFault,
    pv.IsPoleFault AS IsPoleFault,
    pv.AvgBatteryPercentage AS AvgBatteryPercentage,
    pv.AvgPanelPercentage AS AvgPanelPercentage,
    pv.AvgLightPercentage AS AvgLightPercentage
FROM Buckets b
LEFT JOIN PoleVitals pv
    ON pv.PoleId = b.PoleId
   AND pv.PeriodType = 'Hour'
   AND pv.PeriodStart = (b.BucketStart AT TIME ZONE b.TimeZoneName)
ORDER BY b.BucketStart DESC
OPTION (MAXRECURSION 0)
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
    installDate, lat, long, lastUpdate, lampPower1, lampPower2,
    batteryElecCurrent1, batteryElecCurrent2, solarBoardVoltage,
    solarBoardElecCurrent -- the last six from that pole's single most
    recent PoleTelemetry row, same as get_pole_vitals()'s own per-pole
    fields -- see _POLE_INFO_FOR_HISTORY_SQL_TEMPLATE's own comment) plus its full history of PoleVitals rows for the given
    period_type, each entry as its own dict in a "vitals" list
    (periodStart, periodEnd, isOnline, isLedFault, isBatteryFault,
    isPanelFault, isOpenIssueFault, isPoleFault, avgBatteryPercentage,
    avgPanelPercentage, avgLightPercentage). Deliberately NO rollup/
    aggregation across entries -- each one is a direct read of one
    PoleVitals row.

    period_type: must be 'Hour' -- the only period type this endpoint
    still supports (see _VALID_PERIOD_TYPES' own comment: 'Day' was
    removed from PoleVitals entirely by explicit request, and
    Last48Hours was never valid here to begin with, since it's a single
    current-state row, not a history to page through). Raises
    ValueError for anything else; the HTTP layer maps that to a 400.

    limit: max number of hourly buckets returned, most-recent-first,
    always exactly `limit` entries -- one per hour, contiguous, counting
    back from the CURRENT hour (see _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE's
    own comment for the truncation details). Defaults to DEFAULT_LIMIT,
    capped at MAX_LIMIT (see shared/api_utils.py).

    That window is anchored to the CURRENT MOMENT (SYSDATETIMEOFFSET()),
    NOT to this pole's own latest telemetry -- per explicit request/
    correction, reversing an earlier version of this endpoint that
    anchored to the pole's own last reading specifically so a silent
    pole would still show its last known activity. Now, a genuinely
    missing hour -- no PoleVitals row at all for that bucket, whether
    because the pole went offline, was installed partway through that
    hour, or simply hasn't been processed yet -- still gets its OWN
    entry in "vitals", with periodStart/periodEnd populated (computed
    from the generated bucket sequence itself, not read from a
    nonexistent PoleVitals row) and every other field (isOnline,
    isLedFault, isBatteryFault, isPanelFault, isOpenIssueFault,
    isPoleFault, avgBatteryPercentage, avgPanelPercentage,
    avgLightPercentage) null. "vitals" is therefore always exactly
    `limit` entries long -- never fewer, unlike this endpoint's earlier
    behavior of silently omitting gap hours entirely.

    Returns None if no Pole exists with that id. If the pole exists but
    has no PoleTelemetry row yet, lastUpdate and the other
    latest-telemetry fields (lampPower1/2, batteryElecCurrent1/2,
    solarBoardVoltage, solarBoardElecCurrent) come back null -- but
    "vitals" still comes back as a full `limit`-length list of
    all-null-except-periodStart/periodEnd entries (this endpoint no
    longer depends on PoleTelemetry existing at all to produce its hour
    sequence, since that sequence is generated from the current moment,
    not from the pole's own telemetry history).
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

        # 'Hour' is the only period type left -- see _VALID_PERIOD_TYPES'
        # own comment. This template generates a full, contiguous,
        # gap-filled sequence of `limit` hourly buckets anchored to the
        # CURRENT moment -- see _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE's
        # own comment for why (a request/correction reversing this
        # endpoint's earlier "anchor to the pole's own last reading"
        # behavior). Only ONE limit parameter now (the Numbers CTE's own
        # upper bound) -- no more double-binding the same value for both
        # a TOP (?) and a separate DATEADD window bound, since the
        # generated bucket sequence itself is what controls row count
        # now, not a filter on top of PoleVitals' own rows.
        hour_limit = clamp_limit(limit)
        cursor.execute(
            _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE,
            pole_id,
            hour_limit,
        )
        vitals_rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    (
        pole_id_,
        pole_number,
        vendor_pole_id,
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
    ) = pole_row
    return {
        "id": json_safe(pole_id_),
        "poleNumber": json_safe(pole_number),
        "locationId": json_safe(vendor_pole_id),
        "installDate": json_safe(install_date),
        "lat": json_safe(lat),
        "long": json_safe(long_),
        "lastUpdate": json_safe(last_update),
        "lampPower1": json_safe(lamp_power_1),
        "lampPower2": json_safe(lamp_power_2),
        "batteryElecCurrent1": json_safe(battery_elec_current_1),
        "batteryElecCurrent2": json_safe(battery_elec_current_2),
        "solarBoardVoltage": json_safe(solar_board_voltage),
        "solarBoardElecCurrent": json_safe(solar_board_elec_current),
        "vitals": [_pole_vitals_history_row_to_dict(row) for row in vitals_rows],
    }
