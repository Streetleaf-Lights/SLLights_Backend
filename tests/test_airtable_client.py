"""Tests for shared/airtable_client.py"""

import pytest

from shared import airtable_client
from tests.conftest import make_airtable_response, make_http_response


def test_fetch_all_records_single_page(mock_requests_get):
    record = {"id": "rec1", "createdTime": "2026-07-02T18:00:00.000Z", "fields": {}}
    mock_requests_get.return_value = make_http_response(
        make_airtable_response([record])
    )

    records, offsets_seen = airtable_client.fetch_all_records("Companies_New")

    assert records == [record]
    assert offsets_seen == []
    assert mock_requests_get.call_count == 1


def test_fetch_all_records_paginates_until_no_offset(mock_requests_get):
    page1_record = {"id": "rec1", "createdTime": "t", "fields": {}}
    page2_record = {"id": "rec2", "createdTime": "t", "fields": {}}

    mock_requests_get.side_effect = [
        make_http_response(make_airtable_response([page1_record], offset="offsetToken1")),
        make_http_response(make_airtable_response([page2_record])),
    ]

    records, offsets_seen = airtable_client.fetch_all_records("Companies_New")

    assert records == [page1_record, page2_record]
    assert offsets_seen == ["offsetToken1"]
    assert mock_requests_get.call_count == 2

    # second call must carry the offset token forward as a query param
    second_call_params = mock_requests_get.call_args_list[1].kwargs["params"]
    assert second_call_params["offset"] == "offsetToken1"


def test_fetch_all_records_three_pages(mock_requests_get):
    r1 = {"id": "rec1", "createdTime": "t", "fields": {}}
    r2 = {"id": "rec2", "createdTime": "t", "fields": {}}
    r3 = {"id": "rec3", "createdTime": "t", "fields": {}}

    mock_requests_get.side_effect = [
        make_http_response(make_airtable_response([r1], offset="off1")),
        make_http_response(make_airtable_response([r2], offset="off2")),
        make_http_response(make_airtable_response([r3])),
    ]

    records, offsets_seen = airtable_client.fetch_all_records("Companies_New")

    assert records == [r1, r2, r3]
    assert offsets_seen == ["off1", "off2"]
    assert mock_requests_get.call_count == 3


def test_fetch_all_records_empty_table(mock_requests_get):
    mock_requests_get.return_value = make_http_response(make_airtable_response([]))

    records, offsets_seen = airtable_client.fetch_all_records("Companies_New")

    assert records == []
    assert offsets_seen == []


def test_fetch_all_records_propagates_http_errors(mock_requests_get):
    mock_requests_get.return_value = make_http_response({}, status_code=500)

    with pytest.raises(Exception):
        airtable_client.fetch_all_records("Companies_New")


def test_fetch_all_records_uses_bearer_auth_and_correct_url(mock_requests_get, monkeypatch):
    monkeypatch.setattr(airtable_client, "AIRTABLE_API_KEY", "secret-key-123")
    monkeypatch.setattr(airtable_client, "AIRTABLE_BASE_ID", "appABC123")
    mock_requests_get.return_value = make_http_response(make_airtable_response([]))

    airtable_client.fetch_all_records("Companies_New")

    call = mock_requests_get.call_args
    called_url = call.args[0] if call.args else call.kwargs.get("url")
    assert called_url == "https://api.airtable.com/v0/appABC123/Companies_New"
    assert call.kwargs["headers"]["Authorization"] == "Bearer secret-key-123"


def test_fetch_all_records_first_request_has_no_offset_param(mock_requests_get):
    mock_requests_get.return_value = make_http_response(make_airtable_response([]))

    airtable_client.fetch_all_records("Companies_New")

    first_call_params = mock_requests_get.call_args_list[0].kwargs["params"]
    assert "offset" not in first_call_params
    assert first_call_params["pageSize"] == airtable_client.PAGE_SIZE


