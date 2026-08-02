"""Tests for shared/daylight_utils.py"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared.daylight_utils import is_daylight

EASTERN = ZoneInfo("America/New_York")
ALASKA = ZoneInfo("America/Anchorage")

# Chaparral Ph3, Brevard County FL -- real coordinates from this
# project's confirmed Leadsun /lamps sample.
_BREVARD_LAT, _BREVARD_LNG = 27.99507, -80.7236

# Utqiagvik (Barrow), Alaska -- ~71.3N, has real polar day/night, relevant
# since this project's timezone mapping already covers Alaska.
_UTQIAGVIK_LAT, _UTQIAGVIK_LNG = 71.2906, -156.7886

# Astral's own computed sunrise/sunset for Brevard FL on 2026-07-15,
# confirmed by direct computation while building this feature.
_KNOWN_SUNRISE = datetime(2026, 7, 15, 6, 36, 39, tzinfo=EASTERN)
_KNOWN_SUNSET = datetime(2026, 7, 15, 20, 20, 57, tzinfo=EASTERN)


class TestIsDaylightBasicCases:
    def test_noon_is_daylight(self):
        noon = datetime(2026, 7, 15, 12, 0, tzinfo=EASTERN)
        assert is_daylight(noon, _BREVARD_LAT, _BREVARD_LNG) is True

    def test_2am_is_not_daylight(self):
        two_am = datetime(2026, 7, 15, 2, 0, tzinfo=EASTERN)
        assert is_daylight(two_am, _BREVARD_LAT, _BREVARD_LNG) is False

    def test_returns_a_plain_bool(self):
        noon = datetime(2026, 7, 15, 12, 0, tzinfo=EASTERN)
        result = is_daylight(noon, _BREVARD_LAT, _BREVARD_LNG)
        assert result is True or result is False


class TestIsDaylightBoundary:
    def test_shortly_before_sunrise_is_not_daylight(self):
        before = _KNOWN_SUNRISE - timedelta(minutes=5)
        assert is_daylight(before, _BREVARD_LAT, _BREVARD_LNG) is False

    def test_shortly_after_sunrise_is_daylight(self):
        after = _KNOWN_SUNRISE + timedelta(minutes=5)
        assert is_daylight(after, _BREVARD_LAT, _BREVARD_LNG) is True

    def test_shortly_before_sunset_is_daylight(self):
        before = _KNOWN_SUNSET - timedelta(minutes=5)
        assert is_daylight(before, _BREVARD_LAT, _BREVARD_LNG) is True

    def test_shortly_after_sunset_is_not_daylight(self):
        after = _KNOWN_SUNSET + timedelta(minutes=5)
        assert is_daylight(after, _BREVARD_LAT, _BREVARD_LNG) is False


class TestIsDaylightPolarCases:
    """
    Utqiagvik has literal polar day/night -- astral's own sunrise()/
    sunset() functions raise ValueError for these dates ("Sun is always
    above/below the horizon on this day"), which is exactly why
    is_daylight() is built on elevation() instead. These tests exist
    specifically to confirm that choice actually avoids the failure.
    """

    def test_midnight_sun_noon_is_daylight(self):
        summer_noon = datetime(2026, 6, 21, 12, 0, tzinfo=ALASKA)
        assert is_daylight(summer_noon, _UTQIAGVIK_LAT, _UTQIAGVIK_LNG) is True

    def test_midnight_sun_actual_midnight_is_still_daylight(self):
        """The whole point of "midnight sun" -- the sun doesn't set even
        at literal midnight."""
        summer_midnight = datetime(2026, 6, 21, 0, 0, tzinfo=ALASKA)
        assert is_daylight(summer_midnight, _UTQIAGVIK_LAT, _UTQIAGVIK_LNG) is True

    def test_polar_night_noon_is_not_daylight(self):
        winter_noon = datetime(2026, 12, 21, 12, 0, tzinfo=ALASKA)
        assert is_daylight(winter_noon, _UTQIAGVIK_LAT, _UTQIAGVIK_LNG) is False

    def test_does_not_raise_for_either_polar_case(self):
        """Regression guard: astral's sunrise()/sunset() raise ValueError
        for both polar cases -- is_daylight() must not propagate that."""
        summer_noon = datetime(2026, 6, 21, 12, 0, tzinfo=ALASKA)
        winter_noon = datetime(2026, 12, 21, 12, 0, tzinfo=ALASKA)
        is_daylight(summer_noon, _UTQIAGVIK_LAT, _UTQIAGVIK_LNG)  # must not raise
        is_daylight(winter_noon, _UTQIAGVIK_LAT, _UTQIAGVIK_LNG)  # must not raise


class TestIsDaylightRequiresAwareDatetime:
    def test_naive_datetime_raises_value_error(self):
        naive = datetime(2026, 7, 15, 12, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            is_daylight(naive, _BREVARD_LAT, _BREVARD_LNG)


class TestIsDaylightTimezoneInvariance:
    def test_same_utc_instant_agrees_regardless_of_timezone_label(self):
        """
        Solar position depends on the actual UTC instant, not which
        timezone was used to express it -- the same physical moment must
        give the same answer whether passed in as UTC, Eastern, or any
        other equivalent representation.
        """
        dt_utc = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("UTC"))
        dt_eastern = dt_utc.astimezone(EASTERN)
        dt_alaska_tz = dt_utc.astimezone(ALASKA)  # different LABEL, same instant

        result_utc = is_daylight(dt_utc, _BREVARD_LAT, _BREVARD_LNG)
        result_eastern = is_daylight(dt_eastern, _BREVARD_LAT, _BREVARD_LNG)
        result_alaska_label = is_daylight(dt_alaska_tz, _BREVARD_LAT, _BREVARD_LNG)

        assert result_utc == result_eastern == result_alaska_label


class TestIsDaylightCivilTwilight:
    def test_civil_twilight_extends_the_window_before_sunrise(self):
        """A moment after civil dawn but before actual sunrise should be
        daylight under the broader civil-twilight definition, but not
        under the strict sunrise/sunset definition."""
        shortly_before_sunrise = _KNOWN_SUNRISE - timedelta(minutes=10)

        assert is_daylight(shortly_before_sunrise, _BREVARD_LAT, _BREVARD_LNG) is False
        assert (
            is_daylight(
                shortly_before_sunrise, _BREVARD_LAT, _BREVARD_LNG, use_civil_twilight=True
            )
            is True
        )

    def test_civil_twilight_extends_the_window_after_sunset(self):
        shortly_after_sunset = _KNOWN_SUNSET + timedelta(minutes=10)

        assert is_daylight(shortly_after_sunset, _BREVARD_LAT, _BREVARD_LNG) is False
        assert (
            is_daylight(
                shortly_after_sunset, _BREVARD_LAT, _BREVARD_LNG, use_civil_twilight=True
            )
            is True
        )

    def test_default_is_the_stricter_sunrise_sunset_definition(self):
        """Confirms use_civil_twilight defaults to False, not True."""
        shortly_before_sunrise = _KNOWN_SUNRISE - timedelta(minutes=10)
        assert is_daylight(shortly_before_sunrise, _BREVARD_LAT, _BREVARD_LNG) == is_daylight(
            shortly_before_sunrise, _BREVARD_LAT, _BREVARD_LNG, use_civil_twilight=False
        )

    def test_deep_night_is_not_daylight_under_either_definition(self):
        deep_night = datetime(2026, 7, 15, 2, 0, tzinfo=EASTERN)
        assert is_daylight(deep_night, _BREVARD_LAT, _BREVARD_LNG) is False
        assert is_daylight(deep_night, _BREVARD_LAT, _BREVARD_LNG, use_civil_twilight=True) is False
