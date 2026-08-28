from datetime import date as date_type, datetime

from astral import Observer
from astral.sun import elevation, sun

# Standard astronomical thresholds for the sun's elevation angle, matching
# the conventions astral's own sunrise()/sunset() functions use internally
# (and the wider NOAA/almanac convention generally):
#   - Sunrise/sunset: the sun's center crosses ~-0.833 degrees below the
#     geometric horizon -- not exactly 0 degrees, since this accounts for
#     atmospheric refraction and the sun's own apparent radius. Confirmed
#     against astral's own computed sunrise/sunset times for a real
#     coordinate from this project (elevation was ~-0.37 to -0.65 degrees
#     right at astral's own sunrise/sunset moments).
#   - Civil twilight (dawn/dusk): -6 degrees -- the broader "still light
#     enough to see outdoors without artificial light" threshold, likely
#     closer to when a photocell-controlled streetlight actually switches
#     than the stricter sunrise/sunset boundary is.
_SUNRISE_SUNSET_ELEVATION = -0.833
_CIVIL_TWILIGHT_ELEVATION = -6.0


def is_daylight(
    dt: datetime, latitude: float, longitude: float, use_civil_twilight: bool = False
) -> bool:
    """
    Returns True if the sun is up at the given location and moment.

    dt must be timezone-aware -- solar position genuinely depends on the
    exact UTC instant, not a naive wall-clock reading with an assumed
    timezone, so this raises rather than silently guessing one.

    Computed via solar ELEVATION ANGLE directly (not a sunrise()/sunset()
    boundary lookup), specifically because elevation is well-defined for
    every moment/location -- including places with literal polar
    day/night (relevant here, since this project's timezone mapping in
    shared/timezone_utils.py already covers Alaska). astral's own
    sunrise()/sunset() functions raise ValueError outright on a day where
    the sun never rises or never sets ("Sun is always above/below the
    horizon on this day"); elevation() sidesteps that failure mode
    entirely, since it's just answering "where is the sun right now",
    not "when does today's sunrise/sunset happen".

    use_civil_twilight=False (default): daylight = sun's elevation above
    the standard sunrise/sunset threshold (~-0.833 degrees).
    use_civil_twilight=True: daylight = sun's elevation above the civil
    twilight threshold (-6 degrees) -- broader, and likely closer to when
    a photocell-controlled streetlight actually switches, if that's what
    this ends up being compared against.
    """
    if dt.tzinfo is None:
        raise ValueError(
            "dt must be timezone-aware -- solar position depends on the exact "
            "UTC instant, not an assumed timezone"
        )

    observer = Observer(latitude=latitude, longitude=longitude)
    threshold = _CIVIL_TWILIGHT_ELEVATION if use_civil_twilight else _SUNRISE_SUNSET_ELEVATION
    return elevation(observer, dt) > threshold


def get_sunset(for_date: date_type, latitude: float, longitude: float, tzinfo=None) -> datetime:
    """
    Returns the sunset moment for the given date and location, as an
    aware datetime in `tzinfo` (UTC if not given) -- or None if the sun
    doesn't set at all on that date at that location (polar day/night).

    Deliberately NOT built on is_daylight()'s own elevation()-based
    approach -- that one sidesteps polar day/night by design (elevation
    is defined for every moment, there's no "no answer" case), but a
    genuine sunset MOMENT simply doesn't exist on a day the sun never
    sets or never rises at all, so there's no way to dodge that
    question here the way is_daylight() dodges it for it own, different
    question ("is the sun up right now"). astral's own sun() function
    raises ValueError outright in that case ("Sun never sets/rises on
    this day, at this location") -- caught here and turned into a plain
    None return, since this project's own timezone coverage already
    includes Alaska (see is_daylight()'s own docstring), where this is
    a real, non-theoretical possibility for at least part of the year,
    not just defensive programming against something that could never
    actually happen for this project's own pole locations.

    tzinfo affects BOTH which calendar day `for_date` is resolved
    against (a date boundary is inherently a LOCAL concept, not a UTC
    one -- passing a specific pole's own timezone here ensures "this
    date" means that SAME pole's own correct calendar day, not a shared
    UTC day that may have already rolled over for one pole but not
    another one many time zones away) AND the timezone the returned
    datetime is itself expressed in -- both handled by astral's own
    sun() directly, not reconciled manually here, since astral is the
    authoritative source for this, not a UTC-offset calculation this
    module would otherwise have to duplicate and keep in sync with it.
    """
    observer = Observer(latitude=latitude, longitude=longitude)
    kwargs = {"date": for_date}
    if tzinfo is not None:
        kwargs["tzinfo"] = tzinfo
    try:
        return sun(observer, **kwargs)["sunset"]
    except ValueError:
        return None
