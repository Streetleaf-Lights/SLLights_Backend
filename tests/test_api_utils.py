"""Tests for shared/api_utils.py"""

import pytest

from shared import api_utils


class TestClampLimit:
    def test_none_returns_default(self):
        assert api_utils.clamp_limit(None) == api_utils.DEFAULT_LIMIT

    def test_zero_returns_default(self):
        """0 is falsy in Python -- treated the same as None/not-given,
        not as "return zero rows"."""
        assert api_utils.clamp_limit(0) == api_utils.DEFAULT_LIMIT

    def test_within_range_passes_through(self):
        assert api_utils.clamp_limit(50) == 50

    def test_above_max_is_capped(self):
        assert api_utils.clamp_limit(api_utils.MAX_LIMIT + 500) == api_utils.MAX_LIMIT

    def test_negative_is_floored_to_one(self):
        assert api_utils.clamp_limit(-10) == 1

    def test_string_digit_is_coerced_to_int(self):
        assert api_utils.clamp_limit("50") == 50


class TestJsonSafe:
    def test_none_passes_through(self):
        assert api_utils.json_safe(None) is None

    def test_str_int_float_bool_pass_through_unchanged(self):
        assert api_utils.json_safe("hello") == "hello"
        assert api_utils.json_safe(42) == 42
        assert api_utils.json_safe(3.14) == 3.14
        assert api_utils.json_safe(True) is True

    def test_unrecognized_type_is_stringified(self):
        import datetime

        value = datetime.datetime(2026, 1, 1, 12, 0, 0)
        result = api_utils.json_safe(value)
        assert isinstance(result, str)
        assert result == str(value)


