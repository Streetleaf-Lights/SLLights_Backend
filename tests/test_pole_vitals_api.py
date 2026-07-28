"""Tests for shared/pole_vitals_api.py"""

import pytest

from shared import api_utils, pole_vitals_api


def _row(customer_id, customer_name, project_id, project_name, total, working, faults, no_telemetry=0):
    return (customer_id, customer_name, project_id, project_name, total, working, faults, no_telemetry)


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


def _set_fetchall_results(mock_cursor, aggregate_rows, pole_rows=None):
    """
    get_pole_vitals() calls cursor.fetchall() twice per invocation --
    once for the aggregate query, once for the pole-details query.
    Tests that don't care about the poles list specifically can omit
    pole_rows entirely (defaults to no pole detail rows, so every
    project's "poles" list comes out empty, which is harmless for tests
    only checking the rollup fields).
    """
    mock_cursor.fetchall.side_effect = [aggregate_rows, pole_rows or []]


class TestWorkingPercentage:
    def test_zero_total_lights_returns_zero_not_a_crash(self):
        """0/0 is mathematically undefined -- must return a plain 0.0,
        not raise ZeroDivisionError and not return None."""
        assert pole_vitals_api._working_percentage(0, 0) == 0.0

    def test_computes_expected_percentage(self):
        assert pole_vitals_api._working_percentage(8, 10) == 80.0

    def test_all_working_is_100_percent(self):
        assert pole_vitals_api._working_percentage(10, 10) == 100.0

    def test_none_working_is_zero_percent(self):
        assert pole_vitals_api._working_percentage(0, 10) == 0.0

    def test_rounds_to_two_decimal_places(self):
        assert pole_vitals_api._working_percentage(1, 3) == 33.33


class TestFetchSqlStructure:
    """Structural checks on the SQL itself -- the "unclassified pole"
    and "zero-pole project" behaviors are fundamentally SQL-level
    correctness (COUNT(*) vs COUNT(LightStatus), LEFT JOIN vs INNER
    JOIN), not something a mocked-row test can verify, since mocked rows
    already represent post-aggregation results."""

    def test_total_lights_counts_every_pole_not_just_classified_ones(self):
        """COUNT(*) in PoleWithStatus, not COUNT(LightStatus) -- a pole
        with no Hour PoleVitals row yet (LightStatus IS NULL after the
        LEFT JOIN) must still count toward TotalLights."""
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "COUNT(*) AS TotalLights" in sql

    def test_poles_left_joined_to_recent_pole_stats_not_inner(self):
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "LEFT JOIN RecentPoleStats rps ON p.LocationId = rps.LocationId" in sql

    def test_working_count_includes_both_working_and_daylight(self):
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "LightStatus IN ('Working', 'DayLight')" in sql

    def test_total_faults_is_not_working_only(self):
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "LightStatus = 'Not Working'" in sql

    def test_no_telemetry_count_counts_null_light_status(self):
        """A pole with no Hour PoleVitals row yet (LightStatus IS NULL
        after the LEFT JOIN) is counted here specifically -- distinct
        from both WorkingCount and TotalFaults, not folded into either."""
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "SUM(CASE WHEN LightStatus IS NULL THEN 1 ELSE 0 END) AS NoTelemetryCount" in sql
        assert "ISNULL(pa.NoTelemetryCount, 0)" in sql

    def test_projects_left_joined_to_customers_not_inner(self):
        """A customer with zero projects must still appear (as a
        'phantom' row with every project column NULL), not be silently
        dropped by an INNER JOIN -- this is what lets
        get_pole_vitals(customer_id=X) distinguish 'exists, no projects'
        from 'doesn't exist' at all."""
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "LEFT JOIN Projects proj ON proj.CustomerId = c.Id" in sql

    def test_projects_left_joined_to_project_agg_not_inner(self):
        """A project with zero poles must still appear (TotalLights=0
        via ISNULL), not be silently dropped by an INNER JOIN."""
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "LEFT JOIN ProjectAgg pa ON pa.ProjectId = proj.Id" in sql
        assert "ISNULL(pa.TotalLights, 0)" in sql

    def test_latest_hour_vitals_filters_to_hour_period_type_via_parameter(self):
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "WHERE PeriodType = ?" in sql

    def test_recent_pole_stats_filters_to_a_rolling_window_not_a_single_row(self):
        """Confirms 'rolled up over the recent window', not 'just the
        single most recent row' -- the ROW_NUMBER()-based 'latest row
        only' pattern this replaced is gone. Checks the {hours_window}
        placeholder is wired in (not a hardcoded number), so the SQL
        can't drift from _RECENT_HOURS_WINDOW."""
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "PeriodStart >= DATEADD(HOUR, -{hours_window}, SYSDATETIMEOFFSET())" in sql
        assert "ROW_NUMBER()" not in sql

    def test_hours_window_placeholder_formats_to_the_real_constant(self):
        formatted = pole_vitals_api._FETCH_SQL_TEMPLATE.format(
            where_clause="WHERE 1=1", hours_window=pole_vitals_api._RECENT_HOURS_WINDOW
        )
        assert "DATEADD(HOUR, -6, SYSDATETIMEOFFSET())" in formatted

    def test_recent_pole_stats_uses_priority_based_light_status_aggregation(self):
        """Same priority as PoleVitals' own bucket-level aggregation:
        Not Working beats Working beats the DayLight default."""
        sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "WHEN MAX(CASE WHEN LightStatus = 'Not Working' THEN 1 ELSE 0 END) = 1 THEN 'Not Working'" in sql
        assert "WHEN MAX(CASE WHEN LightStatus = 'Working' THEN 1 ELSE 0 END) = 1 THEN 'Working'" in sql
        assert "ELSE 'DayLight'" in sql

    def test_status_period_type_constant_is_hour(self):
        assert pole_vitals_api._STATUS_PERIOD_TYPE == "Hour"

    def test_recent_hours_window_constant_is_six(self):
        assert pole_vitals_api._RECENT_HOURS_WINDOW == 6


