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


def patch_all_loaders(mocker):
    """
    Patches load_poles, load_projects, and load_customers, tracking call
    order via a shared list so tests can assert Poles -> Projects -> Customers.
    """
    call_order = []
    mock_poles = mocker.patch(
        "function_app.load_poles", side_effect=lambda: call_order.append("poles")
    )
    mock_projects = mocker.patch(
        "function_app.load_projects", side_effect=lambda: call_order.append("projects")
    )
    mock_customers = mocker.patch(
        "function_app.load_customers", side_effect=lambda: call_order.append("customers")
    )
    return mock_poles, mock_projects, mock_customers, call_order


# --------------------------------------------------------------------------
# loadAirTableData (timer trigger)
# --------------------------------------------------------------------------


class TestLoadAirTableDataTimer:
    @freeze_time("2026-07-13 10:00:00")  # 6:00 AM EDT
    def test_runs_at_6am_eastern_summer(self, mocker):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request())
        mock_poles.assert_called_once()
        mock_projects.assert_called_once()
        mock_customers.assert_called_once()

    @freeze_time("2026-07-13 22:00:00")  # 6:00 PM EDT
    def test_runs_at_6pm_eastern_summer(self, mocker):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request())
        mock_poles.assert_called_once()
        mock_projects.assert_called_once()
        mock_customers.assert_called_once()

    @freeze_time("2026-01-13 11:00:00")  # 6:00 AM EST (winter, DST-proof check)
    def test_runs_at_6am_eastern_winter(self, mocker):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request())
        mock_poles.assert_called_once()
        mock_projects.assert_called_once()
        mock_customers.assert_called_once()

    @freeze_time("2026-07-13 23:00:00")  # 7:00 PM EDT -- not a target hour
    def test_skips_outside_target_hours(self, mocker):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request())
        mock_poles.assert_not_called()
        mock_projects.assert_not_called()
        mock_customers.assert_not_called()

    @freeze_time("2026-07-13 14:00:00")  # 10:00 AM EDT -- not a target hour
    def test_skips_midday(self, mocker):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request())
        mock_poles.assert_not_called()
        mock_projects.assert_not_called()
        mock_customers.assert_not_called()

    @freeze_time("2026-07-13 10:00:00")  # 6:00 AM EDT, target hour
    def test_past_due_still_runs_and_logs_warning(self, mocker, caplog):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        with caplog.at_level("WARNING"):
            function_app.loadAirTableData(make_timer_request(past_due=True))
        mock_poles.assert_called_once()
        mock_projects.assert_called_once()
        mock_customers.assert_called_once()
        assert any("past due" in rec.message for rec in caplog.records)

    @freeze_time("2026-07-13 23:00:00")  # not a target hour
    def test_past_due_outside_target_hour_still_skips_load(self, mocker):
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request(past_due=True))
        mock_poles.assert_not_called()
        mock_projects.assert_not_called()
        mock_customers.assert_not_called()

    @freeze_time("2026-07-13 10:00:00")
    def test_poles_runs_before_projects_before_customers(self, mocker):
        _, _, _, call_order = patch_all_loaders(mocker)
        function_app.loadAirTableData(make_timer_request())
        assert call_order == ["poles", "projects", "customers"]

    @freeze_time("2026-07-13 10:00:00")
    def test_propagates_exception_from_load_customers(self, mocker):
        mocker.patch("function_app.load_poles")
        mocker.patch("function_app.load_projects")
        mocker.patch("function_app.load_customers", side_effect=RuntimeError("db down"))
        with pytest.raises(RuntimeError, match="db down"):
            function_app.loadAirTableData(make_timer_request())

    @freeze_time("2026-07-13 10:00:00")
    def test_later_loaders_not_called_if_load_poles_fails(self, mocker):
        """
        Poles runs first with no exception handling around it in
        loadAirTableData, so a failure there prevents Projects and
        Customers from running at all in this invocation (they'll get
        another shot at the next scheduled hour).
        """
        mocker.patch("function_app.load_poles", side_effect=RuntimeError("poles failed"))
        mock_projects = mocker.patch("function_app.load_projects")
        mock_customers = mocker.patch("function_app.load_customers")

        with pytest.raises(RuntimeError, match="poles failed"):
            function_app.loadAirTableData(make_timer_request())

        mock_projects.assert_not_called()
        mock_customers.assert_not_called()

    @freeze_time("2026-07-13 10:00:00")
    def test_load_customers_not_called_if_load_projects_fails(self, mocker):
        mocker.patch("function_app.load_poles")
        mocker.patch("function_app.load_projects", side_effect=RuntimeError("projects failed"))
        mock_customers = mocker.patch("function_app.load_customers")

        with pytest.raises(RuntimeError, match="projects failed"):
            function_app.loadAirTableData(make_timer_request())

        mock_customers.assert_not_called()