class TestComputePoleStatusLabels:
    """
    Direct unit tests for the five calculated fields, per explicit
    request -- moved here from test_pole_vitals_api.py once this
    function itself moved to api_utils.py (poles_api.py's own summary
    mode needed the exact same logic, not a duplicate copy of it).
    """

    def _labels(
        self,
        has_telemetry=True,
        lamp_power_1=0,
        lamp_power_2=0,
        battery_elec_current_1=0,
        battery_elec_current_2=0,
        solar_board_voltage=0,
        solar_board_elec_current=0,
        is_daylight_for_panel_fault=1,
    ):
        return api_utils.compute_pole_status_labels(
            has_telemetry, lamp_power_1, lamp_power_2,
            battery_elec_current_1, battery_elec_current_2,
            solar_board_voltage, solar_board_elec_current,
            is_daylight_for_panel_fault,
        )

    def test_no_telemetry_at_all_gives_all_five_none(self):
        result = self._labels(has_telemetry=False)
        assert result == {
            "lightStatusLabel": None,
            "panelStatusLabel": None,
            "panelIdleReason": None,
            "batteryStatusLabel": None,
            "electricCurrentAverage": None,
        }

    # -- lightStatusLabel --

    def test_light_status_on_when_lamp_power_sum_positive(self):
        assert self._labels(lamp_power_1=5.0, lamp_power_2=0)["lightStatusLabel"] == "ON"

    def test_light_status_off_when_lamp_power_sum_zero(self):
        assert self._labels(lamp_power_1=0, lamp_power_2=0)["lightStatusLabel"] == "OFF"

    def test_light_status_treats_a_null_individual_reading_as_zero(self):
        assert self._labels(lamp_power_1=None, lamp_power_2=3.0)["lightStatusLabel"] == "ON"
        assert self._labels(lamp_power_1=None, lamp_power_2=0)["lightStatusLabel"] == "OFF"

    # -- panelStatusLabel --

    def test_panel_status_charging_when_product_positive(self):
        result = self._labels(solar_board_voltage=18.0, solar_board_elec_current=2.0)
        assert result["panelStatusLabel"] == "Charging"

    def test_panel_status_idle_when_either_factor_zero(self):
        assert self._labels(solar_board_voltage=0, solar_board_elec_current=2.0)["panelStatusLabel"] == "Idle"
        assert self._labels(solar_board_voltage=18.0, solar_board_elec_current=0)["panelStatusLabel"] == "Idle"

    # -- panelIdleReason --

    def test_panel_idle_reason_sundown_when_not_daylight(self):
        result = self._labels(is_daylight_for_panel_fault=0, battery_elec_current_1=50, battery_elec_current_2=50)
        assert result["panelIdleReason"] == "Sundown"

    def test_panel_idle_reason_battery_full_when_daylight_and_current_sum_200(self):
        result = self._labels(
            is_daylight_for_panel_fault=1, battery_elec_current_1=100, battery_elec_current_2=100
        )
        assert result["panelIdleReason"] == "Battery Full"

    def test_panel_idle_reason_na_when_daylight_and_current_sum_not_200(self):
        result = self._labels(
            is_daylight_for_panel_fault=1, battery_elec_current_1=50, battery_elec_current_2=50
        )
        assert result["panelIdleReason"] == "N/A"

    def test_panel_idle_reason_null_daylight_falls_through_to_battery_check(self):
        """NULL is not equal to 0 -- a genuinely unknown daylight state
        must NOT be treated as "Sundown"."""
        result = self._labels(
            is_daylight_for_panel_fault=None, battery_elec_current_1=100, battery_elec_current_2=100
        )
        assert result["panelIdleReason"] == "Battery Full"

        result = self._labels(
            is_daylight_for_panel_fault=None, battery_elec_current_1=50, battery_elec_current_2=50
        )
        assert result["panelIdleReason"] == "N/A"

    def test_panel_idle_reason_is_none_when_panel_status_is_charging(self):
        """Only computed when panelStatusLabel is actually "Idle" -- a
        panel that's actively charging has no "idle reason" at all,
        even if IsDaylightForPanelFault or the battery-current sum
        would otherwise satisfy one of the idle conditions."""
        result = self._labels(
            solar_board_voltage=18.0, solar_board_elec_current=2.0,
            is_daylight_for_panel_fault=0,
        )
        assert result["panelStatusLabel"] == "Charging"
        assert result["panelIdleReason"] is None

        result = self._labels(
            solar_board_voltage=18.0, solar_board_elec_current=2.0,
            battery_elec_current_1=100, battery_elec_current_2=100,
        )
        assert result["panelStatusLabel"] == "Charging"
        assert result["panelIdleReason"] is None

    def test_panel_idle_reason_is_computed_when_panel_status_is_idle(self):
        result = self._labels(
            solar_board_voltage=0, solar_board_elec_current=0,
            is_daylight_for_panel_fault=0,
        )
        assert result["panelStatusLabel"] == "Idle"
        assert result["panelIdleReason"] == "Sundown"

    # -- batteryStatusLabel --

    def test_battery_status_full_when_current_sum_200(self):
        result = self._labels(battery_elec_current_1=100, battery_elec_current_2=100, lamp_power_1=5.0)
        assert result["batteryStatusLabel"] == "Full"

    def test_battery_status_discharging_when_not_full_and_lamp_on(self):
        result = self._labels(battery_elec_current_1=50, battery_elec_current_2=50, lamp_power_1=5.0)
        assert result["batteryStatusLabel"] == "Discharging"

    def test_battery_status_charging_when_not_full_and_lamp_off(self):
        result = self._labels(battery_elec_current_1=50, battery_elec_current_2=50, lamp_power_1=0, lamp_power_2=0)
        assert result["batteryStatusLabel"] == "Charging"

    def test_battery_status_full_takes_priority_over_discharging(self):
        """Full is checked BEFORE the lamp-on check, per the requested
        ordering -- a fully-charged battery reports Full even if the
        lamp also happens to be on."""
        result = self._labels(battery_elec_current_1=100, battery_elec_current_2=100, lamp_power_1=5.0)
        assert result["batteryStatusLabel"] == "Full"

    # -- electricCurrentAverage --

    def test_electric_current_average_is_the_mean_of_the_two_readings(self):
        result = self._labels(battery_elec_current_1=30.0, battery_elec_current_2=40.0)
        assert result["electricCurrentAverage"] == 35.0

    def test_electric_current_average_treats_a_null_individual_reading_as_zero(self):
        result = self._labels(battery_elec_current_1=None, battery_elec_current_2=40.0)
        assert result["electricCurrentAverage"] == 20.0

    def test_electric_current_average_not_rounded(self):
        result = self._labels(battery_elec_current_1=1.0, battery_elec_current_2=2.0)
        assert result["electricCurrentAverage"] == 1.5