class TestPoleDetailsSqlStructure:
    def test_full_template_matches_its_pre_refactor_text_byte_for_byte(self):
        """_POLE_DETAILS_SQL_TEMPLATE used to be one inline string;
        _RECENT_POLE_STATS_CTE was later factored out of it into its own
        constant so shared/poles_api.py's summary query could reuse it
        without a second, drift-prone copy. This is the permanent
        regression guard confirming that refactor changed nothing about
        the assembled SQL -- not just "looks the same", but literally
        identical text to what was there before the extraction."""
        expected = """
;WITH RecentPoleStats AS (
    SELECT
        LocationId,
        ROUND(AVG(AvgBatteryPercentage), 2) AS BatteryPercentage,
        ROUND(AVG(AvgPanelPercentage), 2) AS PanelPercentage,
        ROUND(AVG(AvgLightPercentage), 2) AS LightPercentage,
        CAST(MAX(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END) AS BIT) AS IsOnline,
        CASE
            WHEN MAX(CASE WHEN LightStatus = 'Not Working' THEN 1 ELSE 0 END) = 1 THEN 'Not Working'
            WHEN MAX(CASE WHEN LightStatus = 'Working' THEN 1 ELSE 0 END) = 1 THEN 'Working'
            ELSE 'DayLight'
        END AS LightStatus
    FROM PoleVitals
    WHERE PeriodType = ?
      AND PeriodStart >= DATEADD(HOUR, -{hours_window}, SYSDATETIMEOFFSET())
    GROUP BY LocationId
)
SELECT
    proj.Id AS ProjectId,
    p.Id AS PoleId,
    p.PoleNumber AS PoleNumber,
    p.LocationId AS LocationId,
    p.InstallDate AS InstallDate,
    p.Lat AS Lat,
    p.Long AS Long,
    latest_pt.LastUpload AS LastUpload,
    latest_pt.BatteryVoltage1 AS BatteryVoltage1,
    latest_pt.BatteryVoltage2 AS BatteryVoltage2,
    rps.LightStatus AS LightStatus,
    rps.IsOnline AS IsOnline,
    rps.BatteryPercentage AS BatteryPercentage,
    rps.PanelPercentage AS PanelPercentage,
    rps.LightPercentage AS LightPercentage,
    c.Id AS CustomerId
FROM Poles p
JOIN Projects proj ON p.ProjectId = proj.Id
JOIN Customers c ON proj.CustomerId = c.Id
LEFT JOIN RecentPoleStats rps ON p.LocationId = rps.LocationId
OUTER APPLY (
    SELECT TOP 1 pt.LastUpload, pt.BatteryVoltage1, pt.BatteryVoltage2
    FROM PoleTelemetry pt
    WHERE pt.LocationId = p.LocationId
    ORDER BY pt.LastUpload DESC
) AS latest_pt
{where_clause}
ORDER BY proj.Id, p.PoleNumber
"""
        assert pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE == expected

    def test_full_template_embeds_the_shared_recent_pole_stats_cte(self):
        """_RECENT_POLE_STATS_CTE was factored out of
        _POLE_DETAILS_SQL_TEMPLATE specifically so shared/poles_api.py's
        lighter summary query could reuse the exact same CASE/MAX logic
        rather than a second, drift-prone copy -- confirms the full
        template still actually contains it after that refactor, not
        just a similar-looking duplicate."""
        assert pole_vitals_api._RECENT_POLE_STATS_CTE in pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE

    def test_selects_install_date_lat_long_directly_from_poles(self):
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "p.InstallDate AS InstallDate" in sql
        assert "p.Lat AS Lat" in sql
        assert "p.Long AS Long" in sql

    def test_selects_customer_id_from_the_already_joined_customers_table(self):
        """Customers is already JOINed here for the where_clause's
        c.Id = ? filtering -- CustomerId just needs to be selected
        alongside everything else, not a new join."""
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "c.Id AS CustomerId" in sql

    def test_outer_apply_gets_single_most_recent_pole_telemetry_row(self):
        """OUTER, not CROSS or INNER: a pole with no LocationId or zero
        matching PoleTelemetry rows must still appear in the result
        (with these columns NULL), not be dropped."""
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "OUTER APPLY (" in sql
        assert "SELECT TOP 1 pt.LastUpload, pt.BatteryVoltage1, pt.BatteryVoltage2" in sql
        assert "FROM PoleTelemetry pt" in sql
        assert "WHERE pt.LocationId = p.LocationId" in sql
        assert "ORDER BY pt.LastUpload DESC" in sql

    def test_selects_last_update_and_battery_voltage_from_the_apply(self):
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "latest_pt.LastUpload AS LastUpload" in sql
        assert "latest_pt.BatteryVoltage1 AS BatteryVoltage1" in sql
        assert "latest_pt.BatteryVoltage2 AS BatteryVoltage2" in sql

    def test_uses_inner_joins_not_left(self):
        """Unlike the aggregate query, no phantom-row handling is needed
        here -- a project/customer with zero matching poles just
        returns zero rows for this query, which the grouping step in
        Python turns into an empty poles list naturally."""
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "JOIN Projects proj ON p.ProjectId = proj.Id" in sql
        assert "JOIN Customers c ON proj.CustomerId = c.Id" in sql
        assert "LEFT JOIN Projects" not in sql
        assert "LEFT JOIN Customers" not in sql

    def test_selects_project_id_pole_id_number_location_status_online_and_percentages(self):
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "proj.Id AS ProjectId" in sql
        assert "p.Id AS PoleId" in sql
        assert "p.PoleNumber AS PoleNumber" in sql
        assert "p.LocationId AS LocationId" in sql
        assert "rps.LightStatus AS LightStatus" in sql
        assert "rps.IsOnline AS IsOnline" in sql
        assert "rps.BatteryPercentage AS BatteryPercentage" in sql
        assert "rps.PanelPercentage AS PanelPercentage" in sql
        assert "rps.LightPercentage AS LightPercentage" in sql

    def test_recent_pole_stats_cte_averages_the_three_percentage_metrics(self):
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        cte = sql.split("RecentPoleStats AS (")[1].split(")\nSELECT")[0]
        assert "ROUND(AVG(AvgBatteryPercentage), 2) AS BatteryPercentage" in cte
        assert "ROUND(AVG(AvgPanelPercentage), 2) AS PanelPercentage" in cte
        assert "ROUND(AVG(AvgLightPercentage), 2) AS LightPercentage" in cte

    def test_is_online_is_cast_to_bit_not_left_as_a_plain_int(self):
        """MAX(CASE WHEN...) produces a plain INT (0/1) -- without an
        explicit CAST to BIT, pyodbc would hand that back as a Python
        int, so isOnline would serialize as 1/0 in JSON instead of
        true/false."""
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "CAST(MAX(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END) AS BIT) AS IsOnline" in sql

    def test_uses_same_rolling_window_pattern_as_aggregate_query(self):
        """Same per-pole 'rolled up over the recent window' logic as the
        aggregate query -- not a different/inconsistent definition of
        'current status' between the two queries."""
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "PeriodStart >= DATEADD(HOUR, -{hours_window}, SYSDATETIMEOFFSET())" in sql
        assert "WHERE PeriodType = ?" in sql
        assert "ROW_NUMBER()" not in sql

    def test_orders_by_project_then_pole_number(self):
        sql = pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE
        assert "ORDER BY proj.Id, p.PoleNumber" in sql


