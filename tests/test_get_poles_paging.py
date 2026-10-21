"""
Endpoint tests for getPoles pagination: /getPoles?page=1&pageSize=10&...&faults=1

The plain getPoles (no page/pageSize) must keep returning exactly what it
always has -- a bare array, or a single object for poleId -- since existing
consumers (e.g. the all-poles map via summary=true) depend on it.
"""

import json

import azure.functions as func
import pytest

import function_app
from shared.auth_utils import AuthContext
from shared.data_scope import UNRESTRICTED, DataScope

PAGE = {"total": 13950, "page": 1, "pageSize": 10, "items": [{"id": "pole1"}]}


def _ctx(role="Streetleaf Admin", customer_id=None):
    return AuthContext("user-1", role, customer_id, "sess-1")


def _request(**params):
    return func.HttpRequest(
        method="GET", url="/api/getPoles", params={k: str(v) for k, v in params.items()}, headers={}, body=b""
    )


@pytest.fixture
def paged(mocker):
    """Signed in as Streetleaf Admin; both query functions mocked."""
    mocker.patch("function_app.require_auth", return_value=_ctx())
    page_mock = mocker.patch("function_app.get_poles_page", return_value=PAGE)
    plain_mock = mocker.patch("function_app.get_poles", return_value=[{"id": "pole1"}])
    return page_mock, plain_mock


class TestPaginatedResponse:
    def test_all_params_flow_through_and_the_page_is_returned_as_is(self, paged):
        page_mock, plain_mock = paged

        response = function_app.getPoles(
            _request(page=2, pageSize=25, customerId="c1", projectId="p1", faults=1, summary="true", active="true")
        )

        assert response.status_code == 200
        assert response.mimetype == "application/json"
        assert json.loads(response.get_body()) == PAGE
        page_mock.assert_called_once_with(
            page=2, page_size=25, project_id="p1", customer_id="c1", pole_number=None,
            summary=True, active=True, faults=True, scope=UNRESTRICTED,
        )
        plain_mock.assert_not_called()

    def test_the_example_url_from_the_request(self, paged):
        page_mock, _ = paged
        function_app.getPoles(_request(page=1, pageSize=10, customerId="recX", projectId="recY", faults=1))
        kwargs = page_mock.call_args.kwargs
        assert (kwargs["page"], kwargs["page_size"], kwargs["faults"]) == (1, 10, True)
        assert (kwargs["customer_id"], kwargs["project_id"]) == ("recX", "recY")

    def test_page_alone_leaves_page_size_to_the_default(self, paged):
        page_mock, _ = paged
        function_app.getPoles(_request(page=3))
        assert page_mock.call_args.kwargs["page"] == 3
        assert page_mock.call_args.kwargs["page_size"] is None

    def test_page_size_alone_starts_at_page_one(self, paged):
        page_mock, _ = paged
        function_app.getPoles(_request(pageSize=20))
        assert page_mock.call_args.kwargs["page"] == 1
        assert page_mock.call_args.kwargs["page_size"] == 20

    def test_limit_is_ignored_when_paginating(self, paged):
        page_mock, _ = paged
        function_app.getPoles(_request(page=1, pageSize=10, limit=500))
        assert "limit" not in page_mock.call_args.kwargs

    def test_query_failure_is_a_500_with_a_json_error(self, mocker, paged):
        page_mock, _ = paged
        page_mock.side_effect = RuntimeError("db down")
        response = function_app.getPoles(_request(page=1, pageSize=10))
        assert response.status_code == 500
        assert json.loads(response.get_body()) == {"error": "internal error"}


