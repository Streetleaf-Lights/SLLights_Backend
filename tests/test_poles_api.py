"""Tests for shared/poles_api.py"""

import pytest

from shared import api_utils, poles_api


def _pole_row(
    project_id, pole_id, pole_number, location_id,
    install_date=None, lat=None, long_=None,
    last_update=None, battery_voltage_1=None, battery_voltage_2=None,
    light_status=None, is_online=None,
    battery_percentage=None, panel_percentage=None, light_percentage=None,
    customer_id=None,
):
    return (
        project_id, pole_id, pole_number, location_id,
        install_date, lat, long_,
        last_update, battery_voltage_1, battery_voltage_2,
        light_status, is_online,
        battery_percentage, panel_percentage, light_percentage,
        customer_id,
    )


class TestPoleRowToDictWithParents:
    def test_adds_project_id_and_customer_id_alongside_every_field_pole_vitals_api_already_produces(self):
        row = _pole_row(
            "proj1", "pole1", "PN-001", "LOC-001",
            install_date="2023-05-10", lat=33.749, long_=-84.388,
            last_update="2026-07-25 14:30:00", battery_voltage_1=12.6, battery_voltage_2=12.4,
            light_status="Working", is_online=True,
            battery_percentage=87.5, panel_percentage=92.1, light_percentage=88.0,
            customer_id="cust1",
        )

        result = poles_api._pole_row_to_dict_with_parents(row)

        assert result == {
            "id": "pole1",
            "poleNumber": "PN-001",
            "locationId": "LOC-001",
            "installDate": "2023-05-10",
            "lat": 33.749,
            "long": -84.388,
            "lastUpdate": "2026-07-25 14:30:00",
            "batteryVoltage1": 12.6,
            "batteryVoltage2": 12.4,
            "lightStatus": "Working",
            "isOnline": True,
            "avgBatteryPercentage": 87.5,
            "avgPanelPercentage": 92.1,
            "avgLightPercentage": 88.0,
            "projectId": "proj1",
            "customerId": "cust1",
        }

    def test_unclassified_pole_still_gets_a_real_project_id(self):
        """A pole can be fully unclassified (no PoleVitals, no
        PoleTelemetry) and still have a real, non-null projectId --
        that's independent of any telemetry/vitals data existing."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001", customer_id="cust1")
        result = poles_api._pole_row_to_dict_with_parents(row)
        assert result["projectId"] == "proj1"
        assert result["lightStatus"] is None

    def test_unclassified_pole_still_gets_a_real_customer_id(self):
        """Same reasoning as projectId -- customerId is independent of
        any telemetry/vitals data existing for that pole."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001", customer_id="cust1")
        result = poles_api._pole_row_to_dict_with_parents(row)
        assert result["customerId"] == "cust1"


