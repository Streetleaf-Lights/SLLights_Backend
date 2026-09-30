import pytest
from unittest.mock import MagicMock, patch

from shared import pole_issue_api as m
from shared.auth_utils import AuthError


class TestCreatePoleIssue:
    def _patch_db(self, mocker, pole_id="recABC", linked_pole_id="recLINKED"):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (pole_id, linked_pole_id)
        conn.cursor.return_value = cursor
        mocker.patch("shared.pole_issue_api.get_connection", return_value=conn)
        return cursor

    def test_raises_if_pole_number_missing(self):
        with pytest.raises(ValueError, match="poleNumber is required"):
            m.create_pole_issue("", "Electrical Issue", "Some detail")

    def test_raises_if_problem_details_missing(self):
        with pytest.raises(ValueError, match="problemDetails is required"):
            m.create_pole_issue("POLE-001", "Electrical Issue", "")

    def test_raises_if_status_invalid(self):
        with pytest.raises(ValueError, match="status must be one of"):
            m.create_pole_issue("POLE-001", "Bad Status", "Some detail")

    def test_raises_if_pole_not_found(self, mocker):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        conn.cursor.return_value = cursor
        mocker.patch("shared.pole_issue_api.get_connection", return_value=conn)

        with pytest.raises(ValueError, match="no active pole found"):
            m.create_pole_issue("UNKNOWN", "Electrical Issue", "Some detail")

    def test_raises_if_linked_pole_id_missing(self, mocker):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("recABC", None)
        conn.cursor.return_value = cursor
        mocker.patch("shared.pole_issue_api.get_connection", return_value=conn)

        with pytest.raises(RuntimeError, match="no LinkedPoleId"):
            m.create_pole_issue("POLE-001", "Electrical Issue", "Some detail")

    def test_patches_pole_and_posts_issue(self, mocker):
        self._patch_db(mocker)
        mock_patch = mocker.patch("shared.pole_issue_api._patch_pole_status")
        mock_post = mocker.patch(
            "shared.pole_issue_api._post_pole_issue", return_value="recNEW123"
        )
        mocker.patch("shared.pole_issue_api.load_pole_issues")

        result = m.create_pole_issue("POLE-001", "Electrical Issue", "Lamp flickering")

        mock_patch.assert_called_once_with("recABC", "Electrical Issue")
        mock_post.assert_called_once_with("recLINKED", "Lamp flickering")
        assert result["issueId"] == "recNEW123"
        assert result["poleNumber"] == "POLE-001"
        assert result["status"] == "Electrical Issue"

    def test_syncs_via_load_pole_issues(self, mocker):
        self._patch_db(mocker)
        mocker.patch("shared.pole_issue_api._patch_pole_status")
        mocker.patch("shared.pole_issue_api._post_pole_issue", return_value="recNEW")
        mock_load = mocker.patch("shared.pole_issue_api.load_pole_issues")

        m.create_pole_issue("POLE-001", "Structural Issue", "Leaning")

        mock_load.assert_called_once()

    def test_structural_issue_status_valid(self, mocker):
        self._patch_db(mocker)
        mocker.patch("shared.pole_issue_api._patch_pole_status")
        mocker.patch("shared.pole_issue_api._post_pole_issue", return_value="recNEW")
        mocker.patch("shared.pole_issue_api.load_pole_issues")

        result = m.create_pole_issue("POLE-001", "Structural Issue", "Leaning pole")
        assert result["status"] == "Structural Issue"

    def test_airtable_patch_failure_raises(self, mocker):
        self._patch_db(mocker)
        mocker.patch(
            "shared.pole_issue_api._patch_pole_status",
            side_effect=RuntimeError("Airtable PATCH failed"),
        )
        mocker.patch("shared.pole_issue_api.load_pole_issues")

        with pytest.raises(RuntimeError, match="Airtable PATCH failed"):
            m.create_pole_issue("POLE-001", "Electrical Issue", "detail")

    def test_airtable_post_failure_raises(self, mocker):
        self._patch_db(mocker)
        mocker.patch("shared.pole_issue_api._patch_pole_status")
        mocker.patch(
            "shared.pole_issue_api._post_pole_issue",
            side_effect=RuntimeError("Airtable POST failed"),
        )
        mocker.patch("shared.pole_issue_api.load_pole_issues")

        with pytest.raises(RuntimeError, match="Airtable POST failed"):
            m.create_pole_issue("POLE-001", "Electrical Issue", "detail")
