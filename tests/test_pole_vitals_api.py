"""Tests for shared/pole_vitals_api.py"""

import pytest

from shared import pole_vitals_api as m


# --------------------------------------------------------------------------
# Pure functions
# --------------------------------------------------------------------------


class TestPercentWorking:
    def test_zero_total_lights_returns_zero_not_error(self):
        assert m._percent_working(0, 0) == 0.0

    def test_no_faults_is_100_percent(self):
        assert m._percent_working(10, 0) == 100.0

    def test_all_faulted_is_zero_percent(self):
        assert m._percent_working(10, 10) == 0.0

    def test_partial_faults(self):
        assert m._percent_working(8, 3) == 62.5

    def test_rounds_to_two_decimals(self):
        assert m._percent_working(3, 1) == round((2 / 3) * 100, 2)


class TestSumPoleStats:
    def test_sums_across_multiple_rows(self):
        rows = [
            (None, None, "p1", "Proj 1", 8, 6, 3),
            (None, None, "p2", "Proj 2", 4, 4, 0),
        ]
        total_lights, connected, faults = m._sum_pole_stats(rows)
        assert (total_lights, connected, faults) == (12, 10, 3)

    def test_single_row(self):
        rows = [(None, None, "p1", "Proj 1", 5, 5, 1)]
        assert m._sum_pole_stats(rows) == (5, 5, 1)


class TestCustomerRollupFields:
    def test_empty_rows_returns_all_zeros(self):
        assert m._customer_rollup_fields([]) == {
            "totalLights": 0,
            "connectedLights": 0,
            "totalFaults": 0,
            "percentWorking": 0.0,
        }

    def test_computes_pole_weighted_rollup_not_averaged_percentages(self):
        """A true pole-weighted aggregate: summing raw counts across
        projects, THEN computing the percentage -- not averaging each
        project's own already-rounded percentage. A tiny project must not
        get equal weight to a huge one."""
        rows = [
            (None, None, "p1", "Tiny Project", 2, 2, 0),  # 100% working
            (None, None, "p2", "Huge Project", 1000, 500, 500),  # 50% working
        ]
        result = m._customer_rollup_fields(rows)
        # Naive average of percentages would be 75%; pole-weighted is
        # (1002 - 500) / 1002 = ~50.1%, dominated by the huge project.
        assert result["totalLights"] == 1002
        assert result["connectedLights"] == 502
        assert result["totalFaults"] == 500
        assert result["percentWorking"] == round((1002 - 500) / 1002 * 100, 2)
        assert result["percentWorking"] < 60  # nowhere near the naive 75% average


# --------------------------------------------------------------------------
# SQL structure checks
# --------------------------------------------------------------------------


