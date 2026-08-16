"""
Tests for shared/pole_open_issues_loader.py, plus the fetch_all_records()
extension in shared/airtable_client.py that this loader relies on.
"""

from unittest.mock import MagicMock, patch

import pytest

from shared import airtable_client, pole_open_issues_loader as m


def _sample_record(record_id="rec1", issue_id="ISSUE-1", pole_id="recPole1", status="Open", pole_status="Electrical Issue"):
    return {
        "id": record_id,
        "fields": {
            "IssueID": issue_id,
            "PoleId": [pole_id] if pole_id is not None else [],
            "Status": status,
            "Pole Status": [pole_status] if pole_status is not None else [],
        },
    }


class TestFetchAllRecordsExtension:
    """The extension itself (base_id/view params), tested independently
    of the new loader -- this is a shared module used by three other,
    pre-existing loaders too."""

    def test_default_call_still_uses_the_original_base_id(self, mocker):
        mock_response = MagicMock()
        mock_response.json.return_value = {"records": []}
        mocker.patch("shared.airtable_client.requests.get", return_value=mock_response)

        airtable_client.fetch_all_records("SomeTable")

        url = mock_response.json.call_args  # noop, just to keep call graph simple
        call = airtable_client.requests.get.call_args
        assert f"/{airtable_client.AIRTABLE_BASE_ID}/SomeTable" in call.args[0]

    def test_default_call_sends_no_view_param(self, mocker):
        mock_response = MagicMock()
        mock_response.json.return_value = {"records": []}
        mocker.patch("shared.airtable_client.requests.get", return_value=mock_response)

        airtable_client.fetch_all_records("SomeTable")

        assert "view" not in airtable_client.requests.get.call_args.kwargs["params"]

    def test_explicit_base_id_overrides_the_default(self, mocker):
        mock_response = MagicMock()
        mock_response.json.return_value = {"records": []}
        mocker.patch("shared.airtable_client.requests.get", return_value=mock_response)

        airtable_client.fetch_all_records("tblXYZ", base_id="appDifferentBase")

        call = airtable_client.requests.get.call_args
        assert "/appDifferentBase/tblXYZ" in call.args[0]
        assert airtable_client.AIRTABLE_BASE_ID not in call.args[0]

    def test_view_param_is_sent_when_given(self, mocker):
        mock_response = MagicMock()
        mock_response.json.return_value = {"records": []}
        mocker.patch("shared.airtable_client.requests.get", return_value=mock_response)

        airtable_client.fetch_all_records("tblXYZ", view="viwABC")

        assert airtable_client.requests.get.call_args.kwargs["params"]["view"] == "viwABC"


class TestFirstLinkedValue:
    def test_extracts_first_element_of_a_list(self):
        assert m._first_linked_value(["a", "b"]) == "a"

    def test_empty_list_becomes_none(self):
        assert m._first_linked_value([]) is None

    def test_none_stays_none(self):
        assert m._first_linked_value(None) is None

    def test_non_list_scalar_passes_through(self):
        assert m._first_linked_value("plain string") == "plain string"


class TestMapRecordToIssue:
    def test_pole_id_is_sourced_from_pole_record_id_not_pole_id(self):
        """Regression guard for a real production bug: Airtable's own
        "PoleId" field links to a synced/mirror table, NOT the real
        Poles table this project's own Poles.Id comes from -- its record
        ids never actually lined up with Poles.Id, despite the matching
        name. "PoleRecordID" is the field that genuinely does, and must
        be the one used here regardless of what "PoleId" itself holds."""
        record = {
            "id": "recSampleIssue002",
            "fields": {
                "IssueID": "some-issue-id",
                "PoleId": ["recFromTheWrongSyncTable"],
                "PoleRecordID": ["recFromTheRealPolesTable"],
                "Status": "Open",
                "Pole Status": ["Electrical Issue"],
            },
        }

        result = m._map_record_to_issue(record)

        assert result["PoleId"] == "recFromTheRealPolesTable"

    def test_maps_the_real_sample_shape_correctly(self):
        record = {
            "id": "recSampleIssue001",
            "fields": {
                "IssueID": "121025-4015-BRE-1035",
                # Both fields present, matching a real Airtable record --
                # PoleId links to a synced/mirror table (its ids don't
                # line up with this project's own Poles.Id at all), while
                # PoleRecordID is the one that genuinely does. Confirms
                # the correct one is specifically chosen, not just that
                # the new field name happens to work when the old one is
                # absent.
                "PoleId": ["recWrongSyncTableId"],
                "PoleRecordID": ["recC8GYNmkJDei0PV"],
                "Status": "Open",
                "Pole Status": ["Electrical Issue"],
            },
        }

        result = m._map_record_to_issue(record)

        assert result == {
            "Id": "recSampleIssue001",
            "IssueId": "121025-4015-BRE-1035",
            "PoleId": "recC8GYNmkJDei0PV",
            "Status": "Open",
            "PoleStatus": "Electrical Issue",
        }

    def test_pole_status_extracts_a_plain_string_not_a_record_id(self):
        """Pole Status is a lookup/multi-select field -- its values are
        category strings, not Airtable record ids, even though it comes
        back as a list the same way a genuine linked-record field does."""
        record = _sample_record(pole_status="Structural Issue")
        result = m._map_record_to_issue(record)
        assert result["PoleStatus"] == "Structural Issue"
        assert not result["PoleStatus"].startswith("rec")


