"""Tests for shared/poles_api.py"""

import pytest

from shared import poles_api as m


class TestClampSummaryLimit:
    def test_none_defaults_to_summary_max(self):
        assert m._clamp_summary_limit(None) == m._SUMMARY_MAX_LIMIT

    def test_zero_defaults_to_summary_max(self):
        assert m._clamp_summary_limit(0) == m._SUMMARY_MAX_LIMIT

    def test_value_above_max_is_capped(self):
        assert m._clamp_summary_limit(999999) == m._SUMMARY_MAX_LIMIT

    def test_value_within_range_passes_through(self):
        assert m._clamp_summary_limit(500) == 500

    def test_summary_max_is_higher_than_default_api_max(self):
        from shared.api_utils import MAX_LIMIT
        assert m._SUMMARY_MAX_LIMIT > MAX_LIMIT


class TestPoleSummarySqlStructure:
    def test_direct_left_join_no_cte(self):
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "LEFT JOIN PoleVitals rps ON p.LocationId = rps.LocationId AND rps.PeriodType = ?" in sql
        assert "RecentPoleStats" not in sql

    def test_all_five_fault_flags_present(self):
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        for col in ("IsLedFault", "IsBatteryFault", "IsPanelFault", "IsOpenIssueFault", "IsPoleFault"):
            assert f"rps.{col} AS {col}" in sql

    def test_no_light_status(self):
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "LightStatus" not in sql

    def test_lean_outer_apply_for_last_update_only(self):
        """summary mode now includes lastUpdate (the web frontend needs
        it to distinguish "Disconnected" from "Unknown"), via a leaner,
        single-column OUTER APPLY -- confirms it's genuinely lean: no
        PoleModels join, no BatteryChargingMin, no batteryVoltage1/2 or
        the other telemetry columns the full detail query pulls."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "OUTER APPLY" in sql
        assert "SELECT TOP 1 pt.LastUpload" in sql
        assert "PoleModels" not in sql
        assert "BatteryChargingMin" not in sql
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql
        assert "LampPower1" not in sql

    def test_last_update_converted_to_poles_own_local_time_zone(self):
        """Consistency with the full detail query
        (_POLE_DETAILS_SQL_TEMPLATE): lastUpdate is converted via
        PoleTimeZones, not returned as raw UTC -- so a frontend
        consuming both summary and detail responses sees the same
        format either way."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId" in sql
        assert (
            "latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload"
            in sql
        )

    def test_outer_apply_is_outer_not_cross(self):
        """A pole with no LocationId, or zero matching PoleTelemetry
        rows at all, must still appear in the summary (with LastUpload
        NULL) -- not silently disappear from the "give me every pole"
        result."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "OUTER APPLY" in sql
        assert "CROSS APPLY" not in sql

    def test_isonline_uses_a_separate_join_reverted_to_last_48_hours(self):
        """Same fix as pole_vitals_api.py's own _POLE_DETAILS_SQL_TEMPLATE:
        every other field reads rps (LastKnown48Hours), but IsOnline
        specifically reads a SECOND join, rps_online (Last48Hours) --
        a silent pole's LastKnown48Hours.IsOnline would misleadingly
        reflect its own last-known state, not whether it's online RIGHT
        NOW."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "rps_online.IsOnline AS IsOnline" in sql
        assert "rps.IsOnline" not in sql
        assert (
            "LEFT JOIN PoleVitals rps_online ON p.LocationId = rps_online.LocationId "
            "AND rps_online.PeriodType = ?" in sql
        )
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql

    def test_includes_customer_id_as_last_column(self):
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "c.Id AS CustomerId" in sql


class TestSummaryRowToDict:
    def _row(
        self,
        project_id="proj1", pole_id="pole1", pole_number="PN-1", location_id="LOC-1",
        install_date="2025-01-01", lat=28.0, long_=-82.0,
        last_update="2026-08-16 08:00:00 -04:00",
        is_online=True, is_led_fault=False, is_battery_fault=False, is_panel_fault=False,
        is_open_issue_fault=False, is_pole_fault=False,
        battery_percentage=89.0, panel_percentage=45.0, light_percentage=0.0,
        customer_id="cust1",
    ):
        return (
            project_id, pole_id, pole_number, location_id, install_date, lat, long_,
            last_update,
            is_online, is_led_fault, is_battery_fault, is_panel_fault, is_open_issue_fault, is_pole_fault,
            battery_percentage, panel_percentage, light_percentage, customer_id,
        )

    def test_maps_every_field(self):
        result = m._summary_row_to_dict(self._row())
        assert result == {
            "id": "pole1",
            "poleNumber": "PN-1",
            "locationId": "LOC-1",
            "installDate": "2025-01-01",
            "lat": 28.0,
            "long": -82.0,
            "lastUpdate": "2026-08-16 08:00:00 -04:00",
            "isOnline": True,
            "isLedFault": False,
            "isBatteryFault": False,
            "isPanelFault": False,
            "isOpenIssueFault": False,
            "isPoleFault": False,
            "avgBatteryPercentage": 89.0,
            "avgPanelPercentage": 45.0,
            "avgLightPercentage": 0.0,
            "projectId": "proj1",
            "customerId": "cust1",
        }

    def test_lacks_voltage_fields_but_keeps_last_update(self):
        """batteryVoltage1/batteryVoltage2 remain excluded -- the actual
        remaining cost tradeoff this lighter query still makes.
        lastUpdate is now INCLUDED (the point of this turn's change)."""
        result = m._summary_row_to_dict(self._row())
        assert "lastUpdate" in result
        assert "batteryVoltage1" not in result
        assert "batteryVoltage2" not in result

    def test_no_light_status(self):
        result = m._summary_row_to_dict(self._row())
        assert "lightStatus" not in result