class TestPoleRowToDict:
    def test_maps_all_fields(self):
        row = _pole_row(
            "proj1", "pole1", "PN-001", "LOC-001",
            install_date="2023-05-10", lat=33.749, long_=-84.388,
            last_update="2026-07-25 14:30:00", battery_voltage_1=12.6, battery_voltage_2=12.4,
            light_status="Working", is_online=True,
            battery_percentage=87.5, panel_percentage=92.1, light_percentage=88.0,
            customer_id="cust1",
        )
        # Exact dict equality (no extra keys) implicitly confirms
        # ProjectId and CustomerId are excluded here even when both are
        # real, non-null values -- not just coincidentally absent because
        # they defaulted to None. Both are needed by shared/poles_api.py
        # instead, which reads them straight from the row itself, not
        # through this function -- see this function's own docstring.
        assert pole_vitals_api._pole_row_to_dict(row) == {
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
        }

    def test_null_light_status_becomes_json_null_not_a_made_up_string(self):
        """A pole with no Hour PoleVitals row yet -- must be a plain
        None (JSON null), not an invented string like 'No Telemetry',
        since that's not a real LightStatus value in the database."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001", light_status=None)
        result = pole_vitals_api._pole_row_to_dict(row)
        assert result["lightStatus"] is None

    def test_unclassified_pole_also_has_null_is_online(self):
        """Same reasoning as lightStatus -- an unclassified pole has no
        PoleVitals row at all, so isOnline is genuinely unknown, not
        False."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001")
        result = pole_vitals_api._pole_row_to_dict(row)
        assert result["isOnline"] is None

    def test_unclassified_pole_has_all_three_percentages_null(self):
        """A pole with zero Hour rows in the recent window (LEFT JOIN
        produces NULL for everything from RecentPoleStats) must show
        null for all three averaged percentages too, not 0 -- there's
        no data to average, not a confirmed-zero reading."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001")
        result = pole_vitals_api._pole_row_to_dict(row)
        assert result["avgBatteryPercentage"] is None
        assert result["avgPanelPercentage"] is None
        assert result["avgLightPercentage"] is None

    def test_pole_with_no_matching_telemetry_has_null_last_update_and_voltages(self):
        """OUTER APPLY produces NULL for LastUpload/BatteryVoltage1/2
        when a pole has no LocationId or zero matching PoleTelemetry
        rows -- must stay null, not 0 or some fabricated timestamp."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001")
        result = pole_vitals_api._pole_row_to_dict(row)
        assert result["lastUpdate"] is None
        assert result["batteryVoltage1"] is None
        assert result["batteryVoltage2"] is None

    def test_install_date_lat_long_pass_through_from_poles(self):
        """These come from Poles directly, independent of whether the
        pole has any telemetry or vitals data at all -- a pole can be
        fully unclassified (no PoleVitals, no PoleTelemetry) and still
        have its static install-time facts."""
        row = _pole_row("proj1", "pole1", "PN-001", "LOC-001", install_date="2023-05-10", lat=33.749, long_=-84.388)
        result = pole_vitals_api._pole_row_to_dict(row)
        assert result["installDate"] == "2023-05-10"
        assert result["lat"] == 33.749
        assert result["long"] == -84.388

    def test_is_online_true_and_false_pass_through(self):
        assert pole_vitals_api._pole_row_to_dict(
            _pole_row("proj1", "pole1", "PN-001", "LOC-001", light_status="Working", is_online=True)
        )["isOnline"] is True
        assert pole_vitals_api._pole_row_to_dict(
            _pole_row("proj1", "pole1", "PN-001", "LOC-001", light_status="Not Working", is_online=False)
        )["isOnline"] is False


