"""
Endpoint tests for populateLeadsunProject: who may call it, how the body is
read, and how each failure maps to an HTTP status. (What the function itself
does is covered in test_leadsun_project_populator.py.)
"""

import json

import azure.functions as func
import pytest

import function_app
from shared import leadsun_project_populator as pop
from shared.auth_utils import AuthContext
from shared.data_scope import UNRESTRICTED, DataScope
from shared.leadsun_edge_client import LeadsunEdgeApiError

RESULT = {
    "projectId": "recP",
    "projectName": "Pioneer Ranch",
    "leadsunProject": {"ProjectId": "389"},
}
GOOD_BODY = {"ProjectId": "recP", "ProjectName": "Pioneer Ranch", "remark": "phase 2"}


def _ctx(role="Streetleaf Admin", customer_id=None):
    return AuthContext("user-1", role, customer_id, "sess-1")


def _request(body=None, raw=None):
    payload = raw if raw is not None else json.dumps(body).encode()
    return func.HttpRequest(method="POST", url="/api/populateLeadsunProject", params={}, headers={}, body=payload)


@pytest.fixture
def populate(mocker):
    """Signed in as Streetleaf Admin; the function itself mocked."""
    mocker.patch("function_app.require_auth", return_value=_ctx())
    return mocker.patch("function_app.populate_leadsun_project", return_value=RESULT)


def _json(response):
    return json.loads(response.get_body())


class TestAccess:
    def test_a_token_is_required(self, mocker):
        mock_populate = mocker.patch("function_app.populate_leadsun_project")
        response = function_app.populateLeadsunProject(_request(GOOD_BODY))
        assert response.status_code == 401
        mock_populate.assert_not_called()

    @pytest.mark.parametrize("role", ["Customer Owner", "Customer Admin", "User"])
    def test_customer_roles_may_not_call_it(self, mocker, role):
        mocker.patch("function_app.require_auth", return_value=_ctx(role, "c1"))
        mock_populate = mocker.patch("function_app.populate_leadsun_project")
        response = function_app.populateLeadsunProject(_request(GOOD_BODY))
        assert response.status_code == 403
        mock_populate.assert_not_called()

    def test_a_streetleaf_admin_may_and_is_unrestricted(self, populate):
        assert function_app.populateLeadsunProject(_request(GOOD_BODY)).status_code == 201
        assert populate.call_args.kwargs["scope"] == UNRESTRICTED

    def test_a_streetleaf_crew_member_may_but_only_within_their_scope(self, mocker):
        mocker.patch("function_app.require_auth", return_value=_ctx("Streetleaf Crew"))
        mock_populate = mocker.patch("function_app.populate_leadsun_project", return_value=RESULT)

        response = function_app.populateLeadsunProject(_request(GOOD_BODY))

        assert response.status_code == 201
        # held to scheduled projects only, same as on the read endpoints
        assert mock_populate.call_args.kwargs["scope"] == DataScope(scheduled_projects_only=True)

    def test_an_unknown_role_is_refused(self, mocker):
        mocker.patch("function_app.require_auth", return_value=_ctx("Intern"))
        mock_populate = mocker.patch("function_app.populate_leadsun_project")
        assert function_app.populateLeadsunProject(_request(GOOD_BODY)).status_code == 403
        mock_populate.assert_not_called()


