"""
Endpoint-level tests: every protected endpoint requires a token, and every
read endpoint hands its query function the scope that matches the caller's
role. (How each scope turns into SQL is covered in test_data_scope.py.)
"""

import json

import azure.functions as func
import pytest

import function_app
from shared.auth_utils import AuthContext
from shared.data_scope import UNRESTRICTED, DataScope

# (HTTP function, the query function it calls, minimal query params)
READ_ENDPOINTS = [
    ("getCustomers", "get_customers", {}),
    ("getProjects", "get_projects", {}),
    ("getPoles", "get_poles", {}),
    ("getPoleVitals", "get_pole_vitals", {}),
    ("getUsers", "get_users", {}),
    ("getPoleVitalsByPeriod", "get_pole_vitals_by_period", {"poleId": "pole1", "periodType": "Hour"}),
]


def _ctx(role, customer_id=None):
    return AuthContext("user-1", role, customer_id, "sess-1")


def _get(name, params):
    return func.HttpRequest(method="GET", url=f"/api/{name}", params=params, headers={}, body=b"")


def _post(name, body):
    return func.HttpRequest(
        method="POST", url=f"/api/{name}", params={}, headers={}, body=json.dumps(body).encode()
    )


class TestEachRoleGetsItsScope:
    CASES = [
        (_ctx("Streetleaf Admin"), UNRESTRICTED),
        (_ctx("Customer Owner", "cust-1"), DataScope(customer_id="cust-1")),
        (_ctx("Customer Admin", "cust-1"), DataScope(customer_id="cust-1")),
        (_ctx("User", "cust-1"), DataScope(customer_id="cust-1")),
        (_ctx("Streetleaf Crew"), DataScope(scheduled_projects_only=True)),
    ]

    @pytest.mark.parametrize("name,callee,params", READ_ENDPOINTS)
    @pytest.mark.parametrize("ctx,expected", CASES, ids=[c[0].role for c in CASES])
    def test_scope_passed_to_the_query(self, mocker, name, callee, params, ctx, expected):
        mocker.patch("function_app.require_auth", return_value=ctx)
        mock_query = mocker.patch(f"function_app.{callee}", return_value=[])

        getattr(function_app, name)(_get(name, params))

        mock_query.assert_called_once()
        assert mock_query.call_args.kwargs["scope"] == expected

    @pytest.mark.parametrize("name,callee,params", READ_ENDPOINTS)
    def test_a_caller_cannot_widen_their_scope_through_query_params(self, mocker, name, callee, params):
        """customerId in the URL is just another filter ANDed with the scope;
        it never replaces it."""
        mocker.patch("function_app.require_auth", return_value=_ctx("Customer Admin", "cust-1"))
        mock_query = mocker.patch(f"function_app.{callee}", return_value=[])

        getattr(function_app, name)(_get(name, {**params, "customerId": "cust-OTHER"}))

        assert mock_query.call_args.kwargs["scope"] == DataScope(customer_id="cust-1")


class TestTokenRequired:
    """No Authorization header at all -> 401, and the data layer is never reached."""

    @pytest.mark.parametrize("name,callee,params", READ_ENDPOINTS)
    def test_read_endpoints(self, mocker, name, callee, params):
        mock_query = mocker.patch(f"function_app.{callee}")

        response = getattr(function_app, name)(_get(name, params))

        assert response.status_code == 401
        mock_query.assert_not_called()

    def test_set_pole_lights(self, mocker):
        mock_set = mocker.patch("function_app.set_pole_lights")
        response = function_app.setPoleLights(
            _post("setPoleLights", {"poleNumber": "P-1", "brightness": 50, "time": 1})
        )
        assert response.status_code == 401
        mock_set.assert_not_called()

    def test_create_pole_issue(self, mocker):
        mock_create = mocker.patch("function_app.create_pole_issue")
        response = function_app.createPoleIssue(
            _post("createPoleIssue", {"poleNumber": "P-1", "status": "Electrical Issue", "problemDetails": "x"})
        )
        assert response.status_code == 401
        mock_create.assert_not_called()


class TestUnusableAccountsFailClosed:
    @pytest.mark.parametrize("name,callee,params", READ_ENDPOINTS)
    @pytest.mark.parametrize(
        "ctx",
        [_ctx("Customer Admin", None), _ctx("User", None), _ctx("Mystery Role", "cust-1")],
        ids=["customer-admin-with-no-customer", "user-with-no-customer", "unknown-role"],
    )
    def test_refused_with_403_before_any_query(self, mocker, name, callee, params, ctx):
        mocker.patch("function_app.require_auth", return_value=ctx)
        mock_query = mocker.patch(f"function_app.{callee}")

        response = getattr(function_app, name)(_get(name, params))

        assert response.status_code == 403
        mock_query.assert_not_called()
