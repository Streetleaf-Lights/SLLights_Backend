"""
Tests for the coordinate-source change in shared/pole_timezones_loader.py
-- resolving timezones from Poles.Lat/Poles.Long (Airtable) instead of
PoleTelemetry's raw device GPS.

Focused on what changed, not a full test suite for this module's overall
behavior (SP_Execution tracking, error handling, etc.) -- this project
may already have broader coverage for load_pole_timezones() elsewhere
that wasn't available to check against when these were written.
"""

from unittest.mock import MagicMock, patch

from shared import pole_timezones_loader as m


class TestSourceConstants:
    def test_execution_source_is_still_leadsun(self):
        """SP_Execution.Source tracks which pipeline this loader's run
        belongs to -- unchanged, since this loader still runs as part of
        the Leadsun pipeline's load order regardless of where the
        coordinates it resolves come from."""
        assert m.EXECUTION_SOURCE == "Leadsun"

    def test_coordinate_source_is_now_airtable(self):
        """PoleTimeZones.Source tracks where each row's coordinates
        actually came from -- now Airtable (via Poles.Lat/Long), not
        Leadsun (PoleTelemetry's raw GPS)."""
        assert m.COORDINATE_SOURCE == "Airtable"

    def test_the_two_constants_are_distinct(self):
        """The whole point of splitting these apart -- they must not
        silently collapse back into a single shared value."""
        assert m.EXECUTION_SOURCE != m.COORDINATE_SOURCE


class TestFindUnresolvedLocationsSql:
    def test_sources_from_poles_not_pole_telemetry(self):
        sql = m._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "FROM Poles p" in sql
        assert "PoleTelemetry" not in sql

    def test_selects_long_and_lat_aliased_to_longitude_latitude(self):
        """Poles' own column names (Long/Lat) differ from PoleTimeZones'
        (Longitude/Latitude) -- the aliases keep the rest of this
        module's column-position-based unpacking working unchanged."""
        sql = m._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "p.Long AS Longitude" in sql
        assert "p.Lat AS Latitude" in sql

    def test_still_left_joins_pole_time_zones_to_find_unresolved_ones(self):
        sql = m._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId" in sql
        assert "ptz.LocationId IS NULL" in sql

    def test_filters_out_poles_with_no_location_id_yet(self):
        """A pole can exist in Poles before it's linked to a real Leadsun
        device -- without this filter, a NULL LocationId would satisfy
        the LEFT JOIN's "not yet resolved" condition and attempt to
        resolve/insert a timezone row for a pole with no real location."""
        sql = m._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "p.LocationId IS NOT NULL" in sql

    def test_no_longer_needs_group_by_or_aggregation(self):
        """Poles is a reference table (one row per pole), unlike
        PoleTelemetry's many time-series rows per LocationId -- the
        MIN()-based "pick any one representative reading" logic this
        replaced is no longer needed."""
        sql = m._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "GROUP BY" not in sql
        assert "MIN(" not in sql


class TestLoadPoleTimezones:
    def _run_with_one_unresolved_location(self, mocker, mock_cursor):
        mock_cursor.fetchone.return_value = (1,)  # SP_Execution.Id
        mock_cursor.fetchall.return_value = [("LOC-001", -82.27, 28.30)]
        mocker.patch(
            "shared.pole_timezones_loader.resolve_windows_timezone",
            return_value=("America/New_York", "Eastern Standard Time"),
        )

    def test_sp_execution_insert_uses_execution_source(self, mocker):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        self._run_with_one_unresolved_location(mocker, mock_cursor)

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        sp_execution_call = mock_cursor.execute.call_args_list[0]
        bound_source = sp_execution_call.args[4]  # Name, Environment, StartDateTime, Source
        assert bound_source == "Leadsun"

    def test_pole_time_zones_upsert_uses_coordinate_source_not_execution_source(self, mocker):
        """The actual bug this split was meant to prevent: PoleTimeZones
        rows must be tagged "Airtable", not silently inherit the
        SP_Execution-tracking "Leadsun" value."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        self._run_with_one_unresolved_location(mocker, mock_cursor)

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        upsert_call = mock_cursor.execute.call_args_list[2]  # 0=SP_Exec insert, 1=find-unresolved query, 2=upsert
        bound_source = upsert_call.args[6]  # LocationId, Long, Lat, Iana, Windows, Source, SP_ExecId
        assert bound_source == "Airtable"

    def test_resolve_windows_timezone_still_called_with_latitude_then_longitude(self, mocker):
        """Argument order into resolve_windows_timezone() is unchanged --
        only where longitude/latitude originate from changed, not how
        they're used once fetched."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("LOC-001", -82.27, 28.30)]
        mock_resolve = mocker.patch(
            "shared.pole_timezones_loader.resolve_windows_timezone",
            return_value=("America/New_York", "Eastern Standard Time"),
        )

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        mock_resolve.assert_called_once_with(28.30, -82.27)