class TestFetchSqlStructure:
    def test_reads_from_pole_vitals_filtered_by_period_type_param(self):
        sql = m._FETCH_SQL_TEMPLATE
        assert "FROM PoleVitals" in sql
        assert "WHERE PeriodType = ?" in sql

    def test_no_group_by_location_id_no_aggregation_cte(self):
        """Last48Hours is structurally 0-or-1 rows per LocationId, so
        RecentPoleStats here is a plain SELECT, not a GROUP BY
        aggregation the way the old Hour-window design needed."""
        sql = m._FETCH_SQL_TEMPLATE
        recent_pole_stats = sql.split("RecentPoleStats AS (")[1].split(")")[0]
        assert "GROUP BY" not in recent_pole_stats

    def test_total_lights_formula(self):
        """totalLights now counts EVERY pole, unconditionally -- no
        IsOnline/IsOpenIssueFault filtering at all, unlike the OLD
        definition this replaced (which required IsOnline OR
        IsOpenIssueFault)."""
        sql = m._FETCH_SQL_TEMPLATE
        assert "COUNT(*) AS TotalLights" in sql
        # The old, narrower expression must be genuinely gone from
        # TotalLights' own line, not just superseded -- confirms this
        # wasn't a partial edit that left both present somehow.
        total_lights_line = next(line for line in sql.splitlines() if "AS TotalLights" in line)
        assert "IsOnline" not in total_lights_line
        assert "IsOpenIssueFault" not in total_lights_line

    def test_total_faults_still_scoped_to_the_old_narrower_population(self):
        """DELIBERATELY not updated to match totalLights' own new,
        broader "every pole" scope, by explicit request -- totalLights
        and totalFaults are now computed over two different
        populations, not one shared one."""
        sql = m._FETCH_SQL_TEMPLATE
        assert (
            "CASE WHEN (IsOnline = 1 OR IsOpenIssueFault = 1) AND IsPoleFault = 1 THEN 1 ELSE 0 END"
            in sql
        )

    def test_connected_lights_formula(self):
        sql = m._FETCH_SQL_TEMPLATE
        assert "SUM(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END) AS ConnectedLights" in sql

    def test_total_faults_formula_requires_population_membership(self):
        """A pole outside totalFaults' OWN (still-narrower) population
        (not online, no open issue) can't count as a fault either, by
        construction -- the formula must check BOTH conditions, not just
        IsPoleFault alone. Note this is now a DIFFERENT, narrower
        population than totalLights' own -- see
        test_total_faults_still_scoped_to_the_old_narrower_population."""
        sql = m._FETCH_SQL_TEMPLATE
        faults_section = sql.split("AS TotalFaults")[0].split("SUM(")[-1]
        assert "IsOnline = 1 OR IsOpenIssueFault = 1" in faults_section
        assert "IsPoleFault = 1" in faults_section

    def test_left_joins_poles_to_recent_pole_stats(self):
        sql = m._FETCH_SQL_TEMPLATE
        assert "LEFT JOIN RecentPoleStats rps ON p.LocationId = rps.LocationId" in sql

    def test_left_joins_projects_and_project_agg_for_phantom_rows(self):
        sql = m._FETCH_SQL_TEMPLATE
        assert "LEFT JOIN Projects proj ON proj.CustomerId = c.Id" in sql
        assert "LEFT JOIN ProjectAgg pa ON pa.ProjectId = proj.Id" in sql

    def test_isnull_guards_every_aggregate_column(self):
        sql = m._FETCH_SQL_TEMPLATE
        assert "ISNULL(pa.TotalLights, 0)" in sql
        assert "ISNULL(pa.ConnectedLights, 0)" in sql
        assert "ISNULL(pa.TotalFaults, 0)" in sql

    def test_no_light_status_or_working_percentage_leftovers(self):
        sql = m._FETCH_SQL_TEMPLATE
        assert "LightStatus" not in sql
        assert "WorkingCount" not in sql
        assert "NoTelemetryCount" not in sql


