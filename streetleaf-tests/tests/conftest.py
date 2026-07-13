"""
Shared fixtures for the StreetLeaf test suite.

IMPORTANT: shared/airtable_client.py reads AIRTABLE_API_KEY and
AIRTABLE_BASE_ID from os.environ at *import time* (module-level globals,
not inside a function). That means these env vars must exist before the
module is first imported by anything -- including by pytest's test
collection. We set sane defaults here, at the top of conftest.py, before
any other import in this file runs.
"""

import os

os.environ.setdefault("AIRTABLE_API_KEY", "test-airtable-key")
os.environ.setdefault("AIRTABLE_BASE_ID", "test-base-id")
os.environ.setdefault("SQL_CONNECTION_STRING", "test-connection-string")
os.environ.setdefault("ENVIRONMENT", "Dev")

import json
from unittest.mock import MagicMock

import pytest


# --------------------------------------------------------------------------
# Airtable fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def make_airtable_record():
    """Factory for building a raw Airtable record dict with sane defaults."""

    def _make(
        record_id="rec0000000000001",
        created_time="2026-07-02T18:00:00.000Z",
        name="Acme Corp",
        project_names=None,
        executed_projects=None,
        street="123 Main St",
        city="Clearwater",
        state="FL",
        zip_code="33755",
        phone_number="(727) 555-0100",
        extra_fields=None,
    ):
        fields = {
            "Name": name,
            "ProjectNames": project_names if project_names is not None else ["Project A"],
            "Executed Projects": (
                executed_projects if executed_projects is not None else ["proj1"]
            ),
            "Street": street,
            "City": city,
            "State": state,
            "Zip": zip_code,
            "Phone Number": phone_number,
        }
        if extra_fields:
            fields.update(extra_fields)

        return {
            "id": record_id,
            "createdTime": created_time,
            "fields": fields,
        }

    return _make


def make_airtable_response(records, offset=None):
    """Builds the JSON body Airtable's list-records endpoint would return."""
    body = {"records": records}
    if offset:
        body["offset"] = offset
    return body


@pytest.fixture
def mock_requests_get(mocker):
    """Patches requests.get inside shared.airtable_client and returns the mock."""
    return mocker.patch("shared.airtable_client.requests.get")


def make_http_response(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.side_effect = None
    return resp


# --------------------------------------------------------------------------
# SQL / pyodbc fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def mock_cursor():
    """
    A MagicMock standing in for a pyodbc cursor.

    fetchone() defaults to returning (1,) so code that does
    `sp_exec_id = cursor.fetchone()[0]` gets a truthy id (1) by default.
    Override cursor.fetchone.return_value in a test if you need a different id.
    """
    cursor = MagicMock(name="cursor")
    cursor.fetchone.return_value = (1,)
    return cursor


@pytest.fixture
def mock_conn(mock_cursor):
    """A MagicMock standing in for a pyodbc connection, wired to mock_cursor."""
    conn = MagicMock(name="connection")
    conn.cursor.return_value = mock_cursor
    return conn


@pytest.fixture
def patch_get_connection(mocker, mock_conn):
    """Patches shared.customers_loader.get_connection to return mock_conn."""
    return mocker.patch(
        "shared.customers_loader.get_connection", return_value=mock_conn
    )


@pytest.fixture
def patch_fetch_all_records(mocker):
    """Patches shared.customers_loader.fetch_all_records (already imported by name)."""
    return mocker.patch("shared.customers_loader.fetch_all_records")