class TestSumPoleStats:
    def test_sums_each_column_across_rows(self):
        rows = [
            _row("c", "C", "p1", "P1", 10, 8, 1, 1),
            _row("c", "C", "p2", "P2", 90, 90, 0, 0),
        ]
        assert pole_vitals_api._sum_pole_stats(rows) == (100, 98, 1, 1)

    def test_single_row_matches_that_rows_own_values(self):
        rows = [_row("c", "C", "p1", "P1", 10, 8, 1, 1)]
        assert pole_vitals_api._sum_pole_stats(rows) == (10, 8, 1, 1)

    def test_sums_no_telemetry_count_across_rows(self):
        rows = [
            _row("c", "C", "p1", "P1", 10, 6, 1, 3),
            _row("c", "C", "p2", "P2", 20, 15, 2, 3),
        ]
        assert pole_vitals_api._sum_pole_stats(rows) == (30, 21, 3, 6)


class TestCustomerRollupFields:
    def test_optimistic_percentage_treats_no_telemetry_as_working(self):
        """8 working + 1 no-telemetry, out of 10 total -> optimistic
        counts the no-telemetry pole toward the numerator too: 90%, not
        workingPercentage's more conservative 80%."""
        rows = [_row("c", "C", "p1", "P1", 10, 8, 1, 1)]
        result = pole_vitals_api._customer_rollup_fields(rows)
        assert result["workingPercentage"] == 80.0
        assert result["optimisticWorkingPercentage"] == 90.0

    def test_optimistic_percentage_equals_working_percentage_when_no_unclassified_poles(self):
        rows = [_row("c", "C", "p1", "P1", 10, 8, 2, 0)]
        result = pole_vitals_api._customer_rollup_fields(rows)
        assert result["workingPercentage"] == result["optimisticWorkingPercentage"] == 80.0

    def test_optimistic_percentage_is_a_pole_weighted_aggregate_too(self):
        """Same weighting principle as workingPercentage -- summed
        across projects before dividing, not averaged per-project."""
        rows = [
            _row("c", "C", "p1", "Tiny", 10, 0, 0, 10),  # all unclassified
            _row("c", "C", "p2", "Huge", 90, 90, 0, 0),  # all working
        ]
        result = pole_vitals_api._customer_rollup_fields(rows)
        # (0 + 90 + 10) / 100 = 100%
        assert result["optimisticWorkingPercentage"] == 100.0

    def test_empty_rows_returns_all_zeros(self):
        assert pole_vitals_api._customer_rollup_fields([]) == {
            "totalLights": 0,
            "workingPercentage": 0.0,
            "optimisticWorkingPercentage": 0.0,
            "totalFaults": 0,
            "totalNonTelemetryAvailable": 0,
        }

    def test_is_a_true_pole_weighted_aggregate_not_an_average_of_percentages(self):
        """
        The property this whole feature exists to get right: a tiny
        project at 80% and a huge project at 100% must NOT average to
        90% -- the huge project's poles dominate the real aggregate.
        10 lights @ 80% (8 working) + 90 lights @ 100% (90 working) =
        98/100 = 98%, not (80+100)/2 = 90%.
        """
        rows = [
            _row("c", "C", "p1", "Tiny", 10, 8, 1),
            _row("c", "C", "p2", "Huge", 90, 90, 0),
        ]
        result = pole_vitals_api._customer_rollup_fields(rows)
        assert result["workingPercentage"] == 98.0
        assert result["workingPercentage"] != 90.0  # the wrong, naive-average answer

    def test_sums_total_lights_and_faults_across_projects(self):
        rows = [
            _row("c", "C", "p1", "P1", 10, 8, 1),
            _row("c", "C", "p2", "P2", 20, 15, 3),
        ]
        result = pole_vitals_api._customer_rollup_fields(rows)
        assert result["totalLights"] == 30
        assert result["totalFaults"] == 4

    def test_single_project_rollup_matches_that_projects_own_stats(self):
        rows = [_row("c", "C", "p1", "P1", 10, 8, 1, 1)]
        result = pole_vitals_api._customer_rollup_fields(rows)
        assert result == {
            "totalLights": 10,
            "workingPercentage": 80.0,
            "optimisticWorkingPercentage": 90.0,
            "totalFaults": 1,
            "totalNonTelemetryAvailable": 1,
        }


