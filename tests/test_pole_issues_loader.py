"""
Tests for shared/pole_issues_loader.py, plus the fetch_all_records()
extension in shared/airtable_client.py that this loader relies on.
"""

from unittest.mock import MagicMock, patch

import pytest

from shared import airtable_client, pole_issues_loader as m


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
        name. "OriginalPoleRecordID" is the field that genuinely does, and must
        be the one used here regardless of what "PoleId" itself holds."""
        record = {
            "id": "recSampleIssue002",
            "fields": {
                "IssueID": "some-issue-id",
                "PoleId": ["recFromTheWrongSyncTable"],
                "OriginalPoleRecordID": ["recFromTheRealPolesTable"],
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
                # OriginalPoleRecordID is the one that genuinely does. Confirms
                # the correct one is specifically chosen, not just that
                # the new field name happens to work when the old one is
                # absent.
                "PoleId": ["recWrongSyncTableId"],
                "OriginalPoleRecordID": ["recC8GYNmkJDei0PV"],
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
            "DateReported": None,  # filled in per pole timezone by load_pole_issues()
            "DateReportedRaw": None,
            "ProblemDetails": None,
            "IssueSource": None,
        }

    def test_pole_status_extracts_a_plain_string_not_a_record_id(self):
        """Pole Status is a lookup/multi-select field -- its values are
        category strings, not Airtable record ids, even though it comes
        back as a list the same way a genuine linked-record field does."""
        record = _sample_record(pole_status="Structural Issue")
        result = m._map_record_to_issue(record)
        assert result["PoleStatus"] == "Structural Issue"
        assert not result["PoleStatus"].startswith("rec")


class TestMatchesIssueFilter:
    def test_open_electrical_issue_matches(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status="Electrical Issue"))
        assert m._has_valid_pole_status(issue) is True

    def test_open_structural_issue_matches(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status="Structural Issue"))
        assert m._has_valid_pole_status(issue) is True

    def test_closed_issue_passes_filter(self):
        """Closed issues now pass the filter -- only PoleStatus is validated.
        IsOpenIssueFault uses WHERE Status = 'Open' at ingestion time."""
        issue = m._map_record_to_issue(_sample_record(status="Closed", pole_status="Electrical Issue"))
        assert m._has_valid_pole_status(issue) is True

    def test_other_pole_status_does_not_match(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status="Cosmetic Issue"))
        assert m._has_valid_pole_status(issue) is False

    def test_missing_pole_status_does_not_match(self):
        issue = m._map_record_to_issue(_sample_record(status="Open", pole_status=None))
        assert m._has_valid_pole_status(issue) is False


class TestLoadPoleIssues:
    def _setup(self, mocker, records):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (1,)  # SP_Execution.Id
        mocker.patch(
            "shared.pole_issues_loader.fetch_all_records",
            return_value=(records, []),
        )
        mocker.patch("shared.pole_issues_loader.get_connection", return_value=mock_conn)
        return mock_conn, mock_cursor

    def test_fetches_from_the_dedicated_base_and_view(self, mocker):
        mock_fetch = mocker.patch(
            "shared.pole_issues_loader.fetch_all_records",
            return_value=([], []),
        )
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.fetchone.return_value = (1,)
        mocker.patch("shared.pole_issues_loader.get_connection", return_value=mock_conn)

        m.load_pole_issues()

        mock_fetch.assert_called_once_with(
            m.AIRTABLE_POLE_ISSUES_TABLE,
            base_id=m.AIRTABLE_POLE_ISSUES_BASE_ID,
            view=m.AIRTABLE_POLE_ISSUES_VIEW,
        )

    def test_only_upserts_matching_records(self, mocker):
        records = [
            _sample_record(record_id="rec-match", status="Open", pole_status="Electrical Issue"),
            _sample_record(record_id="rec-invalid", status="Open", pole_status="Unknown Type"),
            _sample_record(record_id="rec-other-status", status="Open", pole_status="Cosmetic Issue"),
        ]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_issues()

        upsert_calls = [c for c in mock_cursor.execute.call_args_list if "MERGE PoleIssues" in c.args[0]]
        assert len(upsert_calls) == 1
        assert upsert_calls[0].args[1] == "rec-match"  # rec-invalid fails PoleStatus check

    def test_deletes_rows_no_longer_matching(self, mocker):
        records = [_sample_record(record_id="rec-still-open", status="Open", pole_status="Electrical Issue")]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_issues()

        delete_calls = [c for c in mock_cursor.execute.call_args_list if "DELETE FROM POLEISSUES" in c.args[0].strip().upper()]
        assert len(delete_calls) == 1
        assert "NOT IN (?)" in delete_calls[0].args[0]
        assert delete_calls[0].args[1] == "rec-still-open"

    def test_empty_matching_set_deletes_everything(self, mocker):
        """No records currently match the filter at all -- must delete
        unconditionally (a bare "WHERE Id NOT IN ()" is invalid SQL, so
        this has to be a genuinely separate code path, not just an empty
        parameter list)."""
        records = [_sample_record(record_id="rec-invalid", status="Open", pole_status="Unknown Type")]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_issues()

        delete_calls = [c for c in mock_cursor.execute.call_args_list if "DELETE FROM POLEISSUES" in c.args[0].strip().upper()]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[0].strip() == "DELETE FROM PoleIssues"

    def test_commits_once_after_upserts_and_delete(self, mocker):
        records = [_sample_record(record_id="rec-match")]
        mock_conn, mock_cursor = self._setup(mocker, records)

        m.load_pole_issues()

        mock_conn.commit.assert_called()  # at least once; exact count isn't the point here

    def test_sp_execution_source_is_airtable(self, mocker):
        mock_conn, mock_cursor = self._setup(mocker, [])

        m.load_pole_issues()

        sp_exec_call = mock_cursor.execute.call_args_list[0]
        assert sp_exec_call.args[4] == "AirTable"


class TestDateReportedInPoleLocalTime:
    """DateReported is shown in the POLE's own timezone, not one fixed zone."""

    def _record(self, pole="recPole1", raw="2026-09-01T14:30:00.000Z", record_id="rec1"):
        r = _sample_record(record_id=record_id)
        r["fields"]["OriginalPoleRecordID"] = [pole]
        r["fields"]["DateReportedF"] = raw
        return r

    def _setup(self, mocker, records, timezones):
        cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cursor
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = timezones
        mocker.patch("shared.pole_issues_loader.fetch_all_records", return_value=(records, []))
        mocker.patch("shared.pole_issues_loader.get_connection", return_value=conn)
        return cursor

    def _upserted_date(self, cursor, record_id="rec1"):
        calls = [c.args for c in cursor.execute.call_args_list if "MERGE PoleIssues" in c.args[0]]
        return next(a[6] for a in calls if a[1] == record_id)  # (sql, Id, IssueId, PoleId, Status, PoleStatus, DateReported, ...)

    def test_mapper_keeps_the_raw_value_for_later_conversion(self):
        issue = m._map_record_to_issue(self._record(raw="2026-09-01T14:30:00.000Z"))
        assert issue["DateReported"] is None
        assert issue["DateReportedRaw"] == "2026-09-01T14:30:00.000Z"

    def test_each_issue_is_converted_to_its_own_poles_timezone(self, mocker):
        records = [
            self._record(pole="recChicago", record_id="rec-chi"),
            self._record(pole="recPhoenix", record_id="rec-phx"),
        ]
        cursor = self._setup(mocker, records, [("recChicago", "America/Chicago"), ("recPhoenix", "America/Phoenix")])

        m.load_pole_issues()

        assert self._upserted_date(cursor, "rec-chi") == "2026-09-01 09:30:00.000 -05:00"
        assert self._upserted_date(cursor, "rec-phx") == "2026-09-01 07:30:00.000 -07:00"

    def test_pole_without_a_resolved_timezone_falls_back_to_eastern(self, mocker):
        cursor = self._setup(mocker, [self._record(pole="recNoTz")], [("recNoTz", None)])
        m.load_pole_issues()
        assert self._upserted_date(cursor) == "2026-09-01 10:30:00.000 -04:00"

    def test_missing_date_stays_null(self, mocker):
        cursor = self._setup(mocker, [self._record(raw=None)], [("recPole1", "America/Chicago")])
        m.load_pole_issues()
        assert self._upserted_date(cursor) is None


