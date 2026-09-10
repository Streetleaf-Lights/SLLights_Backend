"""Tests for shared/poles_api.py"""

import pytest
from freezegun import freeze_time

from shared import poles_api as m


class TestPoleSummarySqlStructure:
    """
    Regression guard for the summary query's own widening: it used to
    fetch ONLY pt.LastUpload from its OUTER APPLY, deliberately lean for
    ~14K-pole scale. Per explicit request, it now also fetches
    LampPower1/2, BatteryElecCurrent1/2, SolarBoardVoltage/
    SolarBoardElecCurrent, and IsDaylightForPanelFault -- the raw inputs
    needed to compute four of api_utils.compute_pole_status_labels()'s
    own five fields at summary scale too. batteryVoltage1/2 remain the
    one genuine holdout, since no calculated field depends on them.
    """

    def test_outer_apply_now_includes_the_status_label_inputs(self):
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        apply_block = sql.split("OUTER APPLY (")[1].split(") AS latest_pt")[0]
        for col in (
            "pt.LastUpload",
            "pt.LampPower1", "pt.LampPower2",
            "pt.BatteryElecCurrent1", "pt.BatteryElecCurrent2",
            "pt.SolarBoardVoltage", "pt.SolarBoardElecCurrent",
            "pt.IsDaylightForPanelFault",
        ):
            assert col in apply_block

    def test_battery_voltage_columns_still_excluded(self):
        """No calculated field depends on these -- still the one
        genuine holdout from the full-detail query's own richer set."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql

    def test_pole_time_zone_columns_added_for_sunset_time(self):
        """Needed for sunsetTime's own calculation, per explicit
        request -- sourced from the PoleTimeZones join already present
        for the lastUpdate conversion, not a new join. Distinct
        aliases (TimeZoneLatitude/TimeZoneLongitude), not Latitude/
        Longitude, since p.Lat/p.Long (Poles' own, less trusted
        coordinates) are already selected elsewhere in this same query
        under different names."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "ptz.Latitude AS TimeZoneLatitude" in sql
        assert "ptz.Longitude AS TimeZoneLongitude" in sql
        assert "ptz.IanaTimeZone AS IanaTimeZone" in sql

    def test_outer_apply_still_a_single_correlated_lookup(self):
        """Widened, not duplicated -- still exactly one seek into
        PoleTelemetry per pole, not a second OUTER APPLY added
        alongside the original lean one."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert sql.count("OUTER APPLY") == 1
        assert sql.count("FROM PoleTelemetry") == 1

    def test_is_open_issue_fault_reads_from_pole_open_issues_not_rps(self):
        """Same fix as pole_vitals_api.py's own _POLE_DETAILS_SQL_TEMPLATE:
        isOpenIssueFault must reflect whether a pole CURRENTLY has an
        open issue, independent of the Last48Hours join/telemetry
        recency -- never null, unlike every other fault field."""
        sql = m._POLE_SUMMARY_SQL_TEMPLATE
        assert "rps.IsOpenIssueFault" not in sql
        assert "SELECT 1 FROM PoleOpenIssues poi WHERE poi.PoleId = p.Id" in sql
        assert "THEN CAST(1 AS BIT) ELSE CAST(0 AS BIT) END AS IsOpenIssueFault" in sql


def _summary_row(
    project_id="proj1",
    pole_id="pole1",
    pole_number="PN-1",
    location_id="LOC-1",
    install_date="2025-01-01",
    lat=28.0,
    long_=-82.0,
    active=True,
    last_update="2026-08-26 20:00:00 -04:00",
    lamp_power_1=0,
    lamp_power_2=0,
    battery_elec_current_1=100,
    battery_elec_current_2=100,
    solar_board_voltage=18.0,
    solar_board_elec_current=2.0,
    is_daylight_for_panel_fault=1,
    timezone_latitude=28.2,
    timezone_longitude=-80.7,
    iana_timezone="America/New_York",
    is_online=True,
    is_led_fault=False,
    is_battery_fault=False,
    is_panel_fault=False,
    is_open_issue_fault=False,
    is_pole_fault=False,
    battery_percentage=89.0,
    panel_percentage=45.0,
    light_percentage=0.0,
    customer_id="cust1",
):
    """Matches _POLE_SUMMARY_SQL_TEMPLATE's own column order exactly."""
    return (
        project_id, pole_id, pole_number, location_id, install_date, lat, long_,
        active,
        last_update,
        lamp_power_1, lamp_power_2, battery_elec_current_1, battery_elec_current_2,
        solar_board_voltage, solar_board_elec_current, is_daylight_for_panel_fault,
        timezone_latitude, timezone_longitude, iana_timezone,
        is_online, is_led_fault, is_battery_fault, is_panel_fault, is_open_issue_fault, is_pole_fault,
        battery_percentage, panel_percentage, light_percentage, customer_id,
    )


