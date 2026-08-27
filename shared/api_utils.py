"""
Shared utilities for the read-only query API endpoints (getCustomers,
getProjects, and any future get<Table>() following the same pattern) --
factored out here once a second endpoint needed the exact same logic,
rather than duplicated per-module with the risk of the copies drifting
apart later.
"""

MAX_LIMIT = 1000

# No-limit-specified means "everything, up to MAX_LIMIT" -- these are
# business data tables (customers, projects), not high-volume telemetry,
# so an arbitrarily lower default just means silently truncated results
# for anyone who doesn't know to pass ?limit= explicitly. Learned this the
# hard way with getCustomers's original DEFAULT_LIMIT=100 -- fixed once,
# here, so every endpoint built on this module starts from the corrected
# default rather than repeating that mistake.
DEFAULT_LIMIT = MAX_LIMIT


def json_safe(value):
    """
    pyodbc can return types (datetime, Decimal, etc.) that aren't
    natively JSON-serializable via json.dumps(). Converts anything that
    isn't already a safe type to a plain string; passes everything else
    through unchanged.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def clamp_limit(limit) -> int:
    """Keeps limit within [1, MAX_LIMIT], defaulting to DEFAULT_LIMIT for
    None/invalid input -- a caller can't request an unbounded result set
    no matter what they pass."""
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def compute_pole_status_labels(
    has_telemetry: bool,
    lamp_power_1,
    lamp_power_2,
    battery_elec_current_1,
    battery_elec_current_2,
    solar_board_voltage,
    solar_board_elec_current,
    is_daylight_for_panel_fault,
) -> dict:
    """
    Five presentation-oriented derived fields, per explicit request --
    moved here (originally defined only in pole_vitals_api.py) once
    poles_api.py's own summary mode needed the same logic too, same
    reasoning as this whole module's own docstring: two independent
    copies would risk silently drifting apart from each other over
    time. Computed here in Python from raw telemetry values a caller
    already has (lampPower1/2, batteryElecCurrent1/2,
    solarBoardVoltage/solarBoardElecCurrent, IsDaylightForPanelFault),
    not a new database round-trip or aggregation of their own.

    has_telemetry gates all five together -- True/False, not inferred
    separately per field from whichever specific inputs each one
    happens to use. In both of this function's own callers, every one
    of these inputs comes from the exact same per-pole OUTER APPLY row,
    so they're already either all NULL together (no PoleTelemetry row
    for this pole at all) or all populated together -- has_telemetry
    should be passed as `last_update is not None`, the same "does this
    pole have ANY telemetry" signal both callers already establish for
    their own other latest-reading fields. All five come back None
    together when False -- a definite "ON"/"OFF"/etc. label would
    misleadingly claim to know a state this pole's own data can't
    actually support.

    Within a genuinely-telemetry-having pole, an individual reading
    (e.g. just lampPower2) could still itself be NULL -- treated as 0
    for these sums, matching this project's own established
    ISNULL(x,0)+ISNULL(y,0) convention for these exact same
    LampPower/BatteryElecCurrent pairs elsewhere (e.g.
    pole_vitals_loader.py's own IsPanelFault formula).

    lightStatusLabel: "ON" if LampPower1+LampPower2 > 0, else "OFF".

    panelStatusLabel: "Charging" if SolarBoardVoltage *
    SolarBoardElecCurrent > 0, else "Idle".

    panelIdleReason: only computed when panelStatusLabel is actually
    "Idle" -- None otherwise (including for "Charging"), per explicit
    correction (an earlier version computed this unconditionally,
    alongside panelStatusLabel rather than gated by it). "Sundown" if
    IsDaylightForPanelFault = 0 (using pole_vitals_loader.py's own
    established daylight signal for panel-fault purposes, not a
    separate day/night calculation); else "Battery Full" if
    BatteryElecCurrent1+BatteryElecCurrent2 = 200 (this project's own
    established "battery fully charged" threshold -- see pole_vitals_
    loader.py's own IsPanelFault formula, the same exact condition);
    else "N/A". A NULL IsDaylightForPanelFault (possible even on an
    otherwise-telemetry-having pole, e.g. before pole_daylight_flags_
    loader.py has ever processed it) falls through to the
    battery-current check, same as an explicit non-zero value would --
    NULL is not equal to 0.

    batteryStatusLabel: "Full" if BatteryElecCurrent1+
    BatteryElecCurrent2 = 200 (same threshold as panelIdleReason's own
    "Battery Full" case); else "Discharging" if LampPower1+LampPower2 >
    0 (drawing from the battery to power the lamp); else "Charging".

    electricCurrentAverage: (BatteryElecCurrent1+BatteryElecCurrent2) /
    2 -- the average of the two battery current readings, not rounded.
    poles_api.py's own summary mode deliberately drops this one
    specific key from its own output (per explicit request) after
    calling this same function -- rather than this function itself
    ever returning four keys instead of five for some callers, keeping
    this one function's own contract simple and total, with "which of
    the five to actually expose" left entirely up to each caller.
    """
    if not has_telemetry:
        return {
            "lightStatusLabel": None,
            "panelStatusLabel": None,
            "panelIdleReason": None,
            "batteryStatusLabel": None,
            "electricCurrentAverage": None,
        }

    lamp_power_sum = (lamp_power_1 or 0) + (lamp_power_2 or 0)
    panel_power = (solar_board_voltage or 0) * (solar_board_elec_current or 0)
    battery_current_sum = (battery_elec_current_1 or 0) + (battery_elec_current_2 or 0)

    light_status_label = "ON" if lamp_power_sum > 0 else "OFF"
    panel_status_label = "Charging" if panel_power > 0 else "Idle"

    panel_idle_reason = None
    if panel_status_label == "Idle":
        if is_daylight_for_panel_fault == 0:
            panel_idle_reason = "Sundown"
        elif battery_current_sum == 200:
            panel_idle_reason = "Battery Full"
        else:
            panel_idle_reason = "N/A"

    if battery_current_sum == 200:
        battery_status_label = "Full"
    elif lamp_power_sum > 0:
        battery_status_label = "Discharging"
    else:
        battery_status_label = "Charging"

    return {
        "lightStatusLabel": light_status_label,
        "panelStatusLabel": panel_status_label,
        "panelIdleReason": panel_idle_reason,
        "batteryStatusLabel": battery_status_label,
        "electricCurrentAverage": battery_current_sum / 2,
    }