class TestGetPoleVitalsCustomerLevelRollup:
    """Full-flow tests confirming get_pole_vitals() actually wires the
    rollup fields onto the customer dict correctly, in both the
    unfiltered and customer_id-filtered cases."""

    def test_unfiltered_customer_has_rollup_fields_alongside_projects(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Tiny", 10, 8, 1),
            _row("cust1", "Acme", "proj2", "Huge", 90, 90, 0),
        ])

        result = pole_vitals_api.get_pole_vitals()

        customer = result[0]
        assert customer["totalLights"] == 100
        assert customer["workingPercentage"] == 98.0
        assert customer["totalFaults"] == 1
        assert len(customer["projects"]) == 2

    def test_different_customers_get_independently_computed_rollups(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "P1", 10, 5, 5),
            _row("cust2", "Globex", "proj2", "P2", 10, 10, 0),
        ])

        result = pole_vitals_api.get_pole_vitals()

        assert result[0]["workingPercentage"] == 50.0
        assert result[1]["workingPercentage"] == 100.0

    def test_customer_id_filtered_case_includes_rollup_fields(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Tiny", 10, 8, 1),
            _row("cust1", "Acme", "proj2", "Huge", 90, 90, 0),
        ])

        result = pole_vitals_api.get_pole_vitals(customer_id="cust1")

        assert result["totalLights"] == 100
        assert result["workingPercentage"] == 98.0
        assert result["totalFaults"] == 1

    def test_project_id_filtered_case_does_not_include_customer_rollup(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """A single-project lookup is a project view, not a customer
        view -- it should NOT carry the project's customer's aggregate
        totals alongside its own."""
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Downtown", 10, 8, 1),
        ])

        result = pole_vitals_api.get_pole_vitals(project_id="proj1")

        # Its own stats are present (already covered by
        # TestGetPoleVitalsProjectIdFilter), but nothing from a
        # customer-level rollup should leak in.
        assert set(result.keys()) == {
            "id", "name", "totalLights", "workingPercentage",
            "optimisticWorkingPercentage", "totalFaults",
            "totalNonTelemetryAvailable", "poles", "customerId", "customerName",
        }


