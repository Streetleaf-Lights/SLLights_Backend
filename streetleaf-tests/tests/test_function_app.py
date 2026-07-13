"""Tests for function_app.py (timer trigger + manual HTTP trigger)"""

from unittest.mock import MagicMock

import azure.functions as func
import pytest
from freezegun import freeze_time

import function_app


def make_timer_request(past_due=False):
    timer = MagicMock(spec=func.TimerRequest)
    timer.past_due = past_due
    return timer


def make_http_request():
    return func.HttpRequest(
        method="POST",
        url="/api/loadAirTableDataManual",
        headers={},
        params={},
        body=b"",
    )


# --------------------------------------------------------------------------
# loadAirTableData (timer trigger)
# --------------------------------------------------------------------------


class TestLoadAirTableDataTimer:
    @freeze_time("2026-07-13 10:00:00")  # 6:00 AM EDT
    def test_runs_at_6am_eastern_summer(self, mocker):
        mock_load = mocker.patch("function_app.load_customers")
        function_app.loadAirTableData(make_timer_request())
        mock_load.assert_called_once()

    @freeze_time("2026-07-13 22:00:00")  # 6:00 PM EDT
    def test_runs_at_6pm_eastern_summer(self, mocker):
        mock_load = mocker.patch("function_app.load_customers")
        function_app.loadAirTableData(make_timer_request())
        mock_load.assert_called_once()

    @freeze_time("2026-01-13 11:00:00")  # 6:00 AM EST (winter, DST-proof check)
    def test_runs_at_6am_eastern_winter(self, mocker):
        mock_load = mocker.patch("function_app.load_customers")
        function_app.loadAirTableData(make_timer_request())
        mock_load.assert_called_once()

    @freeze_time("2026-07-13 23:00:00")  # 7:00 PM EDT -- not a target hour
    def test_skips_outside_target_hours(self, mocker):
        mock_load = mocker.patch("function_app.load_customers")
        function_app.loadAirTableData(make_timer_request())
        mock_load.assert_not_called()

    @freeze_time("2026-07-13 14:00:00")  # 10:00 AM EDT -- not a target hour
    def test_skips_midday(self, mocker):
        mock_load = mocker.patch("function_app.load_customers")
        function_app.loadAirTableData(make_timer_request())
        mock_load.assert_not_called()

    @freeze_time("2026-07-13 10:00:00")  # 6:00 AM EDT, target hour
    def test_past_due_still_runs_and_logs_warning(self, mocker, caplog):
        mock_load = mocker.patch("function_app.load_customers")
        with caplog.at_level("WARNING"):
            function_app.loadAirTableData(make_timer_request(past_due=True))
        mock_load.assert_called_once()
        assert any("past due" in rec.message for rec in caplog.records)

    @freeze_time("2026-07-13 23:00:00")  # not a target hour
    def test_past_due_outside_target_hour_still_skips_load(self, mocker):
        mock_load = mocker.patch("function_app.load_customers")
        function_app.loadAirTableData(make_timer_request(past_due=True))
        mock_load.assert_not_called()

    @freeze_time("2026-07-13 10:00:00")
    def test_propagates_exception_from_load_customers(self, mocker):
        mocker.patch("function_app.load_customers", side_effect=RuntimeError("db down"))
        with pytest.raises(RuntimeError, match="db down"):
            function_app.loadAirTableData(make_timer_request())


# --------------------------------------------------------------------------
# loadAirTableDataManual (HTTP trigger)
# --------------------------------------------------------------------------


class TestLoadAirTableDataManual:
    def test_blocked_in_prod(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Prod")
        mock_load = mocker.patch("function_app.load_customers")

        response = function_app.loadAirTableDataManual(make_http_request())

        assert response.status_code == 403
        mock_load.assert_not_called()

    def test_runs_when_not_prod(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mock_load = mocker.patch("function_app.load_customers")

        response = function_app.loadAirTableDataManual(make_http_request())

        assert response.status_code == 200
        assert response.get_body() == b"loadCustomers run complete."
        mock_load.assert_called_once()

    def test_runs_when_environment_unset_defaults_to_dev_behavior(self, mocker, monkeypatch):
        # ENVIRONMENT defaults to "Dev" for any value other than "Prod"
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Staging")
        mock_load = mocker.patch("function_app.load_customers")

        response = function_app.loadAirTableDataManual(make_http_request())

        assert response.status_code == 200
        mock_load.assert_called_once()

    def test_is_synchronous_exception_propagates_to_caller(self, mocker, monkeypatch):
        """
        Locks in current behavior: load_customers() is called directly in
        the request-handling path (no background thread), so a failure in
        load_customers() propagates out of the handler rather than being
        swallowed. If fire-and-forget threading is reintroduced later, this
        test will start failing and should be updated deliberately.
        """
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mocker.patch("function_app.load_customers", side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError, match="db down"):
            function_app.loadAirTableDataManual(make_http_request())
