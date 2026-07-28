"""Tests for shared/timezone_utils.py"""

import pytest

from shared import timezone_utils


class TestResolveIanaTimezone:
    def test_known_coordinate_resolves(self):
        # Chaparral Ph3, Brevard County FL -- real coordinates from this
        # project's confirmed Leadsun /lamps sample.
        assert timezone_utils.resolve_iana_timezone(27.99507, -80.7236) == "America/New_York"

    def test_none_latitude_returns_none(self):
        assert timezone_utils.resolve_iana_timezone(None, -80.7236) is None

    def test_none_longitude_returns_none(self):
        assert timezone_utils.resolve_iana_timezone(27.99507, None) is None

    def test_both_none_returns_none(self):
        assert timezone_utils.resolve_iana_timezone(None, None) is None

    def test_real_world_micro_degrees_longitude_returns_none_not_a_raised_exception(self, caplog):
        """
        Exact real-world case seen in production: a longitude of
        -82533519.0 (-82.533519 x 1,000,000) -- a device reporting
        micro-degrees without converting back to decimal degrees.
        timezonefinder itself raises ValueError for this; must be caught
        and turned into a clean None, not propagate as an unhandled
        exception (which would otherwise mean this LocationId never gets
        a PoleTimeZones row at all, and gets retried -- and re-fails,
        and re-logs -- every single cycle forever).
        """
        with caplog.at_level("WARNING"):
            result = timezone_utils.resolve_iana_timezone(28.5, -82533519.0)

        assert result is None
        assert any("outside the valid coordinate range" in rec.message for rec in caplog.records)

    def test_out_of_range_latitude_returns_none(self, caplog):
        with caplog.at_level("WARNING"):
            result = timezone_utils.resolve_iana_timezone(95.0, -80.0)
        assert result is None
        assert any("outside the valid coordinate range" in rec.message for rec in caplog.records)

    def test_out_of_range_does_not_call_timezonefinder(self, mocker):
        """The range check must reject before ever calling timezonefinder
        -- there's no reason to invoke it (and risk it raising) for a
        value already known to be invalid."""
        mock_tf = mocker.patch("shared.timezone_utils._tf")
        timezone_utils.resolve_iana_timezone(28.5, -82533519.0)
        mock_tf.timezone_at.assert_not_called()

    def test_boundary_values_are_valid_not_rejected(self):
        """-90/90/-180/180 are the exact edges of valid range -- must not
        be rejected by an off-by-one in the range check."""
        # These shouldn't log an "outside valid range" warning or return
        # None purely due to range rejection (they may still resolve to
        # None for other reasons, e.g. genuinely no timezone there, but
        # must not be blocked by the range check itself).
        timezone_utils.resolve_iana_timezone(90.0, 180.0)  # must not raise
        timezone_utils.resolve_iana_timezone(-90.0, -180.0)  # must not raise

    def test_timezonefinder_raising_for_an_unanticipated_reason_is_caught(self, mocker, caplog):
        """Defense in depth: even a within-range coordinate that
        timezonefinder itself rejects for some other reason must not
        propagate as an unhandled exception."""
        mock_tf = mocker.patch("shared.timezone_utils._tf")
        mock_tf.timezone_at.side_effect = ValueError("some other timezonefinder-internal complaint")

        with caplog.at_level("WARNING"):
            result = timezone_utils.resolve_iana_timezone(27.99507, -80.7236)

        assert result is None
        assert any("timezonefinder rejected" in rec.message for rec in caplog.records)

    def test_valid_range_but_genuinely_unresolvable_logs_its_own_warning(self, mocker, caplog):
        """A valid-range coordinate that timezonefinder simply can't map
        to any timezone (returns None without raising) is a third,
        distinct case from both the range check and the raised-exception
        case -- must not go completely silent."""
        mock_tf = mocker.patch("shared.timezone_utils._tf")
        mock_tf.timezone_at.return_value = None

        with caplog.at_level("WARNING"):
            result = timezone_utils.resolve_iana_timezone(27.99507, -80.7236)

        assert result is None
        assert any(
            "no timezone could be resolved" in rec.message for rec in caplog.records
        )