class TestFetchPoleIanaTimezones:
    def test_maps_pole_id_to_timezone(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("p1", "America/Chicago"), ("p2", None)]
        assert m._fetch_pole_iana_timezones(cursor, ["p1", "p2"]) == {"p1": "America/Chicago", "p2": None}

    def test_dedupes_and_skips_empty_ids(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        m._fetch_pole_iana_timezones(cursor, ["p1", "p1", None, "", "p2"])
        sql, *params = cursor.execute.call_args.args
        assert params == ["p1", "p2"]
        assert sql.count("?") == 2

    def test_no_ids_runs_no_query(self):
        cursor = MagicMock()
        assert m._fetch_pole_iana_timezones(cursor, []) == {}
        cursor.execute.assert_not_called()

    def test_chunks_so_one_statement_never_exceeds_the_parameter_limit(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        m._fetch_pole_iana_timezones(cursor, [f"p{i}" for i in range(2500)])
        sizes = [len(c.args) - 1 for c in cursor.execute.call_args_list]
        assert sizes == [1000, 1000, 500]  # SQL Server's hard limit is 2,100 parameters

    def test_joins_vendor_and_provisioned_poles_to_their_timezone_rows(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        m._fetch_pole_iana_timezones(cursor, ["p1"])
        sql = cursor.execute.call_args.args[0]
        assert "p.VendorPoleId = ptz.VendorPoleId" in sql
        assert "p.ProvisionedPoleId = ptz.ProvisionedPoleId" in sql
