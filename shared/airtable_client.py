import os
import time
import requests

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_URL = "https://api.airtable.com/v0"

PAGE_SIZE = 100  # Airtable's max records per page
MIN_REQUEST_INTERVAL_SECONDS = 0.2  # keeps us comfortably under Airtable's 5 req/sec limit


def fetch_all_records(
    table_name: str, base_id: str = None, view: str = None, fields: list = None
) -> tuple[list[dict], list[str]]:
    """
    Fetches every record from an Airtable table, following the `offset`
    pagination cursor until Airtable stops returning one.

    base_id: which Airtable base to query -- defaults to AIRTABLE_BASE_ID
    (the base Customers/Projects/Poles all come from) if not given. Pass
    a different base id explicitly for a table living in a separate base
    (e.g. shared/pole_open_issues_loader.py's PoleOpenIssues table, which
    comes from a genuinely different Airtable base than everything else
    this project loads).

    view: optional Airtable view id/name to scope the query to (Airtable's
    own ?view= request parameter) -- omit to query the whole table,
    unfiltered by any view.

    fields: optional list of Airtable field names to restrict the response
    to (Airtable's own repeated ?fields[]=X&fields[]=Y request parameter)
    -- shrinks each page's response payload when the caller only reads a
    known subset of a record's fields (see poles_loader.py's
    AIRTABLE_POLES_FIELDS for the motivating case). Omitted entirely from
    the request when not given, not sent as an empty list.

    Rate limiting is adaptive, not a flat per-page sleep: each iteration
    measures how long has actually elapsed (via time.monotonic(), immune
    to system clock adjustments) since the previous request STARTED, and
    only sleeps whatever's left of MIN_REQUEST_INTERVAL_SECONDS -- real
    Airtable round-trips measured in production (~0.39s) already exceed
    that floor on their own, so a flat sleep on top of every request would
    be pure waste on the common case; this only sleeps when a request
    genuinely came back faster than the floor allows.

    Returns:
        records: list of raw Airtable record dicts ({"id", "createdTime", "fields"})
        offsets_seen: list of offset tokens consumed along the way (for logging
                      into SP_Execution.BatchCount)
    """
    records: list[dict] = []
    offsets_seen: list[str] = []
    offset = None
    last_request_start = None

    resolved_base_id = base_id or AIRTABLE_BASE_ID
    url = f"{AIRTABLE_API_URL}/{resolved_base_id}/{table_name}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    while True:
        if last_request_start is not None:
            elapsed = time.monotonic() - last_request_start
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        last_request_start = time.monotonic()

        params = {"pageSize": PAGE_SIZE}
        if view:
            params["view"] = view
        if fields:
            params["fields[]"] = fields
        if offset:
            params["offset"] = offset

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        records.extend(data.get("records", []))

        offset = data.get("offset")
        if offset:
            offsets_seen.append(offset)
        else:
            break

    return records, offsets_seen