class TestSummaryRowToDict:
    def test_maps_every_base_field_correctly(self):
        with freeze_time("2026-08-28 12:00:00"):
            row = _summary_row()
            result = m._summary_row_to_dict(row)
        assert result["id"] == "pole1"
        assert result["poleNumber"] == "PN-1"
        assert result["locationId"] == "LOC-1"
        assert result["installDate"] == "2025-01-01"
        assert result["lat"] == 28.0
        assert result["long"] == -82.0
        assert result["active"] is True
        assert result["lastUpdate"] == "2026-08-26 20:00:00 -04:00"
        assert result["isOnline"] is True
        assert result["isLedFault"] is False
        assert result["isPanelFault"] is False
        assert result["avgBatteryPercentage"] == 89.0
        assert result["avgPanelPercentage"] == 45.0
        assert result["avgLightPercentage"] == 0.0
        assert result["sunsetTime"] == "2026-08-28 19:47:58.596527-04:00"
        assert result["projectId"] == "proj1"
        assert result["customerId"] == "cust1"

    def test_no_battery_voltage_keys_at_all(self):
        """Never fetched by this query -- must not appear in the
        returned dict under any name."""
        result = m._summary_row_to_dict(_summary_row())
        assert "batteryVoltage1" not in result
        assert "batteryVoltage2" not in result

    def test_includes_the_four_status_labels_matching_the_reported_example(self):
        """Matches the exact example values reported: OFF / Charging /
        null / Full."""
        row = _summary_row(
            lamp_power_1=0, lamp_power_2=0,
            battery_elec_current_1=100, battery_elec_current_2=100,
            solar_board_voltage=18.0, solar_board_elec_current=2.0,
            is_daylight_for_panel_fault=1,
        )
        with freeze_time("2026-08-27 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["lightStatusLabel"] == "OFF"
        assert result["panelStatusLabel"] == "Charging"
        assert result["panelIdleReason"] is None
        assert result["batteryStatusLabel"] == "Full"

    def test_electric_current_average_is_not_included_at_all(self):
        """The one field of the five deliberately dropped from summary
        mode's own output, per explicit request -- not present under
        any key, not even as None."""
        result = m._summary_row_to_dict(_summary_row())
        assert "electricCurrentAverage" not in result

    def test_connected_label_and_overall_status_label_present(self):
        """Per explicit request: summary mode gets the same
        connectedLabel/overallStatusLabel fields getPoleVitals already
        has."""
        row = _summary_row()
        with freeze_time("2026-08-27 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["connectedLabel"] == "Online"
        assert result["overallStatusLabel"] == "OK"

    def test_overall_status_label_not_reporting_when_no_telemetry(self):
        row = _summary_row(last_update=None)
        result = m._summary_row_to_dict(row)
        assert result["overallStatusLabel"] == "Not Reporting"
        assert result["connectedLabel"] == "Online"  # is_online=True default, independent of lastUpdate

    def test_stale_last_update_nulls_fault_fields_but_not_open_issue_fault(self):
        """Same staleness override as pole_vitals_api.py's own
        _pole_row_to_dict() -- applied here too, per explicit request,
        so summary mode doesn't silently drift from getPoleVitals'
        behavior."""
        row = _summary_row(
            last_update="2026-08-01 08:00:00 -04:00",  # >48h before frozen time below
            is_led_fault=True, is_battery_fault=True, is_panel_fault=True,
            is_open_issue_fault=True, is_pole_fault=True,
        )
        with freeze_time("2026-08-28 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["isLedFault"] is None
        assert result["isBatteryFault"] is None
        assert result["isPanelFault"] is None
        assert result["isPoleFault"] is None
        assert result["isOpenIssueFault"] is True
        assert result["lightStatusLabel"] == "Not Reporting 48H"
        assert result["panelStatusLabel"] == "Not Reporting 48H"
        assert result["batteryStatusLabel"] == "Not Reporting 48H"
        assert result["overallStatusLabel"] == "Not Reporting 48H"

    def test_stale_last_update_leaves_avg_percentages_untouched(self):
        """avgBatteryPercentage/avgPanelPercentage/avgLightPercentage
        come from the PoleVitals rollup row directly (rps), not from
        raw PoleTelemetry -- they're NOT part of this staleness
        override (same scope as pole_vitals_api.py's own version)."""
        row = _summary_row(
            last_update="2026-08-01 08:00:00 -04:00",
            battery_percentage=42.0, panel_percentage=33.0, light_percentage=0.0,
        )
        with freeze_time("2026-08-28 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["avgBatteryPercentage"] == 42.0
        assert result["avgPanelPercentage"] == 33.0
        assert result["avgLightPercentage"] == 0.0
        # is_daylight_for_panel_fault is NOT nulled by this override
        # (same scope as pole_vitals_api.py's own version), so with
        # solar_board_voltage/elec_current now nulled but that flag
        # still present, compute_pole_status_labels() legitimately
        # returns "N/A" here rather than None -- not a bug.
        assert result["panelIdleReason"] == "N/A"

    def test_fresh_last_update_leaves_fault_fields_untouched(self):
        row = _summary_row(
            last_update="2026-08-27 10:00:00 -04:00",
            is_led_fault=True, is_battery_fault=False, is_panel_fault=True,
            is_open_issue_fault=False, is_pole_fault=True,
        )
        with freeze_time("2026-08-28 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["isLedFault"] is True
        assert result["isBatteryFault"] is False
        assert result["isPanelFault"] is True
        assert result["isPoleFault"] is True

    def test_status_labels_computed_even_though_battery_voltage_is_absent(self):
        """Confirms the four status labels don't accidentally depend on
        batteryVoltage1/2 (which this row shape never has) -- they're
        computed purely from lampPower/batteryElecCurrent/solarBoard
        inputs, all of which ARE present here."""
        row = _summary_row(lamp_power_1=5.0, lamp_power_2=0)
        with freeze_time("2026-08-27 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["lightStatusLabel"] == "ON"

    def test_no_telemetry_gives_not_reporting_status_labels(self):
        """Updated per the same staleness-override behavior now applied
        here as in pole_vitals_api.py's own _pole_row_to_dict(): no
        telemetry at all means "Not Reporting", not None."""
        row = _summary_row(
            last_update=None,
            lamp_power_1=None, lamp_power_2=None,
            battery_elec_current_1=None, battery_elec_current_2=None,
            solar_board_voltage=None, solar_board_elec_current=None,
            is_daylight_for_panel_fault=None,
        )
        result = m._summary_row_to_dict(row)
        assert result["lastUpdate"] is None
        assert result["lightStatusLabel"] == "Not Reporting"
        assert result["panelStatusLabel"] == "Not Reporting"
        assert result["panelIdleReason"] is None
        assert result["batteryStatusLabel"] == "Not Reporting"
        assert "electricCurrentAverage" not in result

    def test_panel_idle_reason_only_populated_when_status_is_idle(self):
        row = _summary_row(
            solar_board_voltage=0, solar_board_elec_current=0,  # Idle
            is_daylight_for_panel_fault=0,
        )
        with freeze_time("2026-08-27 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["panelStatusLabel"] == "Idle"
        assert result["panelIdleReason"] == "Sundown"

    def test_sunset_time_independent_of_telemetry_state(self):
        """Unlike the four status labels (gated by has_telemetry, i.e.
        last_update is not None), sunsetTime depends only on
        TimeZoneLatitude/TimeZoneLongitude/IanaTimeZone -- a pole with
        NO recent telemetry at all still has a real, computable sunset
        for its own location, so this must NOT come back None just
        because last_update is None."""
        row = _summary_row(
            last_update=None,
            lamp_power_1=None, lamp_power_2=None,
            battery_elec_current_1=None, battery_elec_current_2=None,
            solar_board_voltage=None, solar_board_elec_current=None,
            is_daylight_for_panel_fault=None,
        )
        with freeze_time("2026-08-28 12:00:00"):
            result = m._summary_row_to_dict(row)
        assert result["lastUpdate"] is None
        assert result["lightStatusLabel"] == "Not Reporting"  # telemetry-gated field, per the new behavior
        assert result["sunsetTime"] == "2026-08-28 19:47:58.596527-04:00"

    def test_sunset_time_none_when_pole_time_zone_coordinates_missing(self):
        """No PoleTimeZones row resolved for this pole at all -- nothing
        to compute a sunset from, regardless of telemetry state."""
        row = _summary_row(timezone_latitude=None, timezone_longitude=None)
        result = m._summary_row_to_dict(row)
        assert result["sunsetTime"] is None

    def test_sunset_time_falls_back_to_eastern_when_iana_zone_missing(self):
        """Coordinates present, but no resolved IANA zone (e.g. outside
        timezone_utils.py's deliberately US-scoped mapping) -- still a
        real, computable sunset, just displayed in the project's
        established Eastern-time fallback rather than coming back None."""
        with freeze_time("2026-08-28 12:00:00"):
            with_iana = m._summary_row_to_dict(_summary_row(iana_timezone="America/New_York"))
            without_iana = m._summary_row_to_dict(_summary_row(iana_timezone=None))
        assert without_iana["sunsetTime"] == with_iana["sunsetTime"]


class TestGetPolesSummaryMode:
    def test_summary_true_uses_the_summary_template_and_row_mapper(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [_summary_row()]

        with freeze_time("2026-08-27 12:00:00"):
            result = m.get_poles(summary=True)

        assert len(result) == 1
        assert result[0]["lightStatusLabel"] == "OFF"
        assert "electricCurrentAverage" not in result[0]
        assert "batteryVoltage1" not in result[0]

        executed_sql = mock_cursor.execute.call_args.args[0]
        assert "pt.LampPower1" in executed_sql  # confirms the widened summary template was used

    def test_summary_false_uses_the_full_detail_template(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(summary=False)

        executed_sql = mock_cursor.execute.call_args.args[0]
        assert executed_sql != m._POLE_SUMMARY_SQL_TEMPLATE.format(where_clause="WHERE 1=1")
        assert "BatteryVoltage1" in executed_sql  # only the full-detail template fetches this


class TestGetPolesActiveFilter:
    def test_active_true_unfiltered_adds_where_clause(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(active=True)

        executed_sql, *params = mock_cursor.execute.call_args.args
        assert "AND p.Active = ?" in executed_sql
        assert params[-1] == 1

    def test_active_false_unfiltered_binds_zero(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(active=False)

        _, *params = mock_cursor.execute.call_args.args
        assert params[-1] == 0

    def test_active_none_unfiltered_omits_where_clause(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles()

        executed_sql = mock_cursor.execute.call_args.args[0]
        assert "AND p.Active" not in executed_sql
        assert "WHERE p.Active" not in executed_sql

    def test_active_combined_with_project_id_adds_condition(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(project_id="proj1", active=True)

        executed_sql, *params = mock_cursor.execute.call_args.args
        assert "proj.Id = ?" in executed_sql
        assert "p.Active = ?" in executed_sql
        assert params[-1] == 1
        assert "proj1" in params

    def test_active_combined_with_customer_id_adds_condition(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(customer_id="cust1", active=False)

        executed_sql, *params = mock_cursor.execute.call_args.args
        assert "c.Id = ?" in executed_sql
        assert "p.Active = ?" in executed_sql
        assert params[-1] == 0

    def test_active_is_ignored_when_pole_id_given(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(pole_id="pole1", active=True)

        executed_sql = mock_cursor.execute.call_args.args[0]
        assert "AND p.Active" not in executed_sql
        assert "p.Active = ?" not in executed_sql

    def test_active_combined_with_summary_mode(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        m.get_poles(summary=True, active=True)

        executed_sql, *params = mock_cursor.execute.call_args.args
        assert "AND p.Active = ?" in executed_sql
        assert params[-1] == 1
