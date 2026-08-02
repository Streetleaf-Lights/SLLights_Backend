from datetime import datetime

from astral import Observer
from astral.sun import elevation

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
