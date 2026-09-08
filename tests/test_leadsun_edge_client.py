"""Tests for shared/leadsun_edge_client.py"""

import pytest
import requests

from shared import leadsun_edge_client as m


def _make_response(mocker, json_body, status_code=200, raise_for_status_error=None):
    response = mocker.MagicMock()
    response.json.return_value = json_body
    response.status_code = status_code
    if raise_for_status_error:
        response.raise_for_status.side_effect = raise_for_status_error
    else:
        response.raise_for_status.return_value = None
    return response


class TestGetToken:
    def test_sends_correct_form_fields_and_url(self, mocker):
        response = _make_response(
            mocker,
            {"success": True, "access_token": "AT", "refresh_token": "RT"},
        )
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        result = m.get_token("SLDEMOS", "hunter2")

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args.args[0] == f"{m.LEADSUN_EDGE_BASE_URL}/get-token"
        assert call_args.kwargs["data"] == {"userName": "SLDEMOS", "pswd": "hunter2"}
        assert result["access_token"] == "AT"
        assert result["refresh_token"] == "RT"

    def test_raises_on_success_false(self, mocker):
        response = _make_response(
            mocker, {"success": False, "message": "Invalid credentials"}
        )
        mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        with pytest.raises(m.LeadsunEdgeApiError, match="Invalid credentials"):
            m.get_token("SLDEMOS", "wrongpassword")

    def test_raises_leadsun_error_with_body_text_on_http_error(self, mocker):
        response = _make_response(
            mocker,
            {},
            status_code=500,
            raise_for_status_error=requests.HTTPError("500 error"),
        )
        response.text = "Internal Server Error: something broke"
        mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        with pytest.raises(m.LeadsunEdgeApiError, match="Internal Server Error"):
            m.get_token("SLDEMOS", "hunter2")


class TestRefreshToken:
    def test_sends_correct_form_field_and_url(self, mocker):
        response = _make_response(
            mocker,
            {"success": True, "access_token": "NEW_AT", "refresh_token": "NEW_RT"},
        )
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        result = m.refresh_token("OLD_RT")

        call_args = mock_post.call_args
        assert call_args.args[0] == f"{m.LEADSUN_EDGE_BASE_URL}/refresh-token"
        assert call_args.kwargs["data"] == {"RefToken": "OLD_RT"}
        assert result["access_token"] == "NEW_AT"

    def test_raises_on_success_false(self, mocker):
        response = _make_response(mocker, {"success": False, "message": "Token expired"})
        mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        with pytest.raises(m.LeadsunEdgeApiError, match="Token expired"):
            m.refresh_token("EXPIRED_RT")