class TestPoleDetailsSqlStructure:
    def test_direct_left_join_no_cte_needed(self):
        """Last48Hours is a single row per pole -- no aggregation CTE
        needed at all, unlike the old Hour-window design."""
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "LEFT JOIN PoleVitals rps ON p.LocationId = rps.LocationId AND rps.PeriodType = ?" in sql
        assert "RecentPoleStats" not in sql

    def test_all_five_fault_flags_present(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        for col in ("IsLedFault", "IsBatteryFault", "IsPanelFault", "IsOpenIssueFault", "IsPoleFault"):
            assert f"rps.{col} AS {col}" in sql

    def test_no_light_status_anywhere(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "LightStatus" not in sql

    def test_device_identity_columns_pulled_from_same_outer_apply_as_last_update(self):
        """ControllerCode/GroupId/ProductId/UserName are sourced from the
        SAME latest PoleTelemetry row as lastUpdate/batteryVoltage1/etc.
        -- one seek into PoleTelemetry already returns this same row, no
        reason for a second one just for these four columns."""
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        apply_block = sql.split("OUTER APPLY (")[1].split(") AS latest_pt")[0]
        for col in ("pt.ControllerCode", "pt.GroupId", "pt.ProductId", "pt.UserName"):
            assert col in apply_block
        assert "latest_pt.ControllerCode AS ControllerCode" in sql
        assert "latest_pt.GroupId AS GroupId" in sql
        assert "latest_pt.ProductId AS ProductId" in sql
        assert "latest_pt.UserName AS UserName" in sql

    def test_last_update_converted_to_pole_local_time_zone(self):
        """The core new requirement: lastUpdate must reflect the pole's
        own local time, not UTC -- via AT TIME ZONE on the already-
        DATETIMEOFFSET PoleTelemetry.LastUpload value, same operation
        pole_vitals_loader.py uses for bucketing."""
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert (
            "latest_pt.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload"
            in sql
        )

    def test_joins_pole_time_zones_for_the_conversion(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId" in sql

    def test_outer_apply_still_used_for_telemetry_not_cross(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "OUTER APPLY" in sql
        assert "CROSS APPLY" not in sql

    def test_inner_joins_poles_to_projects_and_customers(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "JOIN Projects proj ON p.ProjectId = proj.Id" in sql
        assert "JOIN Customers c ON proj.CustomerId = c.Id" in sql

    def test_new_latest_telemetry_columns_present(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        for col in (
            "LampPower1", "LampPower2",
            "BatteryElecCurrent1", "BatteryElecCurrent2",
            "SolarBoardVoltage", "SolarBoardElecCurrent",
        ):
            assert f"latest_pt.{col} AS {col}" in sql

    def test_new_columns_read_from_the_same_outer_apply_not_a_second_one(self):
        """One seek into PoleTelemetry already returns the latest row --
        no reason to query it twice for the same row."""
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert sql.count("OUTER APPLY") == 1
        apply_block = sql.split("OUTER APPLY (")[1].split(") AS latest_pt")[0]
        for col in (
            "pt.LampPower1", "pt.LampPower2",
            "pt.BatteryElecCurrent1", "pt.BatteryElecCurrent2",
            "pt.SolarBoardVoltage", "pt.SolarBoardElecCurrent",
            "pt.ModelId",
        ):
            assert col in apply_block

    def test_battery_charging_min_defaults_to_13_5_via_isnull(self):
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "ISNULL(pm.BatteryChargingMin, 13.5) AS BatteryChargingMin" in sql

    def test_battery_charging_min_joins_pole_models_via_latest_telemetrys_own_model_id(self):
        """Must join on latest_pt.ModelId (that SAME reading's model),
        not some other, possibly stale source of ModelId -- consistent
        with how pole_vitals_loader.py itself resolves this value."""
        sql = m._POLE_DETAILS_SQL_TEMPLATE
        assert "LEFT JOIN PoleModels pm ON latest_pt.ModelId = pm.ModelId" in sql


# --------------------------------------------------------------------------
# Dict-mapping functions
# --------------------------------------------------------------------------


class TestPoleRowToDict:
    def _row(
        self,
        project_id="proj1",
        pole_id="pole1",
        pole_number="PN-001",
        location_id="LOC-001",
        install_date="2025-01-01",
        lat=28.0,
        long_=-82.0,
        last_update="2026-07-31 08:00:00 -04:00",
        controller_code="CC-100",
        group_id=7,
        product_id="PROD-42",
        user_name="jdoe",
        battery_voltage_1=12.6,
        battery_voltage_2=12.4,
        lamp_power_1=8.7,
        lamp_power_2=8.6,
        battery_elec_current_1=15.0,
        battery_elec_current_2=15.2,
        solar_board_voltage=18.0,
        solar_board_elec_current=2.0,
        battery_charging_min=13.5,
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
        return (
            project_id, pole_id, pole_number, location_id, install_date, lat, long_,
            last_update, controller_code, group_id, product_id, user_name,
            battery_voltage_1, battery_voltage_2,
            lamp_power_1, lamp_power_2, battery_elec_current_1, battery_elec_current_2,
            solar_board_voltage, solar_board_elec_current, battery_charging_min,
            is_online, is_led_fault, is_battery_fault, is_panel_fault, is_open_issue_fault, is_pole_fault,
            battery_percentage, panel_percentage, light_percentage, customer_id,
        )

    def test_maps_every_field_correctly(self):
        result = m._pole_row_to_dict(self._row())
        assert result == {
            "id": "pole1",
            "poleNumber": "PN-001",
            "locationId": "LOC-001",
            "installDate": "2025-01-01",
            "lat": 28.0,
            "long": -82.0,
            "lastUpdate": "2026-07-31 08:00:00 -04:00",
            "controllerCode": "CC-100",
            "groupId": 7,
            "productId": "PROD-42",
            "userName": "jdoe",
            "batteryVoltage1": 12.6,
            "batteryVoltage2": 12.4,
            "lampPower1": 8.7,
            "lampPower2": 8.6,
            "batteryElecCurrent1": 15.0,
            "batteryElecCurrent2": 15.2,
            "solarBoardVoltage": 18.0,
            "solarBoardElecCurrent": 2.0,
            "batteryChargingMin": 13.5,
            "isOnline": True,
            "isLedFault": False,
            "isBatteryFault": False,
            "isPanelFault": False,
            "isOpenIssueFault": False,
            "isPoleFault": False,
            "avgBatteryPercentage": 89.0,
            "avgPanelPercentage": 45.0,
            "avgLightPercentage": 0.0,
        }

    def test_discards_project_id_and_customer_id(self):
        result = m._pole_row_to_dict(self._row())
        assert "projectId" not in result
        assert "customerId" not in result

    def test_no_light_status_key_at_all(self):
        result = m._pole_row_to_dict(self._row())
        assert "lightStatus" not in result

    def test_pole_with_no_vitals_row_has_null_fault_and_online_fields(self):
        """A pole with no Last48Hours row yet (LEFT JOIN produces NULLs)
        -- must be null, not a fabricated default."""
        row = self._row(
            is_online=None, is_led_fault=None, is_battery_fault=None,
            is_panel_fault=None, is_open_issue_fault=None, is_pole_fault=None,
            battery_percentage=None, panel_percentage=None, light_percentage=None,
        )
        result = m._pole_row_to_dict(row)
        assert result["isOnline"] is None
        assert result["isPoleFault"] is None
        assert result["avgBatteryPercentage"] is None

    def test_pole_with_no_telemetry_has_null_last_update_and_latest_reading_fields(self):
        """A pole with no PoleTelemetry row at all -- every field sourced
        from the OUTER APPLY must be null, EXCEPT batteryChargingMin,
        which has its own ISNULL(..., 13.5) default and so still comes
        back a real number even with no telemetry to source a ModelId
        from at all -- see _POLE_DETAILS_SQL_TEMPLATE's own comment."""
        row = self._row(
            last_update=None, controller_code=None, group_id=None, product_id=None,
            user_name=None,
            battery_voltage_1=None, battery_voltage_2=None,
            lamp_power_1=None, lamp_power_2=None,
            battery_elec_current_1=None, battery_elec_current_2=None,
            solar_board_voltage=None, solar_board_elec_current=None,
            battery_charging_min=13.5,  # ISNULL's own default, not None
        )
        result = m._pole_row_to_dict(row)
        assert result["lastUpdate"] is None
        assert result["controllerCode"] is None
        assert result["groupId"] is None
        assert result["productId"] is None
        assert result["userName"] is None
        assert result["batteryVoltage1"] is None
        assert result["batteryVoltage2"] is None
        assert result["lampPower1"] is None
        assert result["lampPower2"] is None
        assert result["batteryElecCurrent1"] is None
        assert result["batteryElecCurrent2"] is None
        assert result["solarBoardVoltage"] is None
        assert result["solarBoardElecCurrent"] is None
        assert result["batteryChargingMin"] == 13.5

    def test_battery_charging_min_defaults_to_13_5_when_model_unmatched(self):
        """The other half of the same ISNULL() default: a pole WITH
        telemetry, but whose ModelId has no PoleModels match at all,
        must also fall back to 13.5 -- not just the no-telemetry-at-all
        case above."""
        row = self._row(battery_charging_min=13.5)
        result = m._pole_row_to_dict(row)
        assert result["batteryChargingMin"] == 13.5


class TestRowToProjectDict:
    def test_maps_fields_and_computes_percent_working(self):
        row = (None, None, "proj1", "Downtown", 8, 6, 3)
        result = m._row_to_project_dict(row, poles=[])
        assert result["id"] == "proj1"
        assert result["name"] == "Downtown"
        assert result["totalLights"] == 8
        assert result["connectedLights"] == 6
        assert result["totalFaults"] == 3
        assert result["percentWorking"] == 62.5
        assert result["poles"] == []

    def test_no_optimistic_working_percentage_or_non_telemetry_fields(self):
        row = (None, None, "proj1", "Downtown", 8, 6, 3)
        result = m._row_to_project_dict(row, poles=[])
        assert "optimisticWorkingPercentage" not in result
        assert "totalNonTelemetryAvailable" not in result
        assert "workingPercentage" not in result

    def test_attaches_the_given_poles_list_as_is(self):
        poles = [{"id": "p1"}, {"id": "p2"}]
        row = (None, None, "proj1", "Downtown", 2, 2, 0)
        result = m._row_to_project_dict(row, poles=poles)
        assert result["poles"] == poles


# --------------------------------------------------------------------------
# get_pole_vitals() -- full flow
# --------------------------------------------------------------------------


class TestGetPoleVitalsUnfiltered:
    def test_uses_last_48_hours_as_the_status_period_type(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals()

        agg_call = mock_cursor.execute.call_args_list[0]
        assert agg_call.args[1] == "Last48Hours"

    def test_pole_detail_query_uses_last_known_48_hours_not_last_48_hours(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """The actual behavior change this pair of tests exists to pin
        down: the ROLLUP query (totalLights/connectedLights/
        percentWorking, checked above) keeps reading Last48Hours -- a
        silent pole is deliberately NOT counted as currently connected.
        But the per-pole DETAIL query (isPoleFault/isPanelFault/
        avgBatteryPercentage/etc., checked here) reads LastKnown48Hours
        instead, so those same silent poles still show their last-known
        state in the "poles" list rather than NULL."""
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals()

        detail_call = mock_cursor.execute.call_args_list[1]
        assert detail_call.args[1] == "LastKnown48Hours"

    def test_pole_detail_query_isonline_specifically_reverts_to_last_48_hours(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """A carve-out from the test above: isOnline is the ONE field
        that does NOT follow the rest of the per-pole detail fields onto
        LastKnown48Hours -- it reads Last48Hours instead (via the
        query's own second PoleVitals join, rps_online), same period
        type as the rollup query, since a silent pole's
        LastKnown48Hours.IsOnline would misleadingly reflect its own
        last-known state rather than whether it's online RIGHT NOW."""
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals()

        detail_call = mock_cursor.execute.call_args_list[1]
        # args[0]=sql, args[1]=LastKnown48Hours (rps), args[2]=Last48Hours
        # (rps_online, for IsOnline specifically)
        assert detail_call.args[2] == "Last48Hours"
        assert "LEFT JOIN PoleVitals rps_online" in detail_call.args[0]
        assert "rps_online.IsOnline AS IsOnline" in detail_call.args[0]

    def test_unfiltered_uses_subquery_limit_on_customers(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals(limit=5)

        agg_sql, period_type, limit = mock_cursor.execute.call_args_list[0].args
        assert "SELECT TOP (?) Id FROM Customers ORDER BY Name" in agg_sql
        assert limit == 5

    def test_no_customers_returns_empty_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]
        assert m.get_pole_vitals() == []

    def test_full_shape_with_one_customer_one_project_one_pole(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        agg_rows = [("cust1", "Acme", "proj1", "Downtown", 8, 6, 3)]
        pole_rows = [
            (
                "proj1", "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0,
                "2026-07-31 08:00:00 -04:00", "CC-100", 7, "PROD-42", "jdoe",
                12.6, 12.4,
                8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
                True, False, True, False, False, True,
                89.0, 45.0, 0.0, "cust1",
            )
        ]
        mock_cursor.fetchall.side_effect = [agg_rows, pole_rows]

        result = m.get_pole_vitals()

        assert len(result) == 1
        customer = result[0]
        assert customer["id"] == "cust1"
        assert customer["totalLights"] == 8
        assert customer["connectedLights"] == 6
        assert customer["totalFaults"] == 3
        assert customer["percentWorking"] == 62.5
        assert len(customer["projects"]) == 1
        project = customer["projects"][0]
        assert len(project["poles"]) == 1
        assert project["poles"][0]["id"] == "pole1"
        assert project["poles"][0]["isPoleFault"] is True
        assert project["poles"][0]["lampPower1"] == 8.7
        assert project["poles"][0]["batteryElecCurrent1"] == 15.0
        assert project["poles"][0]["controllerCode"] == "CC-100"
        assert project["poles"][0]["groupId"] == 7
        assert project["poles"][0]["productId"] == "PROD-42"
        assert project["poles"][0]["userName"] == "jdoe"
        assert project["poles"][0]["solarBoardVoltage"] == 18.0
        assert project["poles"][0]["batteryChargingMin"] == 13.5

    def test_customer_with_zero_projects_gets_empty_projects_and_zeroed_rollup(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        # Phantom row: ProjectId (index 2) is None for a customer with no projects
        agg_rows = [("cust1", "Acme", None, None, None, None, None)]
        mock_cursor.fetchall.side_effect = [agg_rows, []]

        result = m.get_pole_vitals()

        assert result[0]["totalLights"] == 0
        assert result[0]["percentWorking"] == 0.0
        assert result[0]["projects"] == []

    def test_preserves_customer_order_from_query(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        agg_rows = [
            ("custB", "Beta", "projB", "Proj B", 1, 1, 0),
            ("custA", "Alpha", "projA", "Proj A", 1, 1, 0),
        ]
        mock_cursor.fetchall.side_effect = [agg_rows, []]

        result = m.get_pole_vitals()

        assert [c["id"] for c in result] == ["custB", "custA"]


class TestGetPoleVitalsCustomerIdFilter:
    def test_filters_by_customer_id_column(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals(customer_id="cust1")

        agg_sql, period_type, customer_id = mock_cursor.execute.call_args_list[0].args
        assert "WHERE c.Id = ?" in agg_sql
        assert customer_id == "cust1"

    def test_returns_a_single_dict_not_a_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        agg_rows = [("cust1", "Acme", "proj1", "Downtown", 1, 1, 0)]
        mock_cursor.fetchall.side_effect = [agg_rows, []]

        result = m.get_pole_vitals(customer_id="cust1")

        assert isinstance(result, dict)
        assert result["id"] == "cust1"

    def test_nonexistent_customer_returns_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]
        assert m.get_pole_vitals(customer_id="does-not-exist") is None


class TestGetPoleVitalsProjectIdFilter:
    def test_filters_by_project_id_column(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals(project_id="proj1")

        agg_sql, period_type, project_id = mock_cursor.execute.call_args_list[0].args
        assert "WHERE proj.Id = ?" in agg_sql
        assert project_id == "proj1"

    def test_project_id_and_customer_id_combined_both_apply(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]

        m.get_pole_vitals(project_id="proj1", customer_id="cust1")

        agg_sql, period_type, project_id, customer_id = mock_cursor.execute.call_args_list[0].args
        assert "WHERE proj.Id = ? AND c.Id = ?" in agg_sql
        assert (project_id, customer_id) == ("proj1", "cust1")

    def test_returns_a_flat_dict_with_customer_context_not_nested(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        agg_rows = [("cust1", "Acme", "proj1", "Downtown", 1, 1, 0)]
        mock_cursor.fetchall.side_effect = [agg_rows, []]

        result = m.get_pole_vitals(project_id="proj1")

        assert result["id"] == "proj1"
        assert result["customerId"] == "cust1"
        assert result["customerName"] == "Acme"

    def test_nonexistent_project_returns_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.side_effect = [[], []]
        assert m.get_pole_vitals(project_id="does-not-exist") is None


# --------------------------------------------------------------------------
# get_pole_vitals_by_period() -- history endpoint
# --------------------------------------------------------------------------


class TestGetPoleVitalsByPeriod:
    def test_rejects_last_48_hours_as_a_history_period_type(self):
        """Last48Hours is a single current-state row, not a history to
        page through -- must be rejected here even though it's valid for
        pole_vitals_loader.py itself."""
        with pytest.raises(ValueError, match="Hour, Day"):
            m.get_pole_vitals_by_period("pole1", "Last48Hours")

    def test_rejects_invalid_period_type(self):
        with pytest.raises(ValueError):
            m.get_pole_vitals_by_period("pole1", "Week")

    def test_nonexistent_pole_returns_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None
        assert m.get_pole_vitals_by_period("does-not-exist", "Hour") is None

    def test_history_entries_use_fault_flags_not_light_status(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (
            "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0, "2026-07-31 08:00:00 -04:00",
            8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
        )
        mock_cursor.fetchall.return_value = [
            ("2026-07-31 07:00:00 -04:00", "2026-07-31 08:00:00 -04:00",
             True, False, False, False, False, False, 89.0, 45.0, 0.0),
        ]

        result = m.get_pole_vitals_by_period("pole1", "Hour")

        entry = result["vitals"][0]
        assert "lightStatus" not in entry
        assert entry["isOnline"] is True
        assert entry["isPoleFault"] is False
        assert result["lampPower1"] == 8.7
        assert result["batteryChargingMin"] == 13.5

    def test_pole_with_no_history_yet_returns_empty_vitals_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (
            "pole1", "PN-1", "LOC-1", None, None, None, None,
            None, None, None, None, None, None, 13.5,
        )
        mock_cursor.fetchall.return_value = []

        result = m.get_pole_vitals_by_period("pole1", "Day")

        assert result["vitals"] == []
        assert result["lampPower1"] is None
        assert result["batteryChargingMin"] == 13.5

    def test_pole_info_last_update_query_converts_to_local_time_zone(self):
        sql = m._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert "AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time')" in sql
        assert "LEFT JOIN PoleTimeZones ptz" in sql

    def test_pole_info_new_latest_telemetry_columns_present(self):
        sql = m._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        for col in (
            "LampPower1", "LampPower2",
            "BatteryElecCurrent1", "BatteryElecCurrent2",
            "SolarBoardVoltage", "SolarBoardElecCurrent",
        ):
            assert f"latest_pt.{col} AS {col}" in sql

    def test_pole_info_does_not_reintroduce_battery_voltage(self):
        """BatteryVoltage1/BatteryVoltage2 remain deliberately excluded
        from THIS endpoint specifically, per an earlier, separate
        explicit request -- unrelated to this addition, not
        reconsidered here."""
        sql = m._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert "BatteryVoltage1" not in sql
        assert "BatteryVoltage2" not in sql

    def test_pole_info_battery_charging_min_defaults_to_13_5_via_isnull(self):
        sql = m._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert "ISNULL(pm.BatteryChargingMin, 13.5) AS BatteryChargingMin" in sql
        assert "LEFT JOIN PoleModels pm ON latest_pt.ModelId = pm.ModelId" in sql

    def test_hour_history_query_has_a_bound_anchored_to_latest_telemetry(self):
        """The whole point of this specific change: anchored to this
        pole's own latest PoleTelemetry reading (via PoleContext's own
        MaxLastUpload), NOT to SYSDATETIMEOFFSET()/"now" -- so a pole
        that's gone completely offline still returns its own last known
        activity instead of an empty list. Bound to limit hours back
        (via a bound parameter), not a fixed 48 -- a real bug an earlier
        version of this query had (limit=168 would still only ever
        return up to 48 hours' worth back, no matter how much more was
        actually available)."""
        sql = m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "AND pv.PeriodStart >= DATEADD(HOUR, -1 * ?, pc.MaxLastUpload)" in sql
        assert "SYSDATETIMEOFFSET" not in sql
        assert "-48" not in sql

    def test_hour_history_query_resolves_latest_telemetry_via_pole_context_cte(self):
        sql = m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "WITH PoleContext AS (" in sql
        assert "SELECT MAX(pt.LastUpload)" in sql
        assert "FROM PoleTelemetry pt" in sql
        assert "JOIN PoleContext pc ON pv.LocationId = pc.LocationId" in sql

    def test_hour_history_query_excludes_the_missing_last_upload_sentinel(self):
        """Hardcoded literal, not a bound parameter -- matches the same
        established precedent in pole_daylight_flags_loader.py's own
        _FIND_UNFLAGGED_SQL."""
        sql = m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "AND pt.LastUpload <> '9999-12-31 23:59:59.999 +00:00'" in sql

    def test_hour_history_query_hardcodes_hour_not_a_bound_parameter(self):
        """This template is only ever selected when period_type == 'Hour'
        -- nothing to parameterize, so it's a literal, not a bound '?'."""
        sql = m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "pv.PeriodType = 'Hour'" in sql
        # p.Id = ?, TOP (?), and the DATEADD's own -1 * ? -- three, not two.
        assert sql.count("?") == 3

    def test_hour_history_query_window_bound_reuses_the_same_limit_as_top(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """Regression guard for the actual reported bug: limit must be
        bound TWICE with the SAME value (once for TOP (?), once for the
        DATEADD window) -- not a fixed 48 that ignores whatever limit
        the caller actually passed."""
        mock_cursor.fetchone.return_value = (
            "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0, "2026-07-31 08:00:00 -04:00",
            8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
        )
        mock_cursor.fetchall.return_value = []

        m.get_pole_vitals_by_period("pole1", "Hour", limit=168)

        history_call = mock_cursor.execute.call_args_list[-1]
        # (sql, pole_id, top_limit, dateadd_limit) -- top_limit and
        # dateadd_limit must be the SAME value (168), not 168 and 48.
        assert history_call.args == (m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE, "pole1", 168, 168)

    def test_hour_history_query_keeps_the_row_count_limit_too(self):
        """TOP (?) is kept ALONGSIDE the new time bound, not replaced by
        it -- a caller can still ask for fewer than whatever the 48-hour
        window would otherwise return."""
        sql = m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        assert "SELECT TOP (?)" in sql

    def test_day_history_query_unaffected_no_time_bound_added(self):
        """Day intentionally keeps the plain, unbounded-by-wall-clock-time
        query -- this is a deliberate difference from Hour, not an
        oversight."""
        sql = m._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert "SYSDATETIMEOFFSET" not in sql
        assert sql.count("?") == 3  # TOP (?), p.Id = ?, AND pv.PeriodType = ?

    def test_hour_period_type_uses_the_hour_specific_template(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (
            "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0, "2026-07-31 08:00:00 -04:00",
            8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
        )
        mock_cursor.fetchall.return_value = []

        m.get_pole_vitals_by_period("pole1", "Hour", limit=10)

        history_call = mock_cursor.execute.call_args_list[-1]
        assert history_call.args[0] == m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE
        # pole_id, THEN limit TWICE (once for TOP (?), once for the
        # DATEADD window bound -- the SAME value, not two different
        # ones) -- the opposite order, and one extra parameter, from the
        # Day template below, forced by PoleContext's own CTE (which
        # needs pole_id to resolve LocationId) coming textually before
        # the main query's own TOP (?).
        assert history_call.args == (m._POLE_VITALS_HOUR_HISTORY_SQL_TEMPLATE, "pole1", 10, 10)

    def test_day_period_type_uses_the_original_template(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (
            "pole1", "PN-1", "LOC-1", "2025-01-01", 28.0, -82.0, "2026-07-31 08:00:00 -04:00",
            8.7, 8.6, 15.0, 15.2, 18.0, 2.0, 13.5,
        )
        mock_cursor.fetchall.return_value = []

        m.get_pole_vitals_by_period("pole1", "Day", limit=10)

        history_call = mock_cursor.execute.call_args_list[-1]
        assert history_call.args[0] == m._POLE_VITALS_HISTORY_SQL_TEMPLATE
        assert history_call.args == (m._POLE_VITALS_HISTORY_SQL_TEMPLATE, 10, "pole1", "Day")