def test_fetch_all_records_without_fields_arg_omits_fields_param(mock_requests_get):
    mock_requests_get.return_value = make_http_response(make_airtable_response([]))

    airtable_client.fetch_all_records("Companies_New")

    call_params = mock_requests_get.call_args_list[0].kwargs["params"]
    assert "fields[]" not in call_params


def test_fetch_all_records_with_fields_arg_restricts_response_fields(mock_requests_get):
    mock_requests_get.return_value = make_http_response(make_airtable_response([]))

    airtable_client.fetch_all_records("Streetleaf Poles", fields=["LAT", "LONG"])

    call_params = mock_requests_get.call_args_list[0].kwargs["params"]
    assert call_params["fields[]"] == ["LAT", "LONG"]


def test_fetch_all_records_fields_arg_applies_to_every_page(mock_requests_get):
    r1 = {"id": "rec1", "createdTime": "t", "fields": {}}
    r2 = {"id": "rec2", "createdTime": "t", "fields": {}}
    mock_requests_get.side_effect = [
        make_http_response(make_airtable_response([r1], offset="off1")),
        make_http_response(make_airtable_response([r2])),
    ]

    airtable_client.fetch_all_records("Streetleaf Poles", fields=["LAT", "LONG"])

    for call in mock_requests_get.call_args_list:
        assert call.kwargs["params"]["fields[]"] == ["LAT", "LONG"]


def test_fetch_all_records_sleeps_the_remaining_gap_when_request_was_fast(
    mock_requests_get, mocker
):
    mock_sleep = mocker.patch("shared.airtable_client.time.sleep")
    # monotonic() is called 3 times for a 2-page fetch: request-1 start,
    # elapsed-check before request-2, request-2 start. Simulate only 0.05s
    # having elapsed since request-1 started -- under the 0.2s floor.
    mocker.patch(
        "shared.airtable_client.time.monotonic",
        side_effect=[0.0, 0.05, 0.05],
    )

    r1 = {"id": "rec1", "createdTime": "t", "fields": {}}
    r2 = {"id": "rec2", "createdTime": "t", "fields": {}}
    mock_requests_get.side_effect = [
        make_http_response(make_airtable_response([r1], offset="off1")),
        make_http_response(make_airtable_response([r2])),
    ]

    airtable_client.fetch_all_records("Companies_New")

    # should sleep the remaining ~0.15s, not a flat 0.2s
    mock_sleep.assert_called_once()
    slept_for = mock_sleep.call_args.args[0]
    assert slept_for == pytest.approx(0.15, abs=1e-9)


def test_fetch_all_records_skips_sleep_when_request_was_already_slow(
    mock_requests_get, mocker
):
    """
    This is the production case: real Airtable round-trips (~0.39s
    measured) already exceed MIN_REQUEST_INTERVAL_SECONDS (0.2s), so no
    additional sleep should be added on top of naturally-slow requests.
    """
    mock_sleep = mocker.patch("shared.airtable_client.time.sleep")
    mocker.patch(
        "shared.airtable_client.time.monotonic",
        side_effect=[0.0, 0.5, 0.5],
    )

    r1 = {"id": "rec1", "createdTime": "t", "fields": {}}
    r2 = {"id": "rec2", "createdTime": "t", "fields": {}}
    mock_requests_get.side_effect = [
        make_http_response(make_airtable_response([r1], offset="off1")),
        make_http_response(make_airtable_response([r2])),
    ]

    airtable_client.fetch_all_records("Companies_New")

    mock_sleep.assert_not_called()


def test_fetch_all_records_no_sleep_for_single_page(mock_requests_get, mocker):
    mock_sleep = mocker.patch("shared.airtable_client.time.sleep")
    mock_requests_get.return_value = make_http_response(make_airtable_response([]))

    airtable_client.fetch_all_records("Companies_New")

    mock_sleep.assert_not_called()