class TestSendRemoteCommand:
    def test_sends_correct_url_and_auth_header(self, mocker):
        response = _make_response(mocker, {"success": True, "data": None})
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        m.send_remote_command(
            access_token="AT123",
            brightness=50,
            time_minutes=30,
            groups=[
                {
                    "gateway_code": "GT18L94A25082883",
                    "group_id": 1458,
                    "controller_codes": ["A3P70LA323110598"],
                }
            ],
        )

        call_args = mock_post.call_args
        assert call_args.args[0] == f"{m.LEADSUN_EDGE_BASE_URL}/dominate/remote-command"
        assert call_args.kwargs["headers"]["Authorization"] == "AT123"
        assert call_args.kwargs["headers"]["Content-Type"] == "application/json"

    def test_field_mapping_is_cross_wired_exactly_as_confirmed(self, mocker):
        """Regression guard for the confirmed (not a bug) mismatch:
        body["groups"][0]["controllerCode"] gets OUR gateway_code, and
        body["groups"][0]["products"][0]["productId"] gets OUR
        controller_code -- not the field named the same as our own."""
        response = _make_response(mocker, {"success": True, "data": None})
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        m.send_remote_command(
            access_token="AT123",
            brightness=50,
            time_minutes=30,
            groups=[
                {
                    "gateway_code": "GATEWAY-XYZ",
                    "group_id": 1458,
                    "controller_codes": ["CONTROLLER-ABC"],
                }
            ],
        )

        payload = mock_post.call_args.kwargs["json"]
        assert payload["brightness"] == 50
        assert payload["time"] == 30
        group = payload["groups"][0]
        assert group["controllerCode"] == "GATEWAY-XYZ"
        assert group["groupId"] == "1458"  # stringified, matching the curl example
        assert group["products"][0]["productId"] == "CONTROLLER-ABC"

    def test_group_id_is_stringified_even_when_given_as_int(self, mocker):
        response = _make_response(mocker, {"success": True, "data": None})
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        m.send_remote_command(
            access_token="AT",
            brightness=0,
            time_minutes=0,
            groups=[{"gateway_code": "GT", "group_id": 1458, "controller_codes": ["CC"]}],
        )

        payload = mock_post.call_args.kwargs["json"]
        assert payload["groups"][0]["groupId"] == "1458"
        assert isinstance(payload["groups"][0]["groupId"], str)

    def test_multiple_controller_codes_in_one_group(self, mocker):
        """Gateway-wide scope: one group, many poles."""
        response = _make_response(mocker, {"success": True, "data": None})
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        m.send_remote_command(
            access_token="AT",
            brightness=100,
            time_minutes=0,
            groups=[
                {
                    "gateway_code": "GT",
                    "group_id": 1458,
                    "controller_codes": ["CC-1", "CC-2", "CC-3"],
                }
            ],
        )

        products = mock_post.call_args.kwargs["json"]["groups"][0]["products"]
        assert [p["productId"] for p in products] == ["CC-1", "CC-2", "CC-3"]

    def test_multiple_groups_in_one_request(self, mocker):
        """Project-wide scope: many groups, each potentially with many
        poles -- still ONE remote-command call."""
        response = _make_response(mocker, {"success": True, "data": None})
        mock_post = mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        m.send_remote_command(
            access_token="AT",
            brightness=100,
            time_minutes=0,
            groups=[
                {"gateway_code": "GT-A", "group_id": 100, "controller_codes": ["CC-1"]},
                {"gateway_code": "GT-B", "group_id": 200, "controller_codes": ["CC-2", "CC-3"]},
            ],
        )

        payload_groups = mock_post.call_args.kwargs["json"]["groups"]
        assert len(payload_groups) == 2
        assert payload_groups[0]["controllerCode"] == "GT-A"
        assert payload_groups[0]["groupId"] == "100"
        assert [p["productId"] for p in payload_groups[0]["products"]] == ["CC-1"]
        assert payload_groups[1]["controllerCode"] == "GT-B"
        assert [p["productId"] for p in payload_groups[1]["products"]] == ["CC-2", "CC-3"]

    def test_raises_leadsun_error_with_body_text_on_http_error(self, mocker):
        """Regression guard for a real production 401 -- raise_for_status()
        alone gives no clue WHY the server rejected the request; the
        response body text is the only place that explanation lives."""
        response = _make_response(
            mocker,
            {},
            status_code=401,
            raise_for_status_error=requests.HTTPError("401 error"),
        )
        response.text = "Unauthorized: invalid token"
        mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        with pytest.raises(m.LeadsunEdgeApiError, match="Unauthorized: invalid token"):
            m.send_remote_command(
                access_token="BAD_TOKEN",
                brightness=50,
                time_minutes=30,
                groups=[{"gateway_code": "GT", "group_id": 1, "controller_codes": ["CC"]}],
            )

    def test_raises_on_success_false(self, mocker):
        response = _make_response(
            mocker, {"success": False, "message": "Controller offline"}
        )
        mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        with pytest.raises(m.LeadsunEdgeApiError, match="Controller offline"):
            m.send_remote_command(
                access_token="AT",
                brightness=0,
                time_minutes=0,
                groups=[{"gateway_code": "GT", "group_id": 1, "controller_codes": ["CC"]}],
            )

    def test_returns_full_response_body_on_success(self, mocker):
        response = _make_response(
            mocker,
            {"success": True, "message": "Request successful", "statusCode": "200", "data": None},
        )
        mocker.patch("shared.leadsun_edge_client.requests.post", return_value=response)

        result = m.send_remote_command(
            access_token="AT",
            brightness=100,
            time_minutes=0,
            groups=[{"gateway_code": "GT", "group_id": 1, "controller_codes": ["CC"]}],
        )

        assert result == {
            "success": True,
            "message": "Request successful",
            "statusCode": "200",
            "data": None,
        }
