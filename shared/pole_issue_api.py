"""
create_pole_issue: creates a new issue for a pole in Airtable and syncs
it back to our DB.

Flow:
  1. Look up the pole by PoleNumber from our DB -- need Poles.Id (the
     Airtable record ID in the main base) and Poles.LinkedPoleId (the
     corresponding record ID in the synced Poles table inside the Issues
     base, required for the linked PoleId field on PoleIssues).
  2. PATCH the Airtable Poles record to set the pole's Status field.
  3. POST a new PoleIssues record using LinkedPoleId.
  4. Call load_pole_issues() to sync the new record back to our DB.
     Waits 2s before first sync; retries after 5s if the new record
     isn't found yet (Airtable propagation delay).
"""

import logging
import os
import time

import requests

from shared.pole_issues_loader import load_pole_issues
from shared.sql_client import get_connection

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
AIRTABLE_POLE_ISSUES_BASE_ID = os.environ["AIRTABLE_POLE_ISSUES_BASE_ID"]

AIRTABLE_POLES_TABLE = "Streetleaf Poles"
AIRTABLE_POLE_ISSUES_TABLE = "tblKEoTFRGOz7BT84"
AIRTABLE_API_URL = "https://api.airtable.com/v0"

_VALID_POLE_STATUSES = ("Electrical Issue", "Structural Issue")

_LOOKUP_POLE_SQL = """
SELECT Id, LinkedPoleId
FROM Poles
WHERE PoleNumber = ?
  AND Active = 1
"""


def _airtable_headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def _patch_pole_status(pole_airtable_id: str, status: str) -> None:
    url = f"{AIRTABLE_API_URL}/{AIRTABLE_BASE_ID}/{AIRTABLE_POLES_TABLE}/{pole_airtable_id}"
    response = requests.patch(
        url,
        headers=_airtable_headers(),
        json={"fields": {"Status": status}},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"Airtable PATCH Poles/{pole_airtable_id} failed "
            f"({response.status_code}): {response.text}"
        )
    logging.info(
        "create_pole_issue: patched Airtable Poles/%s Status → '%s'.",
        pole_airtable_id, status,
    )


def _post_pole_issue(linked_pole_id: str, problem_details: str) -> str:
    url = f"{AIRTABLE_API_URL}/{AIRTABLE_POLE_ISSUES_BASE_ID}/{AIRTABLE_POLE_ISSUES_TABLE}"
    payload = {
        "records": [{
            "fields": {
                "PoleId": [linked_pole_id],
                "Status": "Open",
                "Source": "Web",
                "ProblemDetails": problem_details,
            }
        }]
    }
    response = requests.post(url, headers=_airtable_headers(), json=payload, timeout=30)
    if not response.ok:
        raise RuntimeError(
            f"Airtable POST PoleIssues failed "
            f"({response.status_code}): {response.text}"
        )
    new_id = response.json()["records"][0]["id"]
    logging.info("create_pole_issue: created Airtable PoleIssues record %s.", new_id)
    return new_id


def create_pole_issue(
    pole_number: str,
    status: str,
    problem_details: str,
) -> dict:
    """
    Creates a new pole issue:
      1. Looks up the pole by PoleNumber (must be Active).
      2. PATCHes the Airtable Poles record Status field.
      3. POSTs a new Open PoleIssues record (Source='Web').
      4. Syncs the new record back via load_pole_issues(), with a 2s
         initial delay and a 5s retry if the record isn't found yet.
    """
    if not pole_number:
        raise ValueError("poleNumber is required")
    if not problem_details:
        raise ValueError("problemDetails is required")
    if status not in _VALID_POLE_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(_VALID_POLE_STATUSES)}")

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(_LOOKUP_POLE_SQL, pole_number)
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if row is None:
        raise ValueError(f"no active pole found with PoleNumber '{pole_number}'")

    pole_airtable_id, linked_pole_id = row

    if not linked_pole_id:
        raise RuntimeError(
            f"pole '{pole_number}' has no LinkedPoleId -- "
            "run loadLinkedPoles first to populate it"
        )

    _patch_pole_status(pole_airtable_id, status)
    new_issue_id = _post_pole_issue(linked_pole_id, problem_details)

    # Wait for Airtable to propagate, then sync back to DB
    time.sleep(2)
    load_pole_issues()

    # If the new record still isn't in DB, wait longer and retry once
    conn2 = get_connection()
    cursor2 = conn2.cursor()
    try:
        cursor2.execute("SELECT 1 FROM PoleIssues WHERE Id = ?", new_issue_id)
        found = cursor2.fetchone() is not None
    finally:
        cursor2.close()
        conn2.close()

    if not found:
        logging.info(
            "create_pole_issue: %s not yet in DB after first sync -- "
            "waiting 5s and retrying.",
            new_issue_id,
        )
        time.sleep(5)
        load_pole_issues()

    return {
        "issueId": new_issue_id,
        "poleNumber": pole_number,
        "status": status,
        "problemDetails": problem_details,
    }
