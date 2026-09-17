"""Tests for shared/provisioned_telemetry_loader.py"""

import json
from unittest.mock import MagicMock

import pytest

from shared import provisioned_telemetry_loader as m

_REAL_EVENT = {
    "Timestamp": 1789391964,
    "PoleID": "0a10aced202194944a071358",
    "LampPower1": 0,
    "BatterySoC": 100,
    "LightRatio": 0,
    "PanelPercentage": 38.2,
    "SolarBoardVoltage": 38.9,
    "SolarBoardCurrent": 1.7,
    "BatteryElecCurrent": 2.2,
    "BatteryVoltage": 26.8,
    "LocationCoordinates": {"Latitude": 27.86201, "Longitude": -82.34443},
    "StreetlightMode": "Default",
    "BatteryFault": 0,
    "MPPTFault": 0,
    "LEDFault": 0,
    "ControllerFault": 0,
}


class TestEpochSecondsToDtoString:
    def test_converts_to_utc_offset_string(self):
        # 1700000000 -> 2023-11-14 22:13:20 UTC
        result = m._epoch_seconds_to_dto_string(1700000000)
        assert result == "2023-11-14 22:13:20.000 +00:00"

    def test_always_utc_offset_never_a_different_offset(self):
        result = m._epoch_seconds_to_dto_string(1789391964)
        assert result.endswith("+00:00")


class TestIsAzureMonitorDiagnosticsEnvelope:
    def test_metrics_shape_is_recognized(self):
        event = {"records": [{"metricName": "ResourceUtilization", "resourceId": "/X"}]}
        assert m._is_azure_monitor_diagnostics_envelope(event) is True

    def test_activity_log_shape_is_recognized(self):
        event = {"records": [{"operationName": "Receive Events: ", "status": "Failed"}]}
        assert m._is_azure_monitor_diagnostics_envelope(event) is True

    def test_real_telemetry_event_is_not_recognized(self):
        assert m._is_azure_monitor_diagnostics_envelope(_REAL_EVENT) is False

    def test_records_present_but_pole_id_also_present_is_not_recognized(self):
        """A real telemetry event could theoretically be extended with
        its own unrelated "records" field someday -- PoleID's presence
        must always win, regardless of "records"."""
        event = {"PoleID": "uid-1", "records": [{"anything": "here"}]}
        assert m._is_azure_monitor_diagnostics_envelope(event) is False

    def test_records_not_a_list_is_not_recognized(self):
        event = {"records": "not-a-list"}
        assert m._is_azure_monitor_diagnostics_envelope(event) is False

    def test_no_records_key_at_all_is_not_recognized(self):
        assert m._is_azure_monitor_diagnostics_envelope({"Timestamp": 123}) is False


