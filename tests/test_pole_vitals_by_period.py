"""
Tests for get_pole_vitals_by_period() in shared/pole_vitals_api.py.

Uses the same patch_get_connection_pole_vitals_api fixture already
defined in tests/conftest.py for the rest of shared/pole_vitals_api.py's
tests.
"""

import pytest

from shared import pole_vitals_api


def _pole_info_row(
    pole_id, pole_number, location_id,
    install_date=None, lat=None, long_=None, last_update=None,
):
    return (pole_id, pole_number, location_id, install_date, lat, long_, last_update)


def _vitals_row(
    period_start, period_end,
    light_status=None, is_online=None,
    battery_percentage=None, panel_percentage=None, light_percentage=None,
):
    return (period_start, period_end, light_status, is_online, battery_percentage, panel_percentage, light_percentage)


class TestPoleVitalsHistorySqlStructure:
    def test_pole_info_query_has_no_battery_voltage_columns(self):
        """batteryVoltage1/batteryVoltage2 were dropped from this
        endpoint entirely per explicit request -- along with the
        PoleTelemetry join that would otherwise be needed to get them,
        this query should only need PoleTelemetry for LastUpload."""
        sql = pole_vitals_api._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql
        assert "LastUpload" in sql

    def test_pole_info_query_uses_a_single_outer_apply(self):
        sql = pole_vitals_api._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert sql.count("OUTER APPLY") == 1
        assert "CROSS APPLY" not in sql

    def test_history_query_has_no_rollup_aggregation(self):
        """Each entry is a direct read of one PoleVitals row -- no
        GROUP BY, no MAX(CASE...) priority logic, no AVG() across rows,
        unlike _RECENT_POLE_STATS_CTE elsewhere in this module."""
        sql = pole_vitals_api._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert "GROUP BY" not in sql
        assert "MAX(CASE" not in sql
        assert "AVG(" not in sql

    def test_history_query_includes_period_start_and_end(self):
        """Without these, an array of otherwise-identical-shaped
        percentage values would have no way to say which hour/day each
        one belongs to."""
        sql = pole_vitals_api._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert "PeriodStart" in sql
        assert "PeriodEnd" in sql

    def test_history_query_orders_most_recent_first(self):
        sql = pole_vitals_api._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert "ORDER BY pv.PeriodStart DESC" in sql

    def test_history_query_is_bounded_by_a_top_clause(self):
        """PoleVitals has no retention/cleanup of its own -- this must
        never be a truly-unbounded SELECT."""
        sql = pole_vitals_api._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert "TOP (?)" in sql

    def test_history_query_filters_by_pole_id_and_period_type(self):
        sql = pole_vitals_api._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert "p.Id = ?" in sql
        assert "pv.PeriodType = ?" in sql


class TestGetPoleVitalsByPeriod:
    def test_rejects_invalid_period_type_without_querying_the_database(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        with pytest.raises(ValueError, match="Hour, Day"):
            pole_vitals_api.get_pole_vitals_by_period("pole1", "Week")
        mock_cursor.execute.assert_not_called()

    def test_rejects_removed_period_types_specifically(self):
        """Week/Month were removed from PoleVitals entirely -- reject
        them the same as any other invalid value, not silently return
        an empty history for them."""
        for removed in ("Week", "Month"):
            with pytest.raises(ValueError):
                pole_vitals_api.get_pole_vitals_by_period("pole1", removed)

    def test_nonexistent_pole_returns_none_without_querying_history(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """Short-circuits before the second (history) query at all --
        no point running it for a pole that doesn't exist."""
        mock_cursor.fetchone.return_value = None

        result = pole_vitals_api.get_pole_vitals_by_period("does-not-exist", "Hour")

        assert result is None
        mock_cursor.fetchall.assert_not_called()

    def test_pole_exists_with_no_history_yet_returns_empty_list_not_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """A real pole that just has no PoleVitals rows of this period
        type yet -- found, not 404-worthy; "vitals" is simply empty."""
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Day")

        assert result["id"] == "pole1"
        assert result["vitals"] == []

    def test_pole_with_no_telemetry_yet_has_null_last_update(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")

        assert result["lastUpdate"] is None

    def test_full_history_maps_correctly_and_does_not_include_battery_voltage(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row(
            "recg1jYzmCtPB170y", "PAS-4938", "12101-4938",
            install_date="2025-08-28", lat=28.3031566, long_=-82.2750467,
            last_update="2026-07-30 12:39:41+00:00",
        )
        mock_cursor.fetchall.return_value = [
            _vitals_row(
                "2026-07-30 11:00:00+00:00", "2026-07-30 12:00:00+00:00",
                light_status="DayLight", is_online=True,
                battery_percentage=89.71, panel_percentage=2.77, light_percentage=17.77,
            ),
            _vitals_row(
                "2026-07-30 10:00:00+00:00", "2026-07-30 11:00:00+00:00",
                light_status="Working", is_online=True,
                battery_percentage=88.5, panel_percentage=45.2, light_percentage=0.0,
            ),
        ]

        result = pole_vitals_api.get_pole_vitals_by_period("recg1jYzmCtPB170y", "Hour")

        assert result["id"] == "recg1jYzmCtPB170y"
        assert result["poleNumber"] == "PAS-4938"
        assert result["lastUpdate"] == "2026-07-30 12:39:41+00:00"
        assert "batteryVoltage1" not in result
        assert "batteryVoltage2" not in result
        assert len(result["vitals"]) == 2
        assert result["vitals"][0] == {
            "periodStart": "2026-07-30 11:00:00+00:00",
            "periodEnd": "2026-07-30 12:00:00+00:00",
            "lightStatus": "DayLight",
            "isOnline": True,
            "avgBatteryPercentage": 89.71,
            "avgPanelPercentage": 2.77,
            "avgLightPercentage": 17.77,
        }
        assert "batteryVoltage1" not in result["vitals"][0]

    def test_entries_are_distinguishable_by_period_boundaries(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """The core reason periodStart/periodEnd had to be added --
        without them, two entries with different values would otherwise
        be indistinguishable in the array."""
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = [
            _vitals_row("hour2-start", "hour2-end", light_status="Not Working"),
            _vitals_row("hour1-start", "hour1-end", light_status="Working"),
        ]

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")

        assert result["vitals"][0]["periodStart"] == "hour2-start"
        assert result["vitals"][1]["periodStart"] == "hour1-start"
        assert result["vitals"][0]["lightStatus"] != result["vitals"][1]["lightStatus"]

    def test_default_limit_is_applied_when_not_specified(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")

        history_call = mock_cursor.execute.call_args_list[1]
        bound_limit = history_call.args[1]
        assert bound_limit is not None and bound_limit > 0

    def test_custom_limit_is_passed_through(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour", limit=5)

        history_call = mock_cursor.execute.call_args_list[1]
        assert history_call.args[1] == 5

    def test_history_query_bound_params_match_placeholder_order(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """TOP (?) appears before p.Id = ? and pv.PeriodType = ? in the
        SQL text, so limit must be bound first, then pole_id, then
        period_type."""
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals_by_period("pole1", "Day", limit=10)

        history_call = mock_cursor.execute.call_args_list[1]
        _, limit, pole_id, period_type = history_call.args
        assert (limit, pole_id, period_type) == (10, "pole1", "Day")
