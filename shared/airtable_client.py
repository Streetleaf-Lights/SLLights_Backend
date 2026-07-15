import os
import time
import requests

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_URL = "https://api.airtable.com/v0"

PAGE_SIZE = 100  # Airtable's max records per page

# Airtable enforces ~5 requests/sec per base. A fixed sleep() after every
# request wastes time once the request's own round-trip latency already
# exceeds this interval (which is common -- e.g. ~0.39s/request measured
# in production, well over the 0.2s floor). Instead, track when the last
# request STARTED and only sleep whatever's left of MIN_REQUEST_INTERVAL_
# SECONDS -- if the request itself was already slower than that, no sleep
# is added at all.
MIN_REQUEST_INTERVAL_SECONDS = 0.2


def fetch_all_records(
    table_name: str, fields: list[str] | None = None
) -> tuple[list[dict], list[str]]:
    """
    Fetches every record from an Airtable table, following the `offset`
    pagination cursor until Airtable stops returning one.

    Args:
        table_name: the Airtable table to fetch from.
        fields: optional list of Airtable field names to request. When
            given, Airtable only returns those fields (plus the always-
            present `id`/`createdTime`), shrinking the response payload --
            worth passing whenever a loader only maps a handful of columns
            out of a much wider table. Omit to fetch every field (default,
            existing behavior).

    Returns:
        records: list of raw Airtable record dicts ({"id", "createdTime", "fields"})
        offsets_seen: list of offset tokens consumed along the way (for logging
                      into SP_Execution.BatchCount)
    """
    records: list[dict] = []
    offsets_seen: list[str] = []
    offset = None

    url = f"{AIRTABLE_API_URL}/{AIRTABLE_BASE_ID}/{table_name}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    last_request_started_at = None

    while True:
        if last_request_started_at is not None:
            elapsed = time.monotonic() - last_request_started_at
            remaining = MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                time.sleep(remaining)

        params = {"pageSize": PAGE_SIZE}
        if offset:
            params["offset"] = offset
        if fields:
            params["fields[]"] = fields

        last_request_started_at = time.monotonic()
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