class TestResolveWindowsTimezone:
    def test_null_island_returns_none_none_not_etc_gmt(self, caplog):
        """
        lat=0.0, lng=0.0 -- "Null Island" -- is a common placeholder GPS
        hardware reports when it hasn't acquired a real fix, not a
        genuine location. timezonefinder would otherwise correctly (but
        unhelpfully) resolve this to "Etc/GMT" -- must NOT be treated as
        a real coordinate needing a new IANA_TO_WINDOWS entry.
        """
        with caplog.at_level("WARNING"):
            iana, windows = timezone_utils.resolve_windows_timezone(0.0, 0.0)

        assert (iana, windows) == (None, None)
        assert any("Null Island" in rec.message for rec in caplog.records)

    def test_null_island_does_not_call_resolve_iana_timezone(self, mocker):
        """Skips resolution entirely for (0, 0) rather than calling
        resolve_iana_timezone() and discarding the result -- cheaper, and
        avoids ever exercising the real Etc/GMT resolution path at all."""
        spy = mocker.patch("shared.timezone_utils.resolve_iana_timezone")
        timezone_utils.resolve_windows_timezone(0.0, 0.0)
        spy.assert_not_called()

    def test_only_exact_null_island_is_treated_as_a_placeholder(self, caplog):
        """Only the exact (0.0, 0.0) pair is treated as a placeholder --
        a real, non-zero coordinate must go through normal resolution
        (and NOT get the Null Island warning), even if it happens to
        land in the same Etc/GMT zone that (0, 0) does."""
        with caplog.at_level("WARNING"):
            timezone_utils.resolve_windows_timezone(27.99507, -80.7236)

        assert not any("Null Island" in rec.message for rec in caplog.records)

    def test_genuinely_unresolvable_coordinate_returns_none_none(self, mocker):
        """A coordinate resolve_iana_timezone() can't resolve at all
        (already logs its own specific reason -- see
        TestResolveIanaTimezone) must still cleanly produce (None, None)
        here, without resolve_windows_timezone() adding a second,
        redundant warning on top."""
        mocker.patch("shared.timezone_utils.resolve_iana_timezone", return_value=None)

        iana, windows = timezone_utils.resolve_windows_timezone(12.34, 56.78)

        assert (iana, windows) == (None, None)

    def test_out_of_range_coordinate_logs_exactly_one_warning_not_two(self, caplog):
        """
        Regression guard: an out-of-range coordinate must produce exactly
        one clear warning (from resolve_iana_timezone's range check), not
        that warning PLUS a second, more generic "couldn't resolve"
        warning from resolve_windows_timezone on top of it.
        """
        with caplog.at_level("WARNING"):
            timezone_utils.resolve_windows_timezone(28.5, -82533519.0)

        assert len(caplog.records) == 1


    @pytest.mark.parametrize(
        "lat,lng,expected_iana,expected_windows",
        [
            (27.99507, -80.7236, "America/New_York", "Eastern Standard Time"),
            (41.8781, -87.6298, "America/Chicago", "Central Standard Time"),
            (39.7392, -104.9903, "America/Denver", "Mountain Standard Time"),
            (33.4484, -112.0740, "America/Phoenix", "US Mountain Standard Time"),
            (37.7749, -122.4194, "America/Los_Angeles", "Pacific Standard Time"),
            (21.3069, -157.8583, "Pacific/Honolulu", "Hawaiian Standard Time"),
            (61.2181, -149.9003, "America/Anchorage", "Alaskan Standard Time"),
            (18.2208, -66.5901, "America/Puerto_Rico", "SA Western Standard Time"),
        ],
    )
    def test_known_us_coordinates_resolve_correctly(self, lat, lng, expected_iana, expected_windows):
        iana, windows = timezone_utils.resolve_windows_timezone(lat, lng)
        assert iana == expected_iana
        assert windows == expected_windows

    def test_arizona_gets_its_own_distinct_zone_not_mountain_time(self):
        """Arizona doesn't observe DST, so it has its own Windows zone
        distinct from "Mountain Standard Time" -- worth a dedicated test
        given how easy this specific case is to get wrong by assuming
        Phoenix just uses the same zone as Denver."""
        _, denver_windows = timezone_utils.resolve_windows_timezone(39.7392, -104.9903)
        _, phoenix_windows = timezone_utils.resolve_windows_timezone(33.4484, -112.0740)
        assert denver_windows != phoenix_windows

    def test_none_coordinates_returns_none_none(self):
        assert timezone_utils.resolve_windows_timezone(None, None) == (None, None)

    def test_unmapped_iana_zone_returns_iana_with_none_windows(self, mocker, caplog):
        """
        A coordinate resolving to a real IANA zone that isn't in
        IANA_TO_WINDOWS (e.g. somewhere outside the deliberately
        US-scoped mapping) must not silently guess -- the caller gets
        (iana_name, None) and a logged error, so it can decide how to
        fall back rather than mis-bucketing that pole's vitals under a
        wrong timezone.
        """
        mocker.patch(
            "shared.timezone_utils.resolve_iana_timezone", return_value="Europe/Paris"
        )
        with caplog.at_level("ERROR"):
            iana, windows = timezone_utils.resolve_windows_timezone(48.8566, 2.3522)

        assert iana == "Europe/Paris"
        assert windows is None
        assert any("no known Windows timezone mapping" in rec.message for rec in caplog.records)


class TestIanaToWindowsMapping:
    def test_every_value_is_a_plausible_windows_zone_name(self):
        """Loose sanity check -- every mapped value should look like a
        real Windows zone name (ends in "Time"), catching an obvious typo
        rather than validating against the full Windows zone list."""
        for iana, windows in timezone_utils.IANA_TO_WINDOWS.items():
            assert windows.endswith("Time"), f"{iana} -> {windows!r} looks malformed"

    def test_no_duplicate_iana_keys(self):
        """Dict literal syntax would silently keep only the last of any
        duplicate key -- confirms the source list itself has none."""
        keys = list(timezone_utils.IANA_TO_WINDOWS.keys())
        assert len(keys) == len(set(keys))
