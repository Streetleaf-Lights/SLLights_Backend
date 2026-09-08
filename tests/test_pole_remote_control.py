"""Tests for shared/pole_remote_control.py"""

import time as time_module

import jwt
import pytest

from shared import pole_remote_control as m


def _make_jwt(exp_seconds_from_now: float) -> str:
    """A real, decodable JWT with the given expiry -- signature doesn't
    matter since _decode_expiry() never verifies it."""
    return jwt.encode(
        {"exp": time_module.time() + exp_seconds_from_now}, "any-secret", algorithm="HS256"
    )


class TestFetchLatestTelemetryForPole:
    def test_pole_not_found_raises(self, patch_get_connection_pole_remote_control, mock_cursor):
        mock_cursor.fetchone.return_value = None  # "SELECT 1 FROM Poles" finds nothing

        with pytest.raises(m.PoleNotFoundError, match="12009-1000-A"):
            m._fetch_latest_telemetry_for_pole("12009-1000-A")

    def test_pole_exists_but_no_telemetry_raises(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [
            (1,),  # pole exists
            None,  # no matching telemetry row
        ]

        with pytest.raises(m.PoleTelemetryNotFoundError, match="12009-1000-A"):
            m._fetch_latest_telemetry_for_pole("12009-1000-A")

    def test_returns_telemetry_row_when_found(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [
            (1,),
            ("SLDEMOS", 1458, "GT18L94A25082883", "A3P70LA323110598"),
        ]

        result = m._fetch_latest_telemetry_for_pole("12009-1000-A")

        assert result == ("SLDEMOS", 1458, "GT18L94A25082883", "A3P70LA323110598")

    def test_closes_cursor_and_connection(
        self, patch_get_connection_pole_remote_control, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [(1,), ("SLDEMOS", 1458, "GT", "CC")]

        m._fetch_latest_telemetry_for_pole("12009-1000-A")

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestFetchLeadsunEdgePassword:
    def test_no_matching_account_raises(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(m.LeadsunEdgeAccountNotFoundError, match="SLDEMOS"):
            m._fetch_leadsun_edge_password("SLDEMOS")

    def test_decrypts_and_returns_password(
        self, patch_get_connection_pole_remote_control, mock_cursor, mocker
    ):
        mock_cursor.fetchone.return_value = ("encrypted-blob",)
        mocker.patch(
            "shared.pole_remote_control.decrypt_secret", return_value="real-password"
        )

        result = m._fetch_leadsun_edge_password("SLDEMOS")

        assert result == "real-password"


class TestGetAccessToken:
    def setup_method(self):
        m._token_cache.clear()

    def test_no_cache_calls_get_token(self, mocker):
        mock_get_token = mocker.patch(
            "shared.pole_remote_control.get_token",
            return_value={"access_token": _make_jwt(3600), "refresh_token": _make_jwt(604800)},
        )

        token = m._get_access_token("SLDEMOS", "hunter2")

        mock_get_token.assert_called_once_with("SLDEMOS", "hunter2")
        assert jwt.decode(token, options={"verify_signature": False})

    def test_fresh_cached_token_is_reused_without_any_api_call(self, mocker):
        mock_get_token = mocker.patch("shared.pole_remote_control.get_token")
        mock_refresh = mocker.patch("shared.pole_remote_control.refresh_token")
        m._token_cache["SLDEMOS"] = {
            "access_token": "CACHED_AT",
            "refresh_token": "CACHED_RT",
            "access_exp": time_module.time() + 3600,
            "refresh_exp": time_module.time() + 604800,
        }

        token = m._get_access_token("SLDEMOS", "hunter2")

        assert token == "CACHED_AT"
        mock_get_token.assert_not_called()
        mock_refresh.assert_not_called()

    def test_expired_access_but_valid_refresh_calls_refresh_token(self, mocker):
        mock_get_token = mocker.patch("shared.pole_remote_control.get_token")
        mock_refresh = mocker.patch(
            "shared.pole_remote_control.refresh_token",
            return_value={"access_token": _make_jwt(3600), "refresh_token": _make_jwt(604800)},
        )
        m._token_cache["SLDEMOS"] = {
            "access_token": "EXPIRED_AT",
            "refresh_token": "STILL_VALID_RT",
            "access_exp": time_module.time() - 10,  # already expired
            "refresh_exp": time_module.time() + 604800,
        }

        m._get_access_token("SLDEMOS", "hunter2")

        mock_refresh.assert_called_once_with("STILL_VALID_RT")
        mock_get_token.assert_not_called()

    def test_both_expired_calls_get_token(self, mocker):
        mock_get_token = mocker.patch(
            "shared.pole_remote_control.get_token",
            return_value={"access_token": _make_jwt(3600), "refresh_token": _make_jwt(604800)},
        )
        m._token_cache["SLDEMOS"] = {
            "access_token": "EXPIRED_AT",
            "refresh_token": "ALSO_EXPIRED_RT",
            "access_exp": time_module.time() - 10,
            "refresh_exp": time_module.time() - 5,
        }

        m._get_access_token("SLDEMOS", "hunter2")

        mock_get_token.assert_called_once_with("SLDEMOS", "hunter2")

    def test_refresh_failure_falls_back_to_get_token(self, mocker):
        mocker.patch(
            "shared.pole_remote_control.refresh_token", side_effect=Exception("refresh rejected")
        )
        mock_get_token = mocker.patch(
            "shared.pole_remote_control.get_token",
            return_value={"access_token": _make_jwt(3600), "refresh_token": _make_jwt(604800)},
        )
        m._token_cache["SLDEMOS"] = {
            "access_token": "EXPIRED_AT",
            "refresh_token": "RT_THAT_WILL_FAIL",
            "access_exp": time_module.time() - 10,
            "refresh_exp": time_module.time() + 604800,
        }

        token = m._get_access_token("SLDEMOS", "hunter2")

        mock_get_token.assert_called_once_with("SLDEMOS", "hunter2")
        assert token is not None

    def test_new_token_is_cached_for_next_call(self, mocker):
        mocker.patch(
            "shared.pole_remote_control.get_token",
            return_value={"access_token": _make_jwt(3600), "refresh_token": _make_jwt(604800)},
        )

        m._get_access_token("SLDEMOS", "hunter2")

        assert "SLDEMOS" in m._token_cache


class TestRowsToGroupsAndUsername:
    def test_single_row_produces_one_group_one_controller_code(self):
        rows = [("SLDEMOS", 1458, "GT", "CC-1")]

        user_name, groups = m._rows_to_groups_and_username(rows)

        assert user_name == "SLDEMOS"
        assert groups == [{"gateway_code": "GT", "group_id": 1458, "controller_codes": ["CC-1"]}]

    def test_multiple_rows_same_group_collect_into_one_entry(self):
        rows = [
            ("SLDEMOS", 1458, "GT", "CC-1"),
            ("SLDEMOS", 1458, "GT", "CC-2"),
        ]

        user_name, groups = m._rows_to_groups_and_username(rows)

        assert len(groups) == 1
        assert groups[0]["controller_codes"] == ["CC-1", "CC-2"]

    def test_multiple_rows_different_groups_produce_separate_entries(self):
        rows = [
            ("SLDEMOS", 100, "GT-A", "CC-1"),
            ("SLDEMOS", 200, "GT-B", "CC-2"),
        ]

        user_name, groups = m._rows_to_groups_and_username(rows)

        assert len(groups) == 2
        by_group_id = {g["group_id"]: g for g in groups}
        assert by_group_id[100]["gateway_code"] == "GT-A"
        assert by_group_id[100]["controller_codes"] == ["CC-1"]
        assert by_group_id[200]["gateway_code"] == "GT-B"
        assert by_group_id[200]["controller_codes"] == ["CC-2"]

    def test_username_taken_from_first_row(self):
        rows = [("SLDEMOS", 1458, "GT", "CC-1"), ("SLDEMOS", 1458, "GT", "CC-2")]

        user_name, _ = m._rows_to_groups_and_username(rows)

        assert user_name == "SLDEMOS"


class TestFetchTelemetryForGateway:
    def test_no_matching_rows_raises_gateway_not_found(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        with pytest.raises(m.GatewayNotFoundError, match="GT-999"):
            m._fetch_telemetry_for_gateway("GT-999")

    def test_returns_all_matching_rows(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [
            ("SLDEMOS", 1458, "GT", "CC-1"),
            ("SLDEMOS", 1458, "GT", "CC-2"),
        ]

        rows = m._fetch_telemetry_for_gateway("GT")

        assert len(rows) == 2

    def test_query_is_bounded_by_lookback_cutoff(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchall.return_value = [("SLDEMOS", 1458, "GT", "CC-1")]

        m._fetch_telemetry_for_gateway("GT")

        sql, gateway_code, cutoff = mock_cursor.execute.call_args.args
        assert "LastUpload >= ?" in sql
        assert gateway_code == "GT"
        assert cutoff is not None


class TestResolveLeadsunProjectId:
    def test_project_not_found_raises(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(m.ProjectNotFoundError, match="recProj999"):
            m._resolve_leadsun_project_id("recProj999")

    def test_project_with_no_leadsun_id_raises(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [(1,), None]

        with pytest.raises(m.ProjectHasNoLeadsunIdError, match="recProj1"):
            m._resolve_leadsun_project_id("recProj1")

    def test_resolves_leadsun_project_id(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [(1,), ("482",)]

        result = m._resolve_leadsun_project_id("recProj1")

        assert result == "482"


class TestFetchTelemetryForProject:
    def test_no_matching_telemetry_raises_project_telemetry_not_found(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [(1,), ("482",)]
        mock_cursor.fetchall.return_value = []

        with pytest.raises(m.ProjectTelemetryNotFoundError, match="recProj1"):
            m._fetch_telemetry_for_project("recProj1")

    def test_returns_all_matching_rows_across_project(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [(1,), ("482",)]
        mock_cursor.fetchall.return_value = [
            ("SLDEMOS", 100, "GT-A", "CC-1"),
            ("SLDEMOS", 200, "GT-B", "CC-2"),
        ]

        rows = m._fetch_telemetry_for_project("recProj1")

        assert len(rows) == 2

    def test_project_not_found_propagates(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(m.ProjectNotFoundError):
            m._fetch_telemetry_for_project("nonexistent")


class TestSetPoleLights:
    def setup_method(self):
        m._token_cache.clear()

    def _mock_common(self, mocker, password="hunter2"):
        mocker.patch("shared.pole_remote_control.decrypt_secret", return_value=password)
        mocker.patch(
            "shared.pole_remote_control.get_token",
            return_value={"access_token": _make_jwt(3600), "refresh_token": _make_jwt(604800)},
        )
        return mocker.patch(
            "shared.pole_remote_control.send_remote_command",
            return_value={"success": True, "data": None},
        )

    def test_no_scope_argument_raises_value_error(self):
        with pytest.raises(ValueError, match="Exactly one"):
            m.set_pole_lights(brightness=50, time_minutes=30)

    def test_multiple_scope_arguments_raises_value_error(self):
        with pytest.raises(ValueError, match="Exactly one"):
            m.set_pole_lights(
                brightness=50, time_minutes=30, pole_number="P1", gateway_code="GT"
            )

    def test_pole_scope_happy_path(
        self, patch_get_connection_pole_remote_control, mock_cursor, mocker
    ):
        mock_cursor.fetchone.side_effect = [
            (1,),  # pole exists
            ("SLDEMOS", 1458, "GT18L94A25082883", "A3P70LA323110598"),  # telemetry
            ("encrypted-blob",),  # LeadsunEdgeAccounts row
        ]
        mock_send = self._mock_common(mocker)

        result = m.set_pole_lights(pole_number="12009-1000-A", brightness=50, time_minutes=30)

        assert result == {"success": True, "data": None}
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["brightness"] == 50
        assert call_kwargs["time_minutes"] == 30
        assert call_kwargs["groups"] == [
            {
                "gateway_code": "GT18L94A25082883",
                "group_id": 1458,
                "controller_codes": ["A3P70LA323110598"],
            }
        ]

    def test_pole_not_found_propagates(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(m.PoleNotFoundError):
            m.set_pole_lights(pole_number="nonexistent", brightness=50, time_minutes=30)

    def test_gateway_scope_happy_path(
        self, patch_get_connection_pole_remote_control, mock_cursor, mocker
    ):
        mock_cursor.fetchall.return_value = [
            ("SLDEMOS", 1458, "GT", "CC-1"),
            ("SLDEMOS", 1458, "GT", "CC-2"),
        ]
        mock_cursor.fetchone.return_value = ("encrypted-blob",)  # LeadsunEdgeAccounts row
        mock_send = self._mock_common(mocker)

        result = m.set_pole_lights(gateway_code="GT", brightness=100, time_minutes=0)

        assert result == {"success": True, "data": None}
        call_kwargs = mock_send.call_args.kwargs
        assert call_kwargs["groups"] == [
            {"gateway_code": "GT", "group_id": 1458, "controller_codes": ["CC-1", "CC-2"]}
        ]

    def test_gateway_not_found_propagates(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []

        with pytest.raises(m.GatewayNotFoundError):
            m.set_pole_lights(gateway_code="GT-999", brightness=50, time_minutes=30)

    def test_project_scope_happy_path(
        self, patch_get_connection_pole_remote_control, mock_cursor, mocker
    ):
        mock_cursor.fetchone.side_effect = [
            (1,),  # project exists
            ("482",),  # resolved LeadsunProjectId
            ("encrypted-blob",),  # LeadsunEdgeAccounts row
        ]
        mock_cursor.fetchall.return_value = [
            ("SLDEMOS", 100, "GT-A", "CC-1"),
            ("SLDEMOS", 200, "GT-B", "CC-2"),
        ]
        mock_send = self._mock_common(mocker)

        result = m.set_pole_lights(project_id="recProj1", brightness=0, time_minutes=0)

        assert result == {"success": True, "data": None}
        call_kwargs = mock_send.call_args.kwargs
        assert len(call_kwargs["groups"]) == 2

    def test_project_not_found_propagates(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None

        with pytest.raises(m.ProjectNotFoundError):
            m.set_pole_lights(project_id="nonexistent", brightness=50, time_minutes=30)

    def test_project_with_no_leadsun_id_propagates(
        self, patch_get_connection_pole_remote_control, mock_cursor
    ):
        mock_cursor.fetchone.side_effect = [(1,), None]

        with pytest.raises(m.ProjectHasNoLeadsunIdError):
            m.set_pole_lights(project_id="recProj1", brightness=50, time_minutes=30)

    def test_brightness_and_time_are_passed_through_unmodified(
        self, patch_get_connection_pole_remote_control, mock_cursor, mocker
    ):
        """No fixed on/off mapping -- whatever the caller passes goes
        straight to Leadsun, by explicit design."""
        mock_cursor.fetchone.side_effect = [
            (1,),
            ("SLDEMOS", 1458, "GT", "CC"),
            ("encrypted-blob",),
        ]
        mock_send = self._mock_common(mocker)

        m.set_pole_lights(pole_number="P1", brightness=0, time_minutes=0)

        assert mock_send.call_args.kwargs["brightness"] == 0
        assert mock_send.call_args.kwargs["time_minutes"] == 0

    def test_only_one_send_remote_command_call_even_for_project_scope(
        self, patch_get_connection_pole_remote_control, mock_cursor, mocker
    ):
        """Regression guard: gateway/project scope must still be ONE API
        call, not one per pole -- Leadsun's own payload already supports
        multiple groups/products per request."""
        mock_cursor.fetchone.side_effect = [(1,), ("482",), ("encrypted-blob",)]
        mock_cursor.fetchall.return_value = [
            ("SLDEMOS", 100, "GT-A", "CC-1"),
            ("SLDEMOS", 100, "GT-A", "CC-2"),
            ("SLDEMOS", 200, "GT-B", "CC-3"),
        ]
        mock_send = self._mock_common(mocker)

        m.set_pole_lights(project_id="recProj1", brightness=50, time_minutes=30)

        mock_send.assert_called_once()
