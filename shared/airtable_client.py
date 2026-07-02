import os
import time
import requests

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_API_URL = "https://api.airtable.com/v0"

PAGE_SIZE = 100              # Airtable's max records per page
REQUEST_DELAY_SECONDS = 0.2  # keeps us comfortably under Airtable's 5 req/sec limit


def fetch_all_records(table_name: str) -> tuple[list[dict], list[str]]:
    """
    Fetches every record from an Airtable table, following the `offset`
    pagination cursor until Airtable stops returning one.

    Returns:
        records: list of raw Airtable record dicts ({"id", "createdTime", "fields"})
        offsets_seen: list of offset tokens consumed along the way (for logging
                      into SP_Execution.BatchIds / BatchCount)
    """
    records: list[dict] = []
    offsets_seen: list[str] = []
    offset = None

    url = f"{AIRTABLE_API_URL}/{AIRTABLE_BASE_ID}/{table_name}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    while True:
        params = {"pageSize": PAGE_SIZE}
        if offset:
            params["offset"] = offset

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        records.extend(data.get("records", []))

        offset = data.get("offset")
        if offset:
            offsets_seen.append(offset)
            time.sleep(REQUEST_DELAY_SECONDS)
        else:
            break

    return records, offsets_seen