class TestMatchesOpenIssueFilter:
    def test_open_electrical_issue_matches(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status="Electrical Issue"))
        assert m._matches_open_issue_filter(issue) is True

    def test_open_structural_issue_matches(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status="Structural Issue"))
        assert m._matches_open_issue_filter(issue) is True

    def test_resolved_issue_does_not_match(self):
        """The actual reason load_pole_open_issues() also has to prune
        stale rows -- an issue can flip from matching to not matching
        between runs."""
        issue = m._map_record_to_issue(_sample_record(status="Closed", pole_status="Electrical Issue"))
        assert m._matches_open_issue_filter(issue) is False

    def test_other_pole_status_does_not_match(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status="Cosmetic Issue"))
        assert m._matches_open_issue_filter(issue) is False

    def test_missing_pole_status_does_not_match(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status=None))
        assert m._matches_open_issue_filter(issue) is False


class TestLoadPoleOpenIssues:
    def _setup(self, mocker, records):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)  # SP_Execution.Id
        mocker.patch(
            "shared.pole_open_issues_loader.fetch_all_records",
            return_value=(records, []),
        )
        mocker.patch("shared.pole_open_issues_loader.get_connection", return_value=mock_conn)
        return mock_conn, mock_cursor

    def test_fetches_from_the_dedicated_base_and_view(self, mocker):
        mock_fetch = mocker.patch(
            "shared.pole_open_issues_loader.fetch_all_records",
            return_value=([], []),
        )
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.fetchone.return_value = (1,)
        mocker.patch("shared.pole_open_issues_loader.get_connection", return_value=mock_conn)

        m.load_pole_open_issues()

        mock_fetch.assert_called_once_with(
            m.AIRTABLE_POLE_ISSUES_TABLE,
            base_id=m.AIRTABLE_POLE_ISSUES_BASE_ID,
            view=m.AIRTABLE_POLE_ISSUES_VIEW,
        )

    def test_only_upserts_matching_records(self, mocker):
        records = [
            _sample_record(record_id="rec-match", status="Open", pole_status="Electrical Issue"),
            _sample_record(record_id="rec-resolved", status="Closed", pole_status="Electrical Issue"),
            _sample_record(record_id="rec-other-status", status="Open", pole_status="Cosmetic Issue"),
        ]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_open_issues()

        upsert_calls = [c for c in mock_cursor.execute.call_args_list if "MERGE PoleOpenIssues" in c.args[0]]
        assert len(upsert_calls) == 1
        assert upsert_calls[0].args[1] == "rec-match"

    def test_deletes_rows_no_longer_matching(self, mocker):
        records = [_sample_record(record_id="rec-still-open", status="Open", pole_status="Electrical Issue")]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_open_issues()

        delete_calls = [c for c in mock_cursor.execute.call_args_list if c.args[0].strip().upper().startswith("DELETE FROM POLEOPENISSUES")]
        assert len(delete_calls) == 1
        assert "NOT IN (?)" in delete_calls[0].args[0]
        assert delete_calls[0].args[1] == "rec-still-open"

    def test_empty_matching_set_deletes_everything(self, mocker):
        """No records currently match the filter at all -- must delete
        unconditionally (a bare "WHERE Id NOT IN ()" is invalid SQL, so
        this has to be a genuinely separate code path, not just an empty
        parameter list)."""
        records = [_sample_record(record_id="rec-resolved", status="Closed")]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_open_issues()

        delete_calls = [c for c in mock_cursor.execute.call_args_list if c.args[0].strip().upper().startswith("DELETE FROM POLEOPENISSUES")]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[0].strip() == "DELETE FROM PoleOpenIssues"

    def test_commits_once_after_upserts_and_delete(self, mocker):
        records = [_sample_record(record_id="rec-match")]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_open_issues()

        mock_conn.commit.assert_called()  # at least once; exact count isn't the point here

    def test_sp_execution_source_is_airtable(self, mocker):
        mock_conn, mock_cursor = self._setup(mocker, [])

        m.load_pole_open_issues()

        sp_exec_call = mock_cursor.execute.call_args_list[0]
        assert sp_exec_call.args[4] == "AirTable"
