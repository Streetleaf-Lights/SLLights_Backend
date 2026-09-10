"""
Shared utilities for the read-only query API endpoints (getCustomers,
getProjects, and any future get<Table>() following the same pattern) --
factored out here once a second endpoint needed the exact same logic,
rather than duplicated per-module with the risk of the copies drifting
apart later.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from shared.daylight_utils import get_sunset
from shared.datetime_utils import parse_dto_string

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


def parse_bool_param(value: str, param_name: str) -> bool:
    """
    Parses a query-string boolean parameter ("true"/"false"/"1"/"0",
    case-insensitive -- same values getPoles' existing `summary` param
    already accepts, just factored out here so `active` doesn't duplicate
    that logic inline a third time across getCustomers/getProjects/
    getPoles).

    Raises ValueError with a message safe to return directly in a 400
    response if `value` doesn't match one of those four accepted forms.
    """
    normalized = value.strip().lower()
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0"):
        return False
    raise ValueError(f"{param_name} must be true/false or 1/0")


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


# How far back "still reporting" reaches before lightStatusLabel/
# panelStatusLabel/batteryStatusLabel/overallStatusLabel all switch to
# their own "Not Reporting 48H" state, per explicit request -- a pole
# whose last known reading is older than this is treated as silent
# going forward, regardless of what that stale reading's own values
# happened to say.
_NOT_REPORTING_STALENESS_THRESHOLD = timedelta(hours=48)


def compute_reporting_staleness_label(last_update) -> str:
    """
    "Not Reporting" if last_update is None (no telemetry at all), "Not
    Reporting 48H" if last_update is older than
    _NOT_REPORTING_STALENESS_THRESHOLD, else None (still reporting
    recently enough that a caller's own normal status logic should
    apply instead of this staleness override).

    last_update is expected to be an aware datetime OR a
    to_dto_string()-shaped string -- this project's DATETIMEOFFSET
    columns usually come back from pyodbc already timezone-aware, but
    the "AT TIME ZONE" computed expression this project's own
    pole-vitals queries use to convert LastUpload into each pole's own
    local offset has been observed coming back as TEXT instead (see
    shared/datetime_utils.parse_dto_string()'s own docstring). Either
    way, comparing the resulting aware datetime against
    datetime.now(timezone.utc) works correctly regardless of what
    OFFSET it carries (e.g. a pole-local offset), since Python compares
    aware datetimes by their absolute instant, not their displayed
    offset.
    """
    last_update = parse_dto_string(last_update)
    if last_update is None:
        return "Not Reporting"
    if (datetime.now(timezone.utc) - last_update) > _NOT_REPORTING_STALENESS_THRESHOLD:
        return "Not Reporting 48H"
    return None


def compute_pole_connectivity_labels(is_online, is_pole_fault, last_update) -> dict:
    """
    Two more presentation-oriented derived fields, layered ALONGSIDE (not
    replacing) compute_pole_status_labels()'s own five -- built for
    pole_vitals_api.py's getPoleVitals specifically, per explicit
    request. NOT plumbed into poles_api.py's own callers of
    compute_pole_status_labels() -- that function's five-field contract
    is unchanged; these two are computed and added on separately, only
    where requested.

    connectedLabel:
      "Online"       if is_online is True
      "Offline"      if is_online is False
      "Disconnected" if is_online is None AND last_update is not None
                        (telemetry exists, but this specific reading's
                        own IsOnline came back NULL)
      "Unknown"      otherwise (is_online is None AND last_update is
                        also None -- no telemetry at all to judge from)

    overallStatusLabel: compute_reporting_staleness_label(last_update)'s
      own "Not Reporting"/"Not Reporting 48H" takes priority when it
      applies; otherwise "Fault" if is_pole_fault is truthy, else "OK".
    """
    if is_online is True:
        connected_label = "Online"
    elif is_online is False:
        connected_label = "Offline"
    elif last_update is not None:
        connected_label = "Disconnected"
    else:
        connected_label = "Unknown"

    staleness_label = compute_reporting_staleness_label(last_update)
    if staleness_label:
        overall_status_label = staleness_label
    elif is_pole_fault:
        overall_status_label = "Fault"
    else:
        overall_status_label = "OK"

    return {
        "connectedLabel": connected_label,
        "overallStatusLabel": overall_status_label,
    }


# Matches pole_vitals_loader.py's own established fallback for a pole
# whose own timezone couldn't be resolved (see PoleTimeZones' own
# comments on WindowsTimeZone/IanaTimeZone both being NULL in that
# case) -- this project's own default assumption when a pole's real
# location-derived timezone isn't available, not an arbitrary new
# choice made just for this one field.
_DEFAULT_TIMEZONE_NAME = "America/New_York"


def compute_pole_local_sunset(latitude, longitude, iana_timezone):
    """
    Returns today's sunset moment for a pole, expressed in that SAME
    pole's own local time -- "today" meaning the pole's OWN local
    calendar date, not the server's, or any other single shared
    reference point's, own "today". These can genuinely differ: at a
    moment just after midnight UTC, a pole in Hawaii is still living in
    the PREVIOUS calendar day while a pole in Maine has already rolled
    over to the next one -- get_sunset()'s own tzinfo parameter (see its
    own docstring) is what makes datetime.now(tz).date() resolve to
    each pole's own correct day here, not a single shared UTC date
    applied uniformly to every pole regardless of where it actually is.

    Returns None if latitude/longitude are missing entirely (no
    PoleTimeZones row resolved for this pole at all -- see
    shared/timezone_utils.py's own resolve_windows_timezone() for when
    that happens), or if the sun genuinely doesn't set at all on this
    pole's own local date at that location (polar day/night -- see
    daylight_utils.get_sunset()'s own docstring).

    iana_timezone falls back to "America/New_York" if not given/
    resolved for this pole -- matching this project's own established
    Eastern-time fallback elsewhere (see pole_vitals_loader.py's own
    comments on the same fallback for WindowsTimeZone) -- deliberately
    NOT returning None in that case: a pole with unresolved coordinates
    already gets None from the check above; a pole WITH resolved
    coordinates but no successfully-mapped IANA zone (e.g. resolves to
    a real timezone outside timezone_utils.py's deliberately US-scoped
    IANA_TO_WINDOWS mapping) still has a real, computable sunset moment
    -- it's specifically the DISPLAY timezone that's unknown, not
    whether a sunset happened.
    """
    if latitude is None or longitude is None:
        return None

    tz = ZoneInfo(iana_timezone) if iana_timezone else ZoneInfo(_DEFAULT_TIMEZONE_NAME)
    today_in_pole_local_time = datetime.now(tz).date()
    return get_sunset(today_in_pole_local_time, latitude, longitude, tzinfo=tz)