class TestPoleRowToDictWithParents:
    def test_adds_project_and_customer_id_to_the_shared_dict(self):
        row = (
            "proj1", "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0,
            "2026-07-31 08:00:00 -04:00", "CC-100", 7, "PROD-42", "jdoe",
            12.6, 12.4,
            8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
            True, False, False, False, False, False,
            89.0, 45.0, 0.0, "cust1",
        )
        result = m._pole_row_to_dict_with_parents(row)
        assert result["id"] == "pole1"
        assert result["projectId"] == "proj1"
        assert result["customerId"] == "cust1"
        assert result["lastUpdate"] == "2026-07-31 08:00:00 -04:00"  # full detail mode keeps this


class TestGetPoles:
    def test_unfiltered_uses_full_template_and_default_limit(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles()

        sql, period_type, rollup_period_type, limit = mock_cursor.execute.call_args.args
        assert "OUTER APPLY" in sql  # full template, not summary
        # LastKnown48Hours, not Last48Hours -- getPoles reuses
        # pole_vitals_api.py's own per-pole DETAIL query/period type
        # (_POLE_DETAIL_PERIOD_TYPE), not its rollup one
        # (_ROLLUP_PERIOD_TYPE) -- there's no rollup concept here at
        # all, only per-pole detail fields, same reasoning as
        # pole_vitals_api.py's own getPoleVitals "poles" list entries.
        assert period_type == "LastKnown48Hours"
        # EXCEPT isOnline specifically, which still reads
        # _ROLLUP_PERIOD_TYPE (Last48Hours) via the query's own second
        # PoleVitals join (rps_online) -- see _POLE_SUMMARY_SQL_TEMPLATE/
        # _POLE_DETAILS_SQL_TEMPLATE's own comments on rps_online for why.
        assert rollup_period_type == "Last48Hours"

    def test_summary_true_uses_summary_template_and_higher_limit(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(summary=True)

        sql, period_type, rollup_period_type, limit = mock_cursor.execute.call_args.args
        assert "OUTER APPLY" in sql  # now leaner (LastUpload only), but present
        assert limit == m._SUMMARY_MAX_LIMIT

    def test_custom_limit_respected_in_summary_mode(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(summary=True, limit=100)

        limit = mock_cursor.execute.call_args.args[3]
        assert limit == 100

    def test_pole_id_filters_by_id_and_returns_single_dict(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        row = (
            "proj1", "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0,
            "2026-07-31 08:00:00 -04:00", "CC-100", 7, "PROD-42", "jdoe",
            12.6, 12.4,
            8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
            True, False, False, False, False, False,
            89.0, 45.0, 0.0, "cust1",
        )
        mock_cursor.fetchall.return_value = [row]

        result = m.get_poles(pole_id="pole1")

        sql, period_type, rollup_period_type, pole_id = mock_cursor.execute.call_args.args
        assert "p.Id = ?" in sql
        assert pole_id == "pole1"
        assert isinstance(result, dict)
        assert result["id"] == "pole1"

    def test_nonexistent_pole_id_returns_none(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert m.get_poles(pole_id="does-not-exist") is None

    def test_project_id_without_pole_id_returns_a_list(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        result = m.get_poles(project_id="proj1")
        assert result == []

    def test_pole_id_and_project_id_combine_with_and(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(pole_id="pole1", project_id="proj1")

        sql, period_type, rollup_period_type, pole_id, project_id = mock_cursor.execute.call_args.args
        assert "p.Id = ? AND proj.Id = ?" in sql

    def test_pole_id_project_id_customer_id_all_combine(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(pole_id="pole1", project_id="proj1", customer_id="cust1")

        sql = mock_cursor.execute.call_args.args[0]
        assert "p.Id = ? AND proj.Id = ? AND c.Id = ?" in sql

    def test_summary_combined_with_pole_id_still_uses_summary_template(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        row = (
            "proj1", "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0,
            "2026-08-16 08:00:00 -04:00",
            True, False, False, False, False, False,
            89.0, 45.0, 0.0, "cust1",
        )
        mock_cursor.fetchall.return_value = [row]

        result = m.get_poles(pole_id="pole1", summary=True)

        sql = mock_cursor.execute.call_args.args[0]
        assert "PoleModels" not in sql  # still lean -- no PoleModels/BatteryChargingMin join
        assert result["id"] == "pole1"
        assert result["lastUpdate"] == "2026-08-16 08:00:00 -04:00"
        assert "batteryVoltage1" not in result

    def test_closes_cursor_and_connection(
        self, patch_get_connection_poles_api, mock_conn, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        m.get_poles()
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_closes_cursor_and_connection_even_on_failure(
        self, patch_get_connection_poles_api, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError):
            m.get_poles()
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
