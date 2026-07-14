"""Tests for shared/datetime_utils.py"""

import re
from datetime import datetime, timedelta, timezone

from shared import datetime_utils

DTO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$")


class TestToDtoString:
    def test_formats_negative_offset(self):
        dt = datetime(2026, 7, 2, 14, 14, 39, 901000, tzinfo=timezone(timedelta(hours=-4)))
        assert datetime_utils.to_dto_string(dt) == "2026-07-02 14:14:39.901 -04:00"

    def test_formats_positive_offset(self):
        dt = datetime(2026, 1, 15, 9, 0, 0, 500000, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        assert datetime_utils.to_dto_string(dt) == "2026-01-15 09:00:00.500 +05:30"

    def test_formats_utc_zero_offset(self):
        dt = datetime(2025, 11, 17, 19, 56, 44, 0, tzinfo=timezone.utc)
        assert datetime_utils.to_dto_string(dt) == "2025-11-17 19:56:44.000 +00:00"

    def test_truncates_microseconds_to_milliseconds(self):
        dt = datetime(2026, 3, 1, 0, 0, 0, 123456, tzinfo=timezone(timedelta(hours=-5)))
        result = datetime_utils.to_dto_string(dt)
        assert result.endswith(".123 -05:00")

    def test_output_matches_dto_shape(self):
        dt = datetime.now(datetime_utils.EASTERN)
        assert DTO_PATTERN.match(datetime_utils.to_dto_string(dt))


class TestNowEastern:
    def test_returns_aware_datetime_in_eastern(self):
        result = datetime_utils.now_eastern()
        assert result.tzinfo is not None
        assert result.utcoffset() is not None


class TestAirtableCreatedTimeToEastern:
    def test_none_returns_none(self):
        assert datetime_utils.airtable_created_time_to_eastern(None) is None

    def test_empty_string_returns_none(self):
        assert datetime_utils.airtable_created_time_to_eastern("") is None

    def test_winter_utc_converts_to_est_minus_5(self):
        # Nov 17 is outside US DST -> EST, UTC-5
        result = datetime_utils.airtable_created_time_to_eastern("2025-11-17T19:56:44.000Z")
        assert result == "2025-11-17 14:56:44.000 -05:00"

    def test_summer_utc_converts_to_edt_minus_4(self):
        # July 2 is inside US DST -> EDT, UTC-4
        result = datetime_utils.airtable_created_time_to_eastern("2026-07-02T18:00:00.000Z")
        assert result == "2026-07-02 14:00:00.000 -04:00"

    def test_result_matches_dto_shape(self):
        result = datetime_utils.airtable_created_time_to_eastern("2026-01-01T00:00:00.000Z")
        assert DTO_PATTERN.match(result)
