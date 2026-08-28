"""
Tests for shared/daylight_utils.py -- specifically get_sunset(), added
per explicit request. is_daylight() predates this addition and likely
already has its own established test coverage elsewhere; if so, merge
this file's own TestGetSunset class into that existing file rather than
overwriting it with this one.
"""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from shared import daylight_utils as m

EASTERN = ZoneInfo("America/New_York")
ALASKA = ZoneInfo("America/Anchorage")

# Brevard County, FL -- matches this project's own real sample data used
# elsewhere in this codebase's own tests.
_FL_LAT, _FL_LONG = 28.2, -80.7

# Utqiagvik (Barrow), Alaska -- far enough north for genuine polar
# day/night, not a synthetic edge case.
_AK_LAT, _AK_LONG = 71.29, -156.79


class TestGetSunset:
    def test_returns_an_aware_datetime_for_a_normal_location_and_date(self):
        result = m.get_sunset(date(2026, 8, 27), _FL_LAT, _FL_LONG, tzinfo=EASTERN)
        assert result is not None
        assert result.tzinfo is not None
        assert result.date() == date(2026, 8, 27)

    def test_defaults_to_utc_when_tzinfo_not_given(self):
        result = m.get_sunset(date(2026, 8, 27), _FL_LAT, _FL_LONG)
        assert result is not None
        assert result.utcoffset().total_seconds() == 0

    def test_result_is_expressed_in_the_given_tzinfo(self):
        eastern_result = m.get_sunset(date(2026, 8, 27), _FL_LAT, _FL_LONG, tzinfo=EASTERN)
        utc_result = m.get_sunset(date(2026, 8, 27), _FL_LAT, _FL_LONG)
        # Same instant, different representation.
        assert eastern_result.astimezone(ZoneInfo("UTC")) == utc_result

    def test_sunset_time_is_plausible_for_late_august_in_florida(self):
        """Not testing astral's own astronomical correctness (that's
        astral's job) -- just a sanity bound that this integrates with
        it correctly, producing something in the right ballpark rather
        than, say, sunrise or a wildly wrong hour."""
        result = m.get_sunset(date(2026, 8, 27), _FL_LAT, _FL_LONG, tzinfo=EASTERN)
        assert 18 <= result.hour <= 21

    def test_polar_day_returns_none_not_an_exception(self):
        """The whole reason this function exists as a wrapper rather
        than a direct call to astral.sun.sun() -- astral's own sun()
        raises ValueError outright when the sun never sets on this
        date at this location, which a summer solstice at this
        latitude genuinely triggers."""
        result = m.get_sunset(date(2026, 6, 21), _AK_LAT, _AK_LONG, tzinfo=ALASKA)
        assert result is None

    def test_polar_night_returns_none_not_an_exception(self):
        """Same underlying astral limitation, the opposite season --
        the sun never RISES on this date at this location, so there's
        no sunset to compute either."""
        result = m.get_sunset(date(2026, 12, 21), _AK_LAT, _AK_LONG, tzinfo=ALASKA)
        assert result is None

    def test_same_location_ordinary_date_still_works_near_the_pole(self):
        """Confirms the ValueError-catching doesn't accidentally
        swallow legitimate results for the SAME extreme-latitude
        location on a date where sunset genuinely does occur (e.g. near
        an equinox) -- polar day/night is seasonal, not permanent, for
        this location."""
        result = m.get_sunset(date(2026, 3, 20), _AK_LAT, _AK_LONG, tzinfo=ALASKA)
        assert result is not None

    def test_date_boundary_resolved_against_given_tzinfo_not_utc(self):
        """A date is inherently a local concept -- passing a specific
        timezone must resolve "this calendar date" against THAT
        timezone, not silently treat it as a UTC day regardless of what
        tzinfo was given."""
        eastern_result = m.get_sunset(date(2026, 8, 27), _FL_LAT, _FL_LONG, tzinfo=EASTERN)
        assert eastern_result.year == 2026
        assert eastern_result.month == 8
        assert eastern_result.day == 27