class TestBody:
    def test_the_contract_keys_are_passed_through(self, populate):
        function_app.populateLeadsunProject(_request(GOOD_BODY))
        populate.assert_called_once_with(
            project_id="recP", project_name="Pioneer Ranch", remark="phase 2", scope=UNRESTRICTED
        )

    def test_remark_is_optional(self, populate):
        function_app.populateLeadsunProject(_request({"ProjectId": "recP", "ProjectName": "X"}))
        assert populate.call_args.kwargs["remark"] is None

    def test_success_is_a_201_with_the_result(self, populate):
        response = function_app.populateLeadsunProject(_request(GOOD_BODY))
        assert response.status_code == 201
        assert response.mimetype == "application/json"
        assert _json(response) == RESULT

    def test_the_success_body_has_no_duplicate_leadsunProjectId(self, populate):
        body = _json(function_app.populateLeadsunProject(_request(GOOD_BODY)))
        assert "leadsunProjectId" not in body
        assert body["leadsunProject"]["ProjectId"] == "389"

    @pytest.mark.parametrize("raw", [b"", b"not json", b"{broken"])
    def test_an_unparseable_body_is_a_400(self, populate, raw):
        response = function_app.populateLeadsunProject(_request(raw=raw))
        assert response.status_code == 400
        assert _json(response) == {"error": "request body must be valid JSON"}
        populate.assert_not_called()

    @pytest.mark.parametrize("body", [[], [1, 2], "text", 5, None])
    def test_a_body_that_is_not_an_object_is_a_400(self, populate, body):
        response = function_app.populateLeadsunProject(_request(body))
        assert response.status_code == 400
        assert _json(response) == {"error": "request body must be a JSON object"}
        populate.assert_not_called()

    def test_the_key_names_are_exactly_ProjectId_ProjectName_and_remark(self, mocker):
        """camelCase spellings are NOT silently accepted -- they read as missing."""
        mocker.patch("function_app.require_auth", return_value=_ctx())
        # real populate function: validation fails before anything external is touched
        response = function_app.populateLeadsunProject(_request({"projectId": "recP", "projectName": "X"}))
        assert response.status_code == 400
        assert _json(response) == {"error": "ProjectId is required"}

    def test_a_missing_project_name_is_a_400(self, mocker):
        mocker.patch("function_app.require_auth", return_value=_ctx())
        response = function_app.populateLeadsunProject(_request({"ProjectId": "recP"}))
        assert response.status_code == 400
        assert _json(response) == {"error": "ProjectName is required"}


class TestErrorMapping:
    @pytest.mark.parametrize(
        "error,status",
        [
            (pop.ProjectNotFoundError("no such project"), 404),
            (pop.PopulateConflictError("not blank"), 409),
            (pop.LeadsunProjectLookupError("never appeared"), 502),
            (pop.AirtableError("airtable down"), 502),
            (pop.DatabaseUpdateError("db down"), 500),
            (pop.LeadsunCredentialsNotConfiguredError("no creds"), 500),
            (LeadsunEdgeApiError("Invalid credentials"), 502),
            (ValueError("ProjectId is required"), 400),
        ],
        ids=lambda e: type(e).__name__ if not isinstance(e, int) else str(e),
    )
    def test_each_failure_has_its_status_and_message(self, populate, error, status):
        populate.side_effect = error
        response = function_app.populateLeadsunProject(_request(GOOD_BODY))
        assert response.status_code == status
        assert _json(response) == {"error": str(error)}

    def test_a_failure_after_the_leadsun_project_exists_returns_its_id(self, populate):
        populate.side_effect = pop.AirtableError("PATCH failed; set it by hand", leadsun_project_id="389")
        response = function_app.populateLeadsunProject(_request(GOOD_BODY))
        assert response.status_code == 502
        assert _json(response) == {"error": "PATCH failed; set it by hand", "leadsunProjectId": "389"}

    def test_a_failure_before_anything_exists_has_no_id(self, populate):
        populate.side_effect = pop.PopulateConflictError("not blank")
        assert "leadsunProjectId" not in _json(function_app.populateLeadsunProject(_request(GOOD_BODY)))

    def test_an_unexpected_error_is_a_generic_500_that_leaks_nothing(self, populate):
        populate.side_effect = RuntimeError("password=hunter2 in connection string")
        response = function_app.populateLeadsunProject(_request(GOOD_BODY))
        assert response.status_code == 500
        assert _json(response) == {"error": "internal error"}