class TestMapEventToTelemetryRow:
    def test_maps_pole_id_to_location_id(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["LocationId"] == "0a10aced202194944a071358"

    def test_maps_timestamp_to_last_upload(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["LastUpload"] == m._epoch_seconds_to_dto_string(1789391964)

    def test_maps_lamp_power_1_directly(self):
        result = m._map_event_to_telemetry_row({**_REAL_EVENT, "LampPower1": 42.5})
        assert result["LampPower1"] == 42.5

    def test_lamp_power_2_is_always_none(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["LampPower2"] is None

    def test_maps_solar_board_voltage_directly(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["SolarBoardVoltage"] == 38.9

    def test_maps_solar_board_current_to_solar_board_elec_current(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["SolarBoardElecCurrent"] == 1.7

    def test_maps_battery_voltage_to_battery_voltage_1(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["BatteryVoltage1"] == 26.8

    def test_battery_voltage_2_is_always_none(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["BatteryVoltage2"] is None

    def test_maps_battery_elec_current_to_battery_elec_current_1(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["BatteryElecCurrent1"] == 2.2

    def test_battery_elec_current_2_is_always_none(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["BatteryElecCurrent2"] is None

    def test_maps_location_coordinates_to_latitude_longitude(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["Latitude"] == 27.86201
        assert result["Longitude"] == -82.34443

    def test_missing_location_coordinates_gives_none_lat_long(self):
        event = {k: v for k, v in _REAL_EVENT.items() if k != "LocationCoordinates"}
        result = m._map_event_to_telemetry_row(event)
        assert result["Latitude"] is None
        assert result["Longitude"] is None

    def test_unmapped_fields_captured_in_extra_fields_json(self):
        """BatterySoC, LightRatio, PanelPercentage, BatteryFault, LEDFault,
        ControllerFault are now dedicated columns -- no longer in ExtraFieldsJson.
        MPPTFault and StreetlightMode remain in ExtraFieldsJson."""
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        extra = json.loads(result["ExtraFieldsJson"])
        assert extra == {
            "StreetlightMode": "Default",
            "MPPTFault": 0,
        }

    def test_known_fields_not_duplicated_into_extra_fields_json(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        extra = json.loads(result["ExtraFieldsJson"])
        for known in ("Timestamp", "PoleID", "LampPower1", "SolarBoardVoltage",
                      "SolarBoardCurrent", "BatteryElecCurrent", "BatteryVoltage",
                      "LocationCoordinates", "BatterySoC", "LightRatio",
                      "PanelPercentage", "BatteryFault", "LEDFault", "ControllerFault"):
            assert known not in extra, f"{known} should not be in ExtraFieldsJson"

    def test_no_extra_fields_gives_none_not_empty_json(self):
        minimal_event = {
            "Timestamp": 1789391964,
            "PoleID": "UID-1",
            "LampPower1": 0,
            "SolarBoardVoltage": 0,
            "SolarBoardCurrent": 0,
            "BatteryElecCurrent": 0,
            "BatteryVoltage": 0,
        }
        result = m._map_event_to_telemetry_row(minimal_event)
        assert result["ExtraFieldsJson"] is None

    def test_missing_timestamp_gives_none_last_upload_not_an_error(self):
        event = {k: v for k, v in _REAL_EVENT.items() if k != "Timestamp"}
        result = m._map_event_to_telemetry_row(event)
        assert result["LastUpload"] is None

    def test_pole_id_preserved_under_underscore_key_for_the_join(self):
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["_PoleID"] == "0a10aced202194944a071358"

    def test_is_online_is_always_true(self):
        """Per explicit request: receiving any telemetry event at all is
        treated as proof this pole is online, regardless of the
        event's own field values -- there's no source field this
        actually reads."""
        result = m._map_event_to_telemetry_row(_REAL_EVENT)
        assert result["IsOnline"] is True

    def test_is_online_is_true_even_with_zero_lamp_power_and_current(self):
        """Confirms this is a fixed assumption, not derived from any of
        the event's own readings (e.g. not "online if LampPower1 > 0")."""
        event = {**_REAL_EVENT, "LampPower1": 0, "BatteryElecCurrent": 0}
        result = m._map_event_to_telemetry_row(event)
        assert result["IsOnline"] is True


class TestBuildRow:
    def test_uses_provisioned_source_not_leadsun(self):
        mapped = m._map_event_to_telemetry_row(_REAL_EVENT)
        row = m._build_row(mapped, sp_exec_id=99, open_issue_provisioned_pole_ids=set(), provisioned_pole_timezones={})
        source_index = m._ALL_COLUMNS.index("Source")
        assert row[source_index] == "Provisioned"

    def test_is_open_issue_fault_false_when_not_in_set(self):
        mapped = m._map_event_to_telemetry_row(_REAL_EVENT)
        row = m._build_row(mapped, sp_exec_id=99, open_issue_provisioned_pole_ids=set(), provisioned_pole_timezones={})
        index = m._ALL_COLUMNS.index("IsOpenIssueFault")
        assert row[index] is False

    def test_row_length_matches_all_columns(self):
        mapped = m._map_event_to_telemetry_row(_REAL_EVENT)
        row = m._build_row(mapped, sp_exec_id=99, open_issue_provisioned_pole_ids=set(), provisioned_pole_timezones={})
        assert len(row) == len(m._ALL_COLUMNS)

    def test_location_id_lands_at_the_expected_position(self):
        mapped = m._map_event_to_telemetry_row(_REAL_EVENT)
        row = m._build_row(mapped, sp_exec_id=99, open_issue_provisioned_pole_ids=set(), provisioned_pole_timezones={})
        location_id_index = m._ALL_COLUMNS.index("LocationId")
        assert row[location_id_index] == "0a10aced202194944a071358"

    def test_is_online_true_lands_at_the_expected_position(self):
        mapped = m._map_event_to_telemetry_row(_REAL_EVENT)
        row = m._build_row(mapped, sp_exec_id=99, open_issue_provisioned_pole_ids=set(), provisioned_pole_timezones={})
        is_online_index = m._ALL_COLUMNS.index("IsOnline")
        assert row[is_online_index] is True

    def test_daylight_flags_not_in_all_columns(self):
        """IsDaylight/IsDaylightForLedFault/IsDaylightForPanelFault are NOT
        in _ALL_COLUMNS -- they're set by a separate UPDATE after the MERGE,
        not through the staging/MERGE upsert path."""
        for col in ("IsDaylight", "IsDaylightForLedFault", "IsDaylightForPanelFault"):
            assert col not in m._ALL_COLUMNS

    def test_daylight_flags_computed_when_timezone_resolved(self, mocker):
        """_compute_daylight_flags returns values when a timezone is resolved."""
        mocker.patch("shared.provisioned_telemetry_loader._is_daylight", return_value=True)
        from datetime import datetime, timezone as tz
        dt = datetime(2026, 9, 15, 14, 0, tzinfo=tz.utc)
        is_day, is_day_led, is_day_panel = m._compute_daylight_flags(dt, 27.78, -82.34)
        assert is_day is not None
        assert is_day_led is not None
        assert is_day_panel is not None

    def test_daylight_flags_null_on_compute_error(self, mocker):
        """If is_daylight() raises, _compute_daylight_flags returns (None, None, None)."""
        mocker.patch("shared.provisioned_telemetry_loader._is_daylight", side_effect=ValueError("bad tz"))
        from datetime import datetime, timezone as tz
        dt = datetime(2026, 9, 15, 14, 0, tzinfo=tz.utc)
        result = m._compute_daylight_flags(dt, 27.78, -82.34)
        assert result == (None, None, None)


class TestFetchProvisionedPoleTimezones:
    def test_returns_dict_keyed_by_provisioned_pole_id(self, mocker):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("pole-abc", 27.78, -82.34), ("pole-xyz", 28.0, -81.0)]
        result = m._fetch_provisioned_pole_timezones(cursor)
        assert result == {"pole-abc": (27.78, -82.34), "pole-xyz": (28.0, -81.0)}

    def test_returns_empty_dict_when_no_rows(self, mocker):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        result = m._fetch_provisioned_pole_timezones(cursor)
        assert result == {}

    def test_executes_select_on_pole_time_zones(self, mocker):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        m._fetch_provisioned_pole_timezones(cursor)
        sql = cursor.execute.call_args.args[0]
        assert "PoleTimeZones" in sql
        assert "ProvisionedPoleId IS NOT NULL" in sql
        assert "WindowsTimeZone IS NOT NULL" in sql


class TestComputeDaylightFlags:
    def test_returns_three_nones_when_last_upload_is_none(self):
        result = m._compute_daylight_flags(None, 27.78, -82.34)
        assert result == (None, None, None)

    def test_returns_three_nones_on_exception(self, mocker):
        mocker.patch("shared.provisioned_telemetry_loader._is_daylight", side_effect=Exception("tz error"))
        from datetime import datetime, timezone
        dt = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        result = m._compute_daylight_flags(dt, 27.78, -82.34)
        assert result == (None, None, None)

    def test_returns_computed_values_on_success(self, mocker):
        mocker.patch("shared.provisioned_telemetry_loader._is_daylight", return_value=True)
        from datetime import datetime, timezone
        dt = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        is_day, is_day_led, is_day_panel = m._compute_daylight_flags(dt, 27.78, -82.34)
        assert is_day is not None
        assert is_day_led is not None
        assert is_day_panel is not None


class TestProcessProvisionedTelemetryEvents:
    def test_full_success_flow(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)

        m.process_provisioned_telemetry_events([_REAL_EVENT])

        insert_call = mock_cursor.execute.call_args_list[0]
        assert insert_call.args[1] == "loadProvisionedPoleTelemetry"
        assert insert_call.args[4] == "Provisioned"

        final_update = mock_cursor.execute.call_args_list[-1]
        assert final_update.args[0].strip().startswith("UPDATE SP_Execution")
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 0)

    def test_updates_poles_lat_long_when_coordinates_present(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)

        m.process_provisioned_telemetry_events([_REAL_EVENT])

        executed = [c.args for c in mock_cursor.execute.call_args_list]
        lat_long_calls = [
            args for args in executed if args[0] == m._UPDATE_POLES_LAT_LONG_SQL
        ]
        assert len(lat_long_calls) == 1
        _, lat, long_, pole_id = lat_long_calls[0]
        assert (lat, long_, pole_id) == (27.86201, -82.34443, "0a10aced202194944a071358")

    def test_skips_lat_long_update_when_coordinates_missing(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        event_no_coords = {k: v for k, v in _REAL_EVENT.items() if k != "LocationCoordinates"}

        m.process_provisioned_telemetry_events([event_no_coords])

        executed = [c.args[0] for c in mock_cursor.execute.call_args_list]
        assert m._UPDATE_POLES_LAT_LONG_SQL not in executed

    def test_multiple_events_all_processed(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        event_2 = {**_REAL_EVENT, "PoleID": "different-uid", "Timestamp": 1789391970}

        m.process_provisioned_telemetry_events([_REAL_EVENT, event_2])

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (2, 0)

    def test_empty_batch_still_completes_successfully(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)

        m.process_provisioned_telemetry_events([])  # must not raise

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 0)

    def test_event_with_no_pole_id_is_skipped_before_any_db_write(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor, caplog
    ):
        """Regression guard for a real production incident: an event
        with no usable PoleID must be validated and skipped BEFORE
        attempting a staging insert -- not allowed to hit the database's
        own NOT NULL constraint on LocationId, which previously produced
        a much less informative SQL-level error with no visibility into
        what the actual bad payload looked like."""
        mock_cursor.fetchone.return_value = (42,)
        bad_event = {"Timestamp": 1789391964, "LampPower1": 0}  # no "PoleID" key at all

        with caplog.at_level("ERROR"):
            m.process_provisioned_telemetry_events([bad_event])

        # Never even attempted the staging table / bulk insert -- only
        # the SP_Execution insert and final update should have executed.
        executed_sql = [c.args[0] for c in mock_cursor.execute.call_args_list]
        assert not any("Staging_PoleSerials" in sql or "PoleTelemetryStaging" in sql for sql in executed_sql)
        assert not mock_cursor.executemany.called

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("skipping event" in msg and "1789391964" in msg for msg in error_messages)

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 1)

    def test_event_with_no_pole_id_does_not_update_poles_lat_long_either(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        bad_event = {
            "Timestamp": 1789391964,
            "LocationCoordinates": {"Latitude": 27.0, "Longitude": -82.0},
        }  # coordinates present, but no PoleID

        m.process_provisioned_telemetry_events([bad_event])

        executed_sql = [c.args[0] for c in mock_cursor.execute.call_args_list]
        assert m._UPDATE_POLES_LAT_LONG_SQL not in executed_sql

    def test_missing_timestamp_is_also_skipped_before_any_db_write(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        bad_event = {"PoleID": "uid-1", "LampPower1": 0}  # no "Timestamp" key at all

        m.process_provisioned_telemetry_events([bad_event])

        assert not mock_cursor.executemany.called
        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 1)

    def test_one_bad_event_does_not_block_a_good_one_in_the_same_batch(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        bad_event = {"Timestamp": 1789391964}  # no PoleID
        good_event = _REAL_EVENT

        m.process_provisioned_telemetry_events([bad_event, good_event])

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 1)

    def test_azure_monitor_metrics_envelope_is_not_counted_as_an_error(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor, caplog
    ):
        """Regression guard for real production noise: Azure Monitor
        metrics data landing on this same Event Hub (via a Diagnostic
        Settings misconfiguration upstream) is recognized and ignored,
        not counted as a business-logic error."""
        metrics_envelope = {
            "records": [
                {
                    "count": 6, "total": 42, "resourceId": "/SUBSCRIPTIONS/.../IOTHUB-STREAM",
                    "time": "2026-09-14T15:48:00Z", "metricName": "ResourceUtilization",
                    "timeGrain": "PT1M",
                }
            ]
        }

        with caplog.at_level("INFO"):
            m.process_provisioned_telemetry_events([metrics_envelope])

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 0)  # NOT counted as an error

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        info_messages = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
        assert not any("skipping event" in msg for msg in error_messages)
        assert any("ignoring an Azure Monitor diagnostics event" in msg for msg in info_messages)

    def test_azure_monitor_activity_log_envelope_is_not_counted_as_an_error(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        """Same recognition, for the OTHER confirmed shape -- a resource/
        activity log entry, not a metric."""
        log_envelope = {
            "records": [
                {
                    "Environment": "Prod", "operationName": "Receive Events: ",
                    "category": "Execution", "status": "Failed", "level": "Error",
                    "resourceId": "/SUBSCRIPTIONS/.../IOTHUB-STREAM",
                }
            ]
        }

        m.process_provisioned_telemetry_events([log_envelope])

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 0)

    def test_malformed_telemetry_looking_event_still_counts_as_a_real_error(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor, caplog
    ):
        """The distinction must cut both ways: an event with no "records"
        key and no PoleID still looks like it was SUPPOSED to be real
        telemetry -- still a genuine, counted error, not silently
        swallowed just because it also lacks a PoleID."""
        genuinely_malformed = {"Timestamp": 1789391964, "LampPower1": 0}

        with caplog.at_level("ERROR"):
            m.process_provisioned_telemetry_events([genuinely_malformed])

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 1)

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("skipping event" in msg for msg in error_messages)

    def test_azure_monitor_envelope_mixed_with_a_good_event_in_same_batch(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        metrics_envelope = {"records": [{"metricName": "ResourceUtilization"}]}

        m.process_provisioned_telemetry_events([metrics_envelope, _REAL_EVENT])

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 0)  # diagnostics envelope ignored, not an error

    def test_only_one_connection_opened_for_the_whole_invocation(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        """Unlike the timer-triggered loaders, there's no fetch phase to
        avoid holding a connection open across -- one connection for the
        whole invocation is correct here, not a bug."""
        mock_cursor.fetchone.return_value = (42,)

        m.process_provisioned_telemetry_events([_REAL_EVENT])

        assert patch_get_connection_provisioned_telemetry.call_count == 1
        mock_conn.close.assert_called_once()

    def test_row_level_failure_falls_back_and_is_counted(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        mock_cursor.fetchall.return_value = []  # open_issues + timezones both return empty
        event_2 = {**_REAL_EVENT, "PoleID": "different-uid"}
        mock_cursor.executemany.side_effect = RuntimeError("bulk merge failed")
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            None,  # _fetch_provisioned_pole_ids_with_open_issues
            None,  # _fetch_provisioned_pole_timezones
            None,  # staging table create
            None,  # truncate staging (after bulk failure)
            None,  # row 1 upsert succeeds
            RuntimeError("row 2 failed"),  # row 2 upsert fails
            None,  # lat/long update for event 1
            None,  # lat/long update for event 2
            None,  # final SP_Execution update
        ]

        m.process_provisioned_telemetry_events([_REAL_EVENT, event_2])  # must not raise

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 1)

    def test_database_failure_is_recorded_and_reraised(
        self, patch_get_connection_provisioned_telemetry, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (42,)
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            RuntimeError("connection lost"),  # staging table create fails
        ]

        with pytest.raises(RuntimeError, match="connection lost"):
            m.process_provisioned_telemetry_events([_REAL_EVENT])

        error_update = mock_cursor.execute.call_args_list[-1]
        assert "ErrorMessage" in error_update.args[0]
        assert "connection lost" in error_update.args[2]
