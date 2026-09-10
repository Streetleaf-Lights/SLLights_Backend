"""
Tests for get_pole_vitals_by_period() in shared/pole_vitals_api.py.

Uses the same patch_get_connection_pole_vitals_api fixture already
defined in tests/conftest.py for the rest of shared/pole_vitals_api.py's
tests.

Rewritten to match the current shape of this endpoint: 'Hour' is the
ONLY valid period_type (Day/Week/Month/Last48Hours/LastKnown48Hours all
rejected), and the Hour history is a generated, gap-filled, contiguous
sequence of exactly `limit` hourly buckets anchored to the CURRENT
moment (SYSDATETIMEOFFSET()) -- NOT to the pole's own last reading, per
explicit request/correction reversing this endpoint's earlier behavior.
A genuinely missing hour still gets its own entry, with periodStart/
periodEnd populated and every other field null, rather than being
silently omitted.
"""

import pytest

from shared import pole_vitals_api


def _pole_info_row(
    pole_id, pole_number, location_id,
    install_date=None, lat=None, long_=None, last_update=None,
    lamp_power_1=None, lamp_power_2=None,
    battery_elec_current_1=None, battery_elec_current_2=None,
    solar_board_voltage=None, solar_board_elec_current=None,
):
    """Matches _POLE_INFO_FOR_HISTORY_SQL_TEMPLATE's own column order."""
    return (
        pole_id, pole_number, location_id, install_date, lat, long_, last_update,
        lamp_power_1, lamp_power_2, battery_elec_current_1, battery_elec_current_2,
        solar_board_voltage, solar_board_elec_current,
    )


def _vitals_row(
    period_start, period_end,
    is_online=None, is_led_fault=None, is_battery_fault=None, is_panel_fault=None,
    is_open_issue_fault=None, is_pole_fault=None,
    battery_percentage=None, panel_percentage=None, light_percentage=None,
):
    """Matches _POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE's own SELECT column
    order -- one row per generated bucket, whether or not a real
    PoleVitals row matched it (a gap bucket comes back with every field
    past periodStart/periodEnd as None, exactly like this helper's own
    defaults)."""
    return (
        period_start, period_end, is_online, is_led_fault, is_battery_fault,
        is_panel_fault, is_open_issue_fault, is_pole_fault,
        battery_percentage, panel_percentage, light_percentage,
    )


class TestPoleVitalsHistorySqlStructure:
    def test_pole_info_query_has_no_battery_voltage_columns(self):
        """batteryVoltage1/batteryVoltage2 were dropped from this
        endpoint entirely per an earlier explicit request -- along with
        the PoleTelemetry join that would otherwise be needed to get
        them, this query should only need PoleTelemetry for LastUpload
        and the other latest-reading fields it does return."""
        sql = pole_vitals_api._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql
        assert "LastUpload" in sql

    def test_pole_info_query_uses_a_single_outer_apply(self):
        sql = pole_vitals_api._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert sql.count("OUTER APPLY") == 1
        assert "CROSS APPLY" not in sql

    def test_hour_history_query_has_no_rollup_aggregation(self):
        """Each entry is a direct read of one PoleVitals row (or NULLs,
        via the LEFT JOIN, for a gap) -- no GROUP BY, no MAX(CASE...)
        priority logic, no AVG() across rows."""
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "GROUP BY" not in sql
        assert "MAX(CASE" not in sql
        assert "AVG(" not in sql

    def test_hour_history_query_includes_period_start_and_end(self):
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "PeriodStart" in sql
        assert "PeriodEnd" in sql

    def test_hour_history_query_orders_most_recent_first(self):
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "ORDER BY b.BucketStart DESC" in sql

    def test_hour_history_query_generates_buckets_not_a_top_clause(self):
        """Row count is controlled by the generated Numbers/Buckets
        sequence, not a TOP (?) filter on top of PoleVitals' own rows --
        this is what makes gap-filling possible at all."""
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "TOP (?)" not in sql
        assert "Numbers" in sql
        assert "OPTION (MAXRECURSION 0)" in sql

    def test_hour_history_query_anchors_to_current_moment_not_last_reading(self):
        """The core reversal from this endpoint's earlier behavior --
        SYSDATETIMEOFFSET(), not a MAX(PoleTelemetry.LastUpload)
        subquery."""
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "SYSDATETIMEOFFSET()" in sql
        assert "MAX(pt.LastUpload)" not in sql
        assert "MaxLastUpload" not in sql

    def test_hour_history_query_left_joins_pole_vitals(self):
        """LEFT JOIN, not JOIN/INNER JOIN -- a generated bucket with no
        matching PoleVitals row must still survive into the result,
        with every PoleVitals-sourced column NULL."""
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "LEFT JOIN PoleVitals" in sql

    def test_hour_history_query_filters_by_pole_id_and_period_type(self):
        sql = pole_vitals_api._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "p.Id = ?" in sql
        assert "pv.PeriodType = 'Hour'" in sql