class TestGetPoleVitalsPolesListWiring:
    """
    Full-flow tests confirming get_pole_vitals() correctly runs the
    second (pole-details) query and attaches each pole to the right
    project -- the two queries share the same where_clause/params, so
    these also implicitly confirm that reuse works correctly across the
    three filtering branches (unfiltered, customerId, projectId).
    """

    def test_second_fetchall_call_is_the_pole_details_query(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [], [])

        pole_vitals_api.get_pole_vitals()

        assert mock_cursor.execute.call_count == 2
        first_sql = mock_cursor.execute.call_args_list[0].args[0]
        second_sql = mock_cursor.execute.call_args_list[1].args[0]
        assert "ProjectAgg" in first_sql  # the aggregate query
        assert "PoleNumber" in second_sql  # the pole-details query

    def test_poles_from_different_projects_dont_cross_contaminate(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(
            mock_cursor,
            [
                _row("cust1", "Acme", "proj1", "Downtown", 1, 1, 0),
                _row("cust1", "Acme", "proj2", "Uptown", 1, 0, 1),
            ],
            [
                _pole_row("proj1", "poleA", "PN-A", "LOC-A", "Working"),
                _pole_row("proj2", "poleB", "PN-B", "LOC-B", "Not Working"),
            ],
        )

        result = pole_vitals_api.get_pole_vitals()

        projects = {p["id"]: p for p in result[0]["projects"]}
        assert [p["id"] for p in projects["proj1"]["poles"]] == ["poleA"]
        assert [p["id"] for p in projects["proj2"]["poles"]] == ["poleB"]

    def test_project_with_no_matching_pole_rows_gets_empty_poles_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """No phantom-row handling needed for this query -- a project
        the pole-details query simply has no rows for still gets a
        clean empty list when looked up."""
        _set_fetchall_results(
            mock_cursor,
            [_row("cust1", "Acme", "proj1", "Downtown", 0, 0, 0)],
            [],
        )

        result = pole_vitals_api.get_pole_vitals()

        assert result[0]["projects"][0]["poles"] == []

    def test_customer_id_filtered_case_attaches_poles_to_each_project(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(
            mock_cursor,
            [_row("cust1", "Acme", "proj1", "Downtown", 1, 1, 0)],
            [_pole_row("proj1", "poleA", "PN-A", "LOC-A", "Working")],
        )

        result = pole_vitals_api.get_pole_vitals(customer_id="cust1")

        assert [p["id"] for p in result["projects"][0]["poles"]] == ["poleA"]

    def test_project_id_filtered_case_attaches_its_own_poles(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(
            mock_cursor,
            [_row("cust1", "Acme", "proj1", "Downtown", 1, 1, 0)],
            [_pole_row("proj1", "poleA", "PN-A", "LOC-A", "Working")],
        )

        result = pole_vitals_api.get_pole_vitals(project_id="proj1")

        assert [p["id"] for p in result["poles"]] == ["poleA"]

    def test_both_queries_use_the_same_where_clause_and_params(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [], [])

        pole_vitals_api.get_pole_vitals(customer_id="cust1")

        first_call = mock_cursor.execute.call_args_list[0]
        second_call = mock_cursor.execute.call_args_list[1]
        # Same bound parameters passed to both queries (period type +
        # customer_id), even though the SQL text itself differs.
        assert first_call.args[1:] == second_call.args[1:]


class TestGetPoleVitalsUnfiltered:
    def test_no_params_queries_top_n_customers_ordered_by_name(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals()

        sql, period_type, limit = mock_cursor.execute.call_args.args
        assert "SELECT TOP (?) Id FROM Customers ORDER BY Name" in sql
        assert period_type == "Hour"
        assert limit == api_utils.DEFAULT_LIMIT

    def test_custom_limit_is_clamped_and_passed_through(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals(limit=99999)

        _, _, limit = mock_cursor.execute.call_args.args
        assert limit == api_utils.MAX_LIMIT

    def test_groups_multiple_projects_under_the_same_customer(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Downtown", 10, 8, 1),
            _row("cust1", "Acme", "proj2", "Uptown", 5, 5, 0),
            _row("cust2", "Globex", "proj3", "Main St", 20, 15, 2),
        ])

        result = pole_vitals_api.get_pole_vitals()

        assert len(result) == 2
        assert result[0]["id"] == "cust1"
        assert result[0]["name"] == "Acme"
        assert len(result[0]["projects"]) == 2
        assert result[0]["projects"][0]["id"] == "proj1"
        assert result[0]["projects"][1]["id"] == "proj2"
        assert result[1]["id"] == "cust2"
        assert len(result[1]["projects"]) == 1

    def test_preserves_customer_order_from_query(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """Grouping via a dict must not silently reorder customers --
        the SQL's ORDER BY c.Name is what determines order, and the
        Python grouping must preserve it."""
        _set_fetchall_results(mock_cursor, [
            _row("cust_z", "Zeta Corp", "proj1", "P1", 1, 1, 0),
            _row("cust_a", "Alpha Corp", "proj2", "P2", 1, 1, 0),
        ])

        result = pole_vitals_api.get_pole_vitals()

        assert [c["id"] for c in result] == ["cust_z", "cust_a"]

    def test_project_dict_shape_and_values(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(
            mock_cursor,
            [_row("cust1", "Acme", "proj1", "Downtown", 10, 8, 1, 1)],
            [_pole_row(
                "proj1", "pole1", "PN-001", "LOC-001",
                install_date="2023-05-10", lat=33.749, long_=-84.388,
                last_update="2026-07-25 14:30:00", battery_voltage_1=12.6, battery_voltage_2=12.4,
                light_status="Working", is_online=True,
                battery_percentage=87.5, panel_percentage=92.1, light_percentage=88.0,
            )],
        )

        result = pole_vitals_api.get_pole_vitals()

        project = result[0]["projects"][0]
        assert project == {
            "id": "proj1",
            "name": "Downtown",
            "totalLights": 10,
            "workingPercentage": 80.0,
            "optimisticWorkingPercentage": 90.0,
            "totalFaults": 1,
            "totalNonTelemetryAvailable": 1,
            "poles": [
                {
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
                }
            ],
        }

    def test_zero_pole_project_does_not_crash_and_shows_zero_percent(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Empty Site", 0, 0, 0),
        ])

        result = pole_vitals_api.get_pole_vitals()

        project = result[0]["projects"][0]
        assert project["totalLights"] == 0
        assert project["workingPercentage"] == 0.0
        assert project["totalFaults"] == 0

    def test_no_customers_returns_empty_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert pole_vitals_api.get_pole_vitals() == []


class TestGetPoleVitalsCustomerIdFilter:
    def test_customer_id_filters_by_customer_id_column(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals(customer_id="cust1")

        sql, period_type, customer_id = mock_cursor.execute.call_args.args
        assert "WHERE c.Id = ?" in sql
        assert period_type == "Hour"
        assert customer_id == "cust1"

    def test_returns_a_single_dict_not_a_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Downtown", 10, 8, 1),
            _row("cust1", "Acme", "proj2", "Uptown", 5, 5, 0),
        ])

        result = pole_vitals_api.get_pole_vitals(customer_id="cust1")

        assert isinstance(result, dict)
        assert result["id"] == "cust1"
        assert result["name"] == "Acme"
        assert len(result["projects"]) == 2

    def test_nonexistent_customer_returns_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert pole_vitals_api.get_pole_vitals(customer_id="does-not-exist") is None

    def test_customer_with_zero_projects_returns_empty_projects_list_not_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        """The LEFT JOIN Projects produces one 'phantom' row for a
        project-less customer, with every project column NULL -- this
        must become {"id":..., "name":..., "projects": []}, distinct
        from a nonexistent customer (which produces zero rows at all)."""
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", None, None, None, None, None, None),
        ])

        result = pole_vitals_api.get_pole_vitals(customer_id="cust1")

        assert result == {
            "id": "cust1",
            "name": "Acme",
            "totalLights": 0,
            "workingPercentage": 0.0,
            "optimisticWorkingPercentage": 0.0,
            "totalFaults": 0,
            "totalNonTelemetryAvailable": 0,
            "projects": [],
        }


class TestGetPoleVitalsCustomerWithZeroProjectsUnfiltered:
    def test_customer_with_zero_projects_still_appears_with_empty_list(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Downtown", 10, 8, 1, 1),
            _row("cust2", "Globex", None, None, None, None, None, None),
        ])

        result = pole_vitals_api.get_pole_vitals()

        assert len(result) == 2
        assert result[1] == {
            "id": "cust2",
            "name": "Globex",
            "totalLights": 0,
            "workingPercentage": 0.0,
            "optimisticWorkingPercentage": 0.0,
            "totalFaults": 0,
            "totalNonTelemetryAvailable": 0,
            "projects": [],
        }


class TestGetPoleVitalsProjectIdFilter:
    def test_project_id_filters_by_project_id_column(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals(project_id="proj1")

        sql, period_type, project_id = mock_cursor.execute.call_args.args
        assert "WHERE proj.Id = ?" in sql
        assert period_type == "Hour"
        assert project_id == "proj1"

    def test_project_id_and_customer_id_combined_both_apply(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals(project_id="proj1", customer_id="cust1")

        sql, period_type, project_id, customer_id = mock_cursor.execute.call_args.args
        assert "WHERE proj.Id = ? AND c.Id = ?" in sql
        assert period_type == "Hour"
        assert project_id == "proj1"
        assert customer_id == "cust1"

    def test_returns_a_flat_dict_not_nested(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        _set_fetchall_results(mock_cursor, [
            _row("cust1", "Acme", "proj1", "Downtown", 10, 8, 1, 1),
        ])

        result = pole_vitals_api.get_pole_vitals(project_id="proj1")

        assert result == {
            "id": "proj1",
            "name": "Downtown",
            "totalLights": 10,
            "workingPercentage": 80.0,
            "optimisticWorkingPercentage": 90.0,
            "totalFaults": 1,
            "totalNonTelemetryAvailable": 1,
            "poles": [],
            "customerId": "cust1",
            "customerName": "Acme",
        }

    def test_nonexistent_project_returns_none(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert pole_vitals_api.get_pole_vitals(project_id="does-not-exist") is None

    def test_limit_is_ignored_when_project_id_given(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        pole_vitals_api.get_pole_vitals(project_id="proj1", limit=5)

        args = mock_cursor.execute.call_args.args
        assert len(args) == 3  # sql, period_type, project_id -- no limit param bound