class TestGetPolesUnfiltered:
    def test_no_params_queries_top_n_poles_ordered_by_pole_number(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles()

        sql, period_type, limit = mock_cursor.execute.call_args.args
        assert "WHERE p.Id IN (SELECT TOP (?) Id FROM Poles ORDER BY PoleNumber)" in sql
        assert period_type == "Hour"
        assert limit == api_utils.DEFAULT_LIMIT

    def test_custom_limit_is_clamped_and_passed_through(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(limit=99999)

        _, _, limit = mock_cursor.execute.call_args.args
        assert limit == api_utils.MAX_LIMIT

    def test_returns_a_list_of_pole_dicts_with_project_and_customer_id(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            _pole_row("proj1", "pole1", "PN-001", "LOC-001", light_status="Working", customer_id="cust1"),
            _pole_row("proj2", "pole2", "PN-002", "LOC-002", light_status="Not Working", customer_id="cust2"),
        ]

        result = poles_api.get_poles()

        assert len(result) == 2
        assert result[0]["id"] == "pole1"
        assert result[0]["projectId"] == "proj1"
        assert result[0]["customerId"] == "cust1"
        assert result[1]["id"] == "pole2"
        assert result[1]["projectId"] == "proj2"
        assert result[1]["customerId"] == "cust2"

    def test_no_matches_returns_empty_list(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert poles_api.get_poles() == []


class TestGetPolesPoleIdFilter:
    def test_pole_id_filters_by_pole_id_column(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(pole_id="pole1")

        sql, period_type, pole_id = mock_cursor.execute.call_args.args
        # The SQL legitimately contains multiple WHEREs of its own (the
        # RecentPoleStats CTE, the OUTER APPLY subquery) -- what matters
        # is the outer where_clause this function builds, not a naive
        # total count.
        assert sql.rstrip().endswith("WHERE p.Id = ?\nORDER BY proj.Id, p.PoleNumber")
        assert period_type == "Hour"
        assert pole_id == "pole1"

    def test_returns_a_single_dict_not_a_list(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            _pole_row("proj1", "pole1", "PN-001", "LOC-001", light_status="Working"),
        ]

        result = poles_api.get_poles(pole_id="pole1")

        assert isinstance(result, dict)
        assert result["id"] == "pole1"

    def test_nonexistent_pole_returns_none(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert poles_api.get_poles(pole_id="does-not-exist") is None

    def test_limit_is_ignored_when_pole_id_given(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(pole_id="pole1", limit=5)

        args = mock_cursor.execute.call_args.args
        assert len(args) == 3  # sql, period_type, pole_id -- no limit param bound


class TestGetPolesProjectIdFilter:
    def test_project_id_alone_filters_by_project_id_column(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(project_id="proj1")

        sql, period_type, project_id = mock_cursor.execute.call_args.args
        assert "proj.Id = ?" in sql
        assert "p.Id = ?" not in sql
        assert period_type == "Hour"
        assert project_id == "proj1"

    def test_project_id_alone_returns_a_list_not_a_single_dict(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            _pole_row("proj1", "pole1", "PN-001", "LOC-001"),
            _pole_row("proj1", "pole2", "PN-002", "LOC-002"),
        ]

        result = poles_api.get_poles(project_id="proj1")

        assert isinstance(result, list)
        assert len(result) == 2

    def test_project_with_zero_poles_returns_empty_list_not_none(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        """A collection filter (like projects_api.get_projects()'s
        customerId), not a single-entity lookup -- "no poles here" is a
        valid empty list, not a 404-worthy None."""
        mock_cursor.fetchall.return_value = []
        assert poles_api.get_poles(project_id="proj-with-no-poles") == []


class TestGetPolesCustomerIdFilter:
    def test_customer_id_alone_filters_by_customer_id_column(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(customer_id="cust1")

        sql, period_type, customer_id = mock_cursor.execute.call_args.args
        assert "c.Id = ?" in sql
        assert period_type == "Hour"
        assert customer_id == "cust1"

    def test_customer_id_alone_returns_a_list(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            _pole_row("proj1", "pole1", "PN-001", "LOC-001"),
        ]
        result = poles_api.get_poles(customer_id="cust1")
        assert isinstance(result, list)

    def test_project_id_and_customer_id_combined_both_apply(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(project_id="proj1", customer_id="cust1")

        sql, period_type, project_id, customer_id = mock_cursor.execute.call_args.args
        assert "proj.Id = ? AND c.Id = ?" in sql
        assert period_type == "Hour"
        assert project_id == "proj1"
        assert customer_id == "cust1"


class TestGetPolesCombinedWithPoleId:
    """
    Real bug caught while writing these tests, not a hypothetical: the
    first version of get_poles() used an if/elif chain, meaning
    pole_id+project_id together silently ignored project_id entirely
    (used only pole_id for filtering) -- directly contradicting the
    function's own docstring, which claims poleId "can be combined with
    projectId and/or customerId to also verify the pole belongs to that
    project/customer". Fixed by building the WHERE clause from whichever
    conditions were actually given, combined with AND, rather than a
    mutually-exclusive chain.
    """

    def test_pole_id_and_project_id_combine_with_and(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(pole_id="pole1", project_id="proj1")

        sql, period_type, pole_id, project_id = mock_cursor.execute.call_args.args
        assert "p.Id = ? AND proj.Id = ?" in sql
        assert period_type == "Hour"
        assert pole_id == "pole1"
        assert project_id == "proj1"

    def test_pole_id_and_customer_id_combine_with_and(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(pole_id="pole1", customer_id="cust1")

        sql, period_type, pole_id, customer_id = mock_cursor.execute.call_args.args
        assert "p.Id = ? AND c.Id = ?" in sql
        assert pole_id == "pole1"
        assert customer_id == "cust1"

    def test_all_three_ids_combine_with_and(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(pole_id="pole1", project_id="proj1", customer_id="cust1")

        sql, period_type, pole_id, project_id, customer_id = mock_cursor.execute.call_args.args
        assert "p.Id = ? AND proj.Id = ? AND c.Id = ?" in sql
        assert (pole_id, project_id, customer_id) == ("pole1", "proj1", "cust1")

    def test_combined_filter_returns_single_dict_not_a_list(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        """Presence of pole_id always means single-object-or-404,
        regardless of what else is combined with it."""
        mock_cursor.fetchall.return_value = [
            _pole_row("proj1", "pole1", "PN-001", "LOC-001"),
        ]

        result = poles_api.get_poles(pole_id="pole1", project_id="proj1")

        assert isinstance(result, dict)

    def test_pole_belonging_to_a_different_project_returns_none(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        """e.g. a real pole Id that belongs to a DIFFERENT project than
        the one specified -- the AND in SQL means zero rows come back."""
        mock_cursor.fetchall.return_value = []
        assert poles_api.get_poles(pole_id="pole1", project_id="wrong-project") is None


def _summary_row(
    project_id, pole_id, pole_number, location_id,
    install_date=None, lat=None, long_=None,
    light_status=None, is_online=None,
    battery_percentage=None, panel_percentage=None, light_percentage=None,
    customer_id=None,
):
    return (
        project_id, pole_id, pole_number, location_id,
        install_date, lat, long_,
        light_status, is_online,
        battery_percentage, panel_percentage, light_percentage,
        customer_id,
    )


class TestClampSummaryLimit:
    def test_none_defaults_to_summary_max_limit(self):
        assert poles_api._clamp_summary_limit(None) == poles_api._SUMMARY_MAX_LIMIT

    def test_value_above_summary_max_limit_is_capped(self):
        assert poles_api._clamp_summary_limit(999999) == poles_api._SUMMARY_MAX_LIMIT

    def test_summary_max_limit_is_well_above_the_default_api_max_limit(self):
        """The whole point of summary mode -- api_utils.MAX_LIMIT (1000)
        would defeat the "give me every pole" use case this exists for."""
        assert poles_api._SUMMARY_MAX_LIMIT > api_utils.MAX_LIMIT

    def test_small_value_passes_through_unchanged(self):
        assert poles_api._clamp_summary_limit(50) == 50


class TestPoleSummarySqlStructure:
    def test_has_no_outer_apply(self):
        """The whole point of this query -- must not pay the per-pole
        PoleTelemetry lookup cost the full-detail query has."""
        assert "OUTER APPLY" not in poles_api._POLE_SUMMARY_SQL_TEMPLATE

    def test_does_not_select_telemetry_fields(self):
        sql = poles_api._POLE_SUMMARY_SQL_TEMPLATE
        assert "LastUpload" not in sql
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql

    def test_embeds_the_shared_recent_pole_stats_cte(self):
        """Reuses pole_vitals_api.py's exact CASE/MAX logic for
        LightStatus/IsOnline/the three avg*Percentage fields, rather
        than a second, independently-maintained copy."""
        from shared.pole_vitals_api import _RECENT_POLE_STATS_CTE
        assert _RECENT_POLE_STATS_CTE in poles_api._POLE_SUMMARY_SQL_TEMPLATE

    def test_still_selects_install_date_lat_long_and_customer_id(self):
        sql = poles_api._POLE_SUMMARY_SQL_TEMPLATE
        assert "p.InstallDate AS InstallDate" in sql
        assert "p.Lat AS Lat" in sql
        assert "p.Long AS Long" in sql
        assert "c.Id AS CustomerId" in sql

    def test_uses_plain_inner_joins_same_as_full_detail_query(self):
        sql = poles_api._POLE_SUMMARY_SQL_TEMPLATE
        assert "JOIN Projects proj ON p.ProjectId = proj.Id" in sql
        assert "JOIN Customers c ON proj.CustomerId = c.Id" in sql


class TestSummaryRowToDict:
    def test_maps_all_fields(self):
        row = _summary_row(
            "proj1", "pole1", "PN-001", "LOC-001",
            install_date="2023-05-10", lat=33.749, long_=-84.388,
            light_status="Working", is_online=True,
            battery_percentage=87.5, panel_percentage=92.1, light_percentage=88.0,
            customer_id="cust1",
        )

        result = poles_api._summary_row_to_dict(row)

        assert result == {
            "id": "pole1",
            "poleNumber": "PN-001",
            "locationId": "LOC-001",
            "installDate": "2023-05-10",
            "lat": 33.749,
            "long": -84.388,
            "lightStatus": "Working",
            "isOnline": True,
            "avgBatteryPercentage": 87.5,
            "avgPanelPercentage": 92.1,
            "avgLightPercentage": 88.0,
            "projectId": "proj1",
            "customerId": "cust1",
        }

    def test_does_not_include_telemetry_fields(self):
        row = _summary_row("proj1", "pole1", "PN-001", "LOC-001")
        result = poles_api._summary_row_to_dict(row)
        assert "lastUpdate" not in result
        assert "batteryVoltage1" not in result
        assert "batteryVoltage2" not in result

    def test_unclassified_pole_has_null_status_fields_not_zero(self):
        row = _summary_row("proj1", "pole1", "PN-001", "LOC-001", customer_id="cust1")
        result = poles_api._summary_row_to_dict(row)
        assert result["lightStatus"] is None
        assert result["isOnline"] is None
        assert result["avgBatteryPercentage"] is None
        assert result["customerId"] == "cust1"


class TestGetPolesSummaryMode:
    def test_unfiltered_uses_summary_template_and_higher_limit(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(summary=True)

        sql, period_type, limit = mock_cursor.execute.call_args.args
        assert "OUTER APPLY" not in sql
        assert period_type == "Hour"
        assert limit == poles_api._SUMMARY_MAX_LIMIT

    def test_unfiltered_without_summary_still_uses_full_template(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles()

        sql, period_type, limit = mock_cursor.execute.call_args.args
        assert "OUTER APPLY" in sql
        assert limit == api_utils.DEFAULT_LIMIT

    def test_summary_result_dicts_lack_telemetry_fields(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            _summary_row("proj1", "pole1", "PN-001", "LOC-001", customer_id="cust1"),
        ]

        result = poles_api.get_poles(summary=True)

        assert "lastUpdate" not in result[0]
        assert "batteryVoltage1" not in result[0]
        assert "batteryVoltage2" not in result[0]

    def test_custom_limit_still_respected_in_summary_mode(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        poles_api.get_poles(summary=True, limit=50)

        _, _, limit = mock_cursor.execute.call_args.args
        assert limit == 50

    def test_summary_combined_with_pole_id_still_uses_summary_template(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        """summary=True isn't just for the unfiltered case -- it also
        switches which query/dict-mapping is used for a single-pole
        lookup, for a consistent shape regardless of how many poles are
        being fetched."""
        mock_cursor.fetchall.return_value = [
            _summary_row("proj1", "pole1", "PN-001", "LOC-001", customer_id="cust1"),
        ]

        result = poles_api.get_poles(pole_id="pole1", summary=True)

        sql = mock_cursor.execute.call_args.args[0]
        assert "OUTER APPLY" not in sql
        assert isinstance(result, dict)
        assert "lastUpdate" not in result

    def test_summary_combined_with_project_id_still_returns_a_list(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            _summary_row("proj1", "pole1", "PN-001", "LOC-001", customer_id="cust1"),
            _summary_row("proj1", "pole2", "PN-002", "LOC-002", customer_id="cust1"),
        ]

        result = poles_api.get_poles(project_id="proj1", summary=True)

        assert isinstance(result, list)
        assert len(result) == 2
        assert all("lastUpdate" not in p for p in result)