class TestGetPoleVitalsByPeriod:
    def test_rejects_invalid_period_type_without_querying_the_database(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        with pytest.raises(ValueError, match="Hour"):
            pole_vitals_api.get_pole_vitals_by_period("pole1", "Week")
        mock_cursor.execute.assert_not_called()

    def test_rejects_removed_period_types_specifically(self):
        """Day/Week/Month/Last48Hours/LastKnown48Hours are all rejected
        the same as any other invalid value -- 'Hour' is the only
        period_type this endpoint has ever validly supported for
        history."""
        for removed in ("Day", "Week", "Month", "Last48Hours", "LastKnown48Hours"):
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

    def test_pole_with_zero_vitals_history_still_returns_full_bucket_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """A real pole with literally no PoleVitals rows at all still
        gets a FULL `limit`-length "vitals" list -- every entry is a
        generated gap bucket, not an empty list. This is the core
        behavior change: the old version would have returned []."""
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = [
            _vitals_row(f"hour{i}-start", f"hour{i}-end") for i in range(5)
        ]

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour", limit=5)

        assert result["id"] == "pole1"
        assert len(result["vitals"]) == 5
        for entry in result["vitals"]:
            assert entry["periodStart"] is not None
            assert entry["periodEnd"] is not None
            assert entry["isOnline"] is None
            assert entry["avgBatteryPercentage"] is None

    def test_pole_with_no_telemetry_yet_has_null_last_update(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = [_vitals_row("h-start", "h-end")]

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")

        assert result["lastUpdate"] is None

    def test_full_history_maps_correctly_and_does_not_include_battery_voltage(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row(
            "recg1jYzmCtPB170y", "PAS-4938", "12101-4938",
            install_date="2025-08-28", lat=28.3031566, long_=-82.2750467,
            last_update="2026-07-30 12:39:41 +00:00",
        )
        mock_cursor.fetchall.return_value = [
            _vitals_row(
                "2026-07-30 11:00:00 +00:00", "2026-07-30 12:00:00 +00:00",
                is_online=True, is_led_fault=False, is_battery_fault=False,
                is_panel_fault=False, is_open_issue_fault=False, is_pole_fault=False,
                battery_percentage=89.71, panel_percentage=2.77, light_percentage=17.77,
            ),
            _vitals_row(
                "2026-07-30 10:00:00 +00:00", "2026-07-30 11:00:00 +00:00",
                is_online=True, is_led_fault=False, is_battery_fault=False,
                is_panel_fault=False, is_open_issue_fault=False, is_pole_fault=False,
                battery_percentage=88.5, panel_percentage=45.2, light_percentage=0.0,
            ),
        ]

        result = pole_vitals_api.get_pole_vitals_by_period("recg1jYzmCtPB170y", "Hour")

        assert result["id"] == "recg1jYzmCtPB170y"
        assert result["poleNumber"] == "PAS-4938"
        assert result["lastUpdate"] == "2026-07-30 12:39:41 +00:00"
        assert "batteryVoltage1" not in result
        assert "batteryVoltage2" not in result
        assert len(result["vitals"]) == 2
        assert result["vitals"][0] == {
            "periodStart": "2026-07-30 11:00:00 +00:00",
            "periodEnd": "2026-07-30 12:00:00 +00:00",
            "isOnline": True,
            "isLedFault": False,
            "isBatteryFault": False,
            "isPanelFault": False,
            "isOpenIssueFault": False,
            "isPoleFault": False,
            "avgBatteryPercentage": 89.71,
            "avgPanelPercentage": 2.77,
            "avgLightPercentage": 17.77,
        }
        assert "batteryVoltage1" not in result["vitals"][0]

    def test_gap_bucket_has_populated_boundaries_but_null_everything_else(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """The core new behavior: a bucket with no matching PoleVitals
        row (simulating the LEFT JOIN's own NULL-for-no-match result)
        still has real periodStart/periodEnd -- generated from the
        bucket sequence itself, not read from a nonexistent PoleVitals
        row -- with every other field null."""
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = [
            _vitals_row(
                "2026-08-28 12:00:00 -04:00", "2026-08-28 13:00:00 -04:00",
                is_online=True, battery_percentage=90.0,
            ),
            _vitals_row("2026-08-28 11:00:00 -04:00", "2026-08-28 12:00:00 -04:00"),  # gap
        ]

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour", limit=2)

        gap_entry = result["vitals"][1]
        assert gap_entry["periodStart"] == "2026-08-28 11:00:00 -04:00"
        assert gap_entry["periodEnd"] == "2026-08-28 12:00:00 -04:00"
        assert gap_entry["isOnline"] is None
        assert gap_entry["isLedFault"] is None
        assert gap_entry["isBatteryFault"] is None
        assert gap_entry["isPanelFault"] is None
        assert gap_entry["isOpenIssueFault"] is None
        assert gap_entry["isPoleFault"] is None
        assert gap_entry["avgBatteryPercentage"] is None
        assert gap_entry["avgPanelPercentage"] is None
        assert gap_entry["avgLightPercentage"] is None

    def test_entries_are_distinguishable_by_period_boundaries(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = [
            _vitals_row("hour2-start", "hour2-end", is_online=False),
            _vitals_row("hour1-start", "hour1-end", is_online=True),
        ]

        result = pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")

        assert result["vitals"][0]["periodStart"] == "hour2-start"
        assert result["vitals"][1]["periodStart"] == "hour1-start"
        assert result["vitals"][0]["isOnline"] != result["vitals"][1]["isOnline"]

    def test_default_limit_is_applied_when_not_specified(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")

        history_call = mock_cursor.execute.call_args_list[1]
        bound_limit = history_call.args[2]
        assert bound_limit is not None and bound_limit > 0

    def test_custom_limit_is_passed_through(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour", limit=5)

        history_call = mock_cursor.execute.call_args_list[1]
        assert history_call.args[2] == 5

    def test_history_query_bound_params_are_pole_id_then_limit_only(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """Only TWO bound params now (pole_id, then limit ONCE) -- no
        more double-binding limit for both a TOP (?) and a separate
        DATEADD window bound, since the generated bucket sequence itself
        controls row count."""
        mock_cursor.fetchone.return_value = _pole_info_row("pole1", "PN-001", "LOC-001")
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour", limit=10)

        history_call = mock_cursor.execute.call_args_list[1]
        _, pole_id, limit = history_call.args
        assert (pole_id, limit) == ("pole1", 10)