# --------------------------------------------------------------------------
# loadAirTableDataManual (HTTP trigger)
# --------------------------------------------------------------------------


class TestLoadAirTableDataManual:
    def test_blocked_in_prod(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Prod")
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)

        response = function_app.loadAirTableDataManual(make_http_request())

        assert response.status_code == 403
        mock_poles.assert_not_called()
        mock_projects.assert_not_called()
        mock_customers.assert_not_called()

    def test_runs_when_not_prod(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)

        response = function_app.loadAirTableDataManual(make_http_request())

        assert response.status_code == 200
        assert response.get_body() == b"loadPoles + loadProjects + loadCustomers run complete."
        mock_poles.assert_called_once()
        mock_projects.assert_called_once()
        mock_customers.assert_called_once()

    def test_poles_runs_before_projects_before_customers(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        _, _, _, call_order = patch_all_loaders(mocker)

        function_app.loadAirTableDataManual(make_http_request())

        assert call_order == ["poles", "projects", "customers"]

    def test_runs_when_environment_unset_defaults_to_dev_behavior(self, mocker, monkeypatch):
        # ENVIRONMENT defaults to "Dev" for any value other than "Prod"
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Staging")
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)

        response = function_app.loadAirTableDataManual(make_http_request())

        assert response.status_code == 200
        mock_poles.assert_called_once()
        mock_projects.assert_called_once()
        mock_customers.assert_called_once()

    def test_is_synchronous_exception_propagates_to_caller(self, mocker, monkeypatch):
        """
        Locks in current behavior: all three loaders are called directly in
        the request-handling path (no background thread), so a failure
        propagates out of the handler rather than being swallowed. If
        fire-and-forget threading is reintroduced later, this test will
        start failing and should be updated deliberately.
        """
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mocker.patch("function_app.load_poles")
        mocker.patch("function_app.load_projects")
        mocker.patch("function_app.load_customers", side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError, match="db down"):
            function_app.loadAirTableDataManual(make_http_request())

    def test_later_loaders_not_called_if_load_poles_fails(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mocker.patch("function_app.load_poles", side_effect=RuntimeError("poles failed"))
        mock_projects = mocker.patch("function_app.load_projects")
        mock_customers = mocker.patch("function_app.load_customers")

        with pytest.raises(RuntimeError, match="poles failed"):
            function_app.loadAirTableDataManual(make_http_request())

        mock_projects.assert_not_called()
        mock_customers.assert_not_called()

    def test_load_customers_not_called_if_load_projects_fails(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mocker.patch("function_app.load_poles")
        mocker.patch("function_app.load_projects", side_effect=RuntimeError("projects failed"))
        mock_customers = mocker.patch("function_app.load_customers")

        with pytest.raises(RuntimeError, match="projects failed"):
            function_app.loadAirTableDataManual(make_http_request())

        mock_customers.assert_not_called()


# --------------------------------------------------------------------------
# loadLeadsunData / loadLeadsunDataManual (Leadsun, separate from
# loadAirTableData -- different source, different cadence, no dependency
# between the two pipelines). Renamed from loadPoleRawData now that it
# orchestrates two loaders (load_pole_models -> load_pole_telemetry), not one.
# --------------------------------------------------------------------------


def make_leadsun_http_request():
    return func.HttpRequest(
        method="POST",
        url="/api/loadLeadsunDataManual",
        headers={},
        params={},
        body=b"",
    )


def patch_leadsun_loaders(mocker):
    """
    Patches load_pole_models and load_pole_telemetry, tracking call order via
    a shared list so tests can assert Model runs before RawData.
    """
    call_order = []
    mock_model = mocker.patch(
        "function_app.load_pole_models", side_effect=lambda: call_order.append("model")
    )
    mock_raw_data = mocker.patch(
        "function_app.load_pole_telemetry", side_effect=lambda: call_order.append("raw_data")
    )
    return mock_model, mock_raw_data, call_order


class TestLoadLeadsunDataTimer:
    def test_runs_unconditionally(self, mocker):
        """Unlike loadAirTableData, there's no hour-gating -- every timer
        fire (every 10 minutes) should call both loaders."""
        mock_model, mock_raw_data, _ = patch_leadsun_loaders(mocker)
        function_app.loadLeadsunData(make_timer_request())
        mock_model.assert_called_once()
        mock_raw_data.assert_called_once()

    def test_model_runs_before_raw_data(self, mocker):
        _, _, call_order = patch_leadsun_loaders(mocker)
        function_app.loadLeadsunData(make_timer_request())
        assert call_order == ["model", "raw_data"]

    def test_past_due_still_runs_and_logs_warning(self, mocker, caplog):
        mock_model, mock_raw_data, _ = patch_leadsun_loaders(mocker)
        with caplog.at_level("WARNING"):
            function_app.loadLeadsunData(make_timer_request(past_due=True))
        mock_model.assert_called_once()
        mock_raw_data.assert_called_once()
        assert any("past due" in rec.message for rec in caplog.records)

    def test_propagates_exception(self, mocker):
        mocker.patch("function_app.load_pole_models")
        mocker.patch(
            "function_app.load_pole_telemetry", side_effect=RuntimeError("leadsun down")
        )
        with pytest.raises(RuntimeError, match="leadsun down"):
            function_app.loadLeadsunData(make_timer_request())

    def test_raw_data_not_called_if_model_fails(self, mocker):
        """Model runs first with no exception handling around it, so a
        failure there prevents RawData from running at all in this
        invocation."""
        mocker.patch("function_app.load_pole_models", side_effect=RuntimeError("model failed"))
        mock_raw_data = mocker.patch("function_app.load_pole_telemetry")

        with pytest.raises(RuntimeError, match="model failed"):
            function_app.loadLeadsunData(make_timer_request())

        mock_raw_data.assert_not_called()

    def test_does_not_touch_airtable_loaders(self, mocker):
        """loadLeadsunData is a separate function -- it must not call any
        of the Airtable-sourced loaders."""
        mock_model, mock_raw_data, _ = patch_leadsun_loaders(mocker)
        mock_poles, mock_projects, mock_customers, _ = patch_all_loaders(mocker)

        function_app.loadLeadsunData(make_timer_request())

        mock_model.assert_called_once()
        mock_raw_data.assert_called_once()
        mock_poles.assert_not_called()
        mock_projects.assert_not_called()
        mock_customers.assert_not_called()


class TestLoadLeadsunDataManual:
    def test_blocked_in_prod(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Prod")
        mock_model, mock_raw_data, _ = patch_leadsun_loaders(mocker)

        response = function_app.loadLeadsunDataManual(make_leadsun_http_request())

        assert response.status_code == 403
        mock_model.assert_not_called()
        mock_raw_data.assert_not_called()

    def test_runs_when_not_prod(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mock_model, mock_raw_data, _ = patch_leadsun_loaders(mocker)

        response = function_app.loadLeadsunDataManual(make_leadsun_http_request())

        assert response.status_code == 200
        assert response.get_body() == b"loadPoleModels + loadPoleTelemetry run complete."
        mock_model.assert_called_once()
        mock_raw_data.assert_called_once()

    def test_model_runs_before_raw_data(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        _, _, call_order = patch_leadsun_loaders(mocker)

        function_app.loadLeadsunDataManual(make_leadsun_http_request())

        assert call_order == ["model", "raw_data"]

    def test_is_synchronous_exception_propagates_to_caller(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mocker.patch("function_app.load_pole_models")
        mocker.patch(
            "function_app.load_pole_telemetry", side_effect=RuntimeError("leadsun down")
        )

        with pytest.raises(RuntimeError, match="leadsun down"):
            function_app.loadLeadsunDataManual(make_leadsun_http_request())

    def test_raw_data_not_called_if_model_fails(self, mocker, monkeypatch):
        monkeypatch.setattr(function_app, "ENVIRONMENT", "Dev")
        mocker.patch("function_app.load_pole_models", side_effect=RuntimeError("model failed"))
        mock_raw_data = mocker.patch("function_app.load_pole_telemetry")

        with pytest.raises(RuntimeError, match="model failed"):
            function_app.loadLeadsunDataManual(make_leadsun_http_request())

        mock_raw_data.assert_not_called()