class TestPlainGetPolesIsUnchanged:
    def test_no_page_params_uses_the_original_query_and_array_response(self, paged):
        page_mock, plain_mock = paged
        response = function_app.getPoles(_request(customerId="c1"))
        assert response.status_code == 200
        assert json.loads(response.get_body()) == [{"id": "pole1"}]
        plain_mock.assert_called_once()
        page_mock.assert_not_called()

    def test_summary_all_poles_map_request_is_not_paginated(self, paged):
        page_mock, plain_mock = paged
        function_app.getPoles(_request(summary="true"))
        plain_mock.assert_called_once()
        page_mock.assert_not_called()

    def test_pole_id_is_always_a_single_object_lookup_even_with_page_params(self, paged):
        page_mock, plain_mock = paged
        plain_mock.return_value = {"id": "pole1"}
        response = function_app.getPoles(_request(poleId="pole1", page=1, pageSize=10))
        assert json.loads(response.get_body()) == {"id": "pole1"}
        plain_mock.assert_called_once()
        page_mock.assert_not_called()

    def test_faults_false_without_paging_is_just_ignored(self, paged):
        _, plain_mock = paged
        response = function_app.getPoles(_request(faults=0))
        assert response.status_code == 200
        plain_mock.assert_called_once()


class TestValidation:
    @pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5", "", " ", "1e3"])
    def test_page_must_be_a_positive_integer(self, paged, bad):
        page_mock, plain_mock = paged
        response = function_app.getPoles(_request(page=bad, pageSize=10))
        assert response.status_code == 400
        assert "page must be a positive integer" in json.loads(response.get_body())["error"]
        page_mock.assert_not_called()
        plain_mock.assert_not_called()

    @pytest.mark.parametrize("bad", ["0", "-5", "ten", "2.5", ""])
    def test_page_size_must_be_a_positive_integer(self, paged, bad):
        page_mock, plain_mock = paged
        response = function_app.getPoles(_request(page=1, pageSize=bad))
        assert response.status_code == 400
        assert "pageSize must be a positive integer" in json.loads(response.get_body())["error"]
        page_mock.assert_not_called()
        plain_mock.assert_not_called()

    @pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("TRUE", True), ("0", False), ("false", False)])
    def test_faults_accepts_the_usual_boolean_forms(self, paged, raw, expected):
        page_mock, _ = paged
        function_app.getPoles(_request(page=1, pageSize=10, faults=raw))
        assert page_mock.call_args.kwargs["faults"] is expected

    def test_faults_rejects_garbage(self, paged):
        page_mock, plain_mock = paged
        response = function_app.getPoles(_request(page=1, pageSize=10, faults="banana"))
        assert response.status_code == 400
        assert "faults must be true/false or 1/0" in json.loads(response.get_body())["error"]
        page_mock.assert_not_called()

    def test_faults_without_paging_is_rejected_rather_than_silently_ignored(self, paged):
        """Otherwise ?faults=1 would quietly return EVERY pole."""
        page_mock, plain_mock = paged
        response = function_app.getPoles(_request(customerId="c1", faults=1))
        assert response.status_code == 400
        assert "faults can only be used together with page/pageSize" in json.loads(response.get_body())["error"]
        page_mock.assert_not_called()
        plain_mock.assert_not_called()


class TestAuthAndScope:
    @pytest.mark.parametrize(
        "ctx,expected",
        [
            (_ctx("Streetleaf Admin"), UNRESTRICTED),
            (_ctx("Customer Admin", "cust-1"), DataScope(customer_id="cust-1")),
            (_ctx("User", "cust-1"), DataScope(customer_id="cust-1")),
            (_ctx("Streetleaf Crew"), DataScope(scheduled_projects_only=True)),
        ],
        ids=["admin", "customer-admin", "user", "crew"],
    )
    def test_each_role_gets_its_scope_in_the_paginated_query(self, mocker, ctx, expected):
        mocker.patch("function_app.require_auth", return_value=ctx)
        page_mock = mocker.patch("function_app.get_poles_page", return_value=PAGE)

        function_app.getPoles(_request(page=1, pageSize=10, faults=1))

        assert page_mock.call_args.kwargs["scope"] == expected

    def test_a_customer_cannot_widen_scope_with_a_customer_id_param(self, mocker):
        mocker.patch("function_app.require_auth", return_value=_ctx("Customer Admin", "cust-1"))
        page_mock = mocker.patch("function_app.get_poles_page", return_value=PAGE)
        function_app.getPoles(_request(page=1, pageSize=10, customerId="cust-OTHER"))
        # passed as a filter that is ANDed with the scope -- never replaces it
        assert page_mock.call_args.kwargs["scope"] == DataScope(customer_id="cust-1")
        assert page_mock.call_args.kwargs["customer_id"] == "cust-OTHER"

    def test_no_token_is_a_401_and_no_query_runs(self, mocker):
        page_mock = mocker.patch("function_app.get_poles_page")
        response = function_app.getPoles(_request(page=1, pageSize=10))
        assert response.status_code == 401
        page_mock.assert_not_called()


