"""Tests for shared/api_utils.py"""

from datetime import datetime, timezone

from shared import api_utils


class TestJsonSafe:
    def test_none_passes_through(self):
        assert api_utils.json_safe(None) is None

    def test_str_int_float_bool_pass_through_unchanged(self):
        assert api_utils.json_safe("abc") == "abc"
        assert api_utils.json_safe(42) == 42
        assert api_utils.json_safe(3.14) == 3.14
        assert api_utils.json_safe(True) is True

    def test_datetime_converted_to_string(self):
        dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = api_utils.json_safe(dt)
        assert isinstance(result, str)

    def test_date_converted_to_string(self):
        """Not just DATETIMEOFFSET columns -- a plain DATE column (e.g.
        Projects.EffectiveDate) needs the same treatment."""
        from datetime import date

        result = api_utils.json_safe(date(2026, 7, 15))
        assert isinstance(result, str)

    def test_unknown_type_converted_to_string(self):
        class Weird:
            def __str__(self):
                return "weird-value"

        assert api_utils.json_safe(Weird()) == "weird-value"


class TestClampLimit:
    def test_default_limit_equals_max_limit(self):
        """
        No limit specified should mean "everything, up to the ceiling",
        not some arbitrarily lower default -- business data tables here
        are very unlikely to need pagination, so silently truncating to a
        low default just loses real results for anyone who doesn't know
        to pass ?limit= explicitly. (This is the fix that came out of a
        real getCustomers production issue.)
        """
        assert api_utils.DEFAULT_LIMIT == api_utils.MAX_LIMIT

    def test_none_returns_default(self):
        assert api_utils.clamp_limit(None) == api_utils.DEFAULT_LIMIT

    def test_zero_returns_default(self):
        assert api_utils.clamp_limit(0) == api_utils.DEFAULT_LIMIT

    def test_normal_value_passes_through(self):
        assert api_utils.clamp_limit(50) == 50

    def test_value_above_max_is_capped(self):
        assert api_utils.clamp_limit(999999) == api_utils.MAX_LIMIT

    def test_negative_value_is_clamped_to_one(self):
        assert api_utils.clamp_limit(-5) == 1

    def test_string_digit_is_coerced_to_int(self):
        assert api_utils.clamp_limit("50") == 50