class TestPoleNumberSearch:
    def test_the_requested_combination_flows_through(self, paged):
        """poleNumber + customerId + projectId, paginated."""
        page_mock, plain_mock = paged

        response = function_app.getPoles(
            _request(page=1, pageSize=10, poleNumber="12009-10", customerId="recC", projectId="recP")
        )

        assert response.status_code == 200
        kwargs = page_mock.call_args.kwargs
        assert (kwargs["pole_number"], kwargs["customer_id"], kwargs["project_id"]) == ("12009-10", "recC", "recP")
        plain_mock.assert_not_called()

    def test_combines_with_faults(self, paged):
        page_mock, _ = paged
        function_app.getPoles(_request(page=1, pageSize=10, poleNumber="10", faults=1))
        assert page_mock.call_args.kwargs["pole_number"] == "10"
        assert page_mock.call_args.kwargs["faults"] is True

    def test_surrounding_whitespace_is_trimmed(self, paged):
        page_mock, _ = paged
        function_app.getPoles(_request(page=1, pageSize=10, poleNumber="  12009-10  "))
        assert page_mock.call_args.kwargs["pole_number"] == "12009-10"

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_search_box_means_no_search_not_no_match(self, paged, blank):
        page_mock, _ = paged
        response = function_app.getPoles(_request(page=1, pageSize=10, poleNumber=blank))
        assert response.status_code == 200
        assert page_mock.call_args.kwargs["pole_number"] is None

    def test_text_that_looks_like_sql_or_wildcards_is_passed_through_unmodified(self, paged):
        """Escaping is the query layer's job (and it binds a parameter); the
        handler must not mangle what the user typed."""
        page_mock, _ = paged
        function_app.getPoles(_request(page=1, pageSize=10, poleNumber="10_5%"))
        assert page_mock.call_args.kwargs["pole_number"] == "10_5%"

    def test_without_paging_it_is_rejected_rather_than_silently_ignored(self, paged):
        """Otherwise ?poleNumber=123 would quietly return EVERY pole."""
        page_mock, plain_mock = paged
        response = function_app.getPoles(_request(poleNumber="123"))
        assert response.status_code == 400
        assert "poleNumber can only be used together with page/pageSize" in json.loads(response.get_body())["error"]
        page_mock.assert_not_called()
        plain_mock.assert_not_called()

    def test_blank_search_without_paging_is_harmless(self, paged):
        _, plain_mock = paged
        response = function_app.getPoles(_request(poleNumber=""))
        assert response.status_code == 200
        plain_mock.assert_called_once()

    def test_pole_id_lookup_ignores_the_search(self, paged):
        page_mock, plain_mock = paged
        plain_mock.return_value = {"id": "pole1"}
        response = function_app.getPoles(_request(poleId="pole1", poleNumber="123"))
        assert response.status_code == 200
        plain_mock.assert_called_once()
        assert "pole_number" not in plain_mock.call_args.kwargs
        page_mock.assert_not_called()

    def test_scope_still_applies_to_a_search(self, mocker):
        mocker.patch("function_app.require_auth", return_value=_ctx("Customer Admin", "cust-1"))
        page_mock = mocker.patch("function_app.get_poles_page", return_value=PAGE)
        function_app.getPoles(_request(page=1, pageSize=10, poleNumber="10"))
        assert page_mock.call_args.kwargs["scope"] == DataScope(customer_id="cust-1")
