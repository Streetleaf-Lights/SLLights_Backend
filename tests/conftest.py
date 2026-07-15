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
os.environ.setdefault(
    "LEADSUN_CLIENT_CERT_PEM",
    "-----BEGIN CERTIFICATE-----\ntest-cert\n-----END CERTIFICATE-----\n"
    "-----BEGIN PRIVATE KEY-----\ntest-key\n-----END PRIVATE KEY-----\n",
)

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


@pytest.fixture
def patch_get_connection_projects(mocker, mock_conn):
    """Patches shared.projects_loader.get_connection to return mock_conn."""
    return mocker.patch(
        "shared.projects_loader.get_connection", return_value=mock_conn
    )


@pytest.fixture
def patch_fetch_all_records_projects(mocker):
    """Patches shared.projects_loader.fetch_all_records (already imported by name)."""
    return mocker.patch("shared.projects_loader.fetch_all_records")


@pytest.fixture
def patch_get_connection_poles(mocker, mock_conn):
    """Patches shared.poles_loader.get_connection to return mock_conn."""
    return mocker.patch(
        "shared.poles_loader.get_connection", return_value=mock_conn
    )


@pytest.fixture
def patch_fetch_all_records_poles(mocker):
    """Patches shared.poles_loader.fetch_all_records (already imported by name)."""
    return mocker.patch("shared.poles_loader.fetch_all_records")


@pytest.fixture
def make_pole_record():
    """Factory for building a raw Airtable 'Streetleaf Poles' record dict
    with sane defaults, using the real Airtable field names."""

    def _make(
        record_id="rec_pole_0000001",
        created_time="2026-07-02T18:00:00.000Z",
        pole_number="P-1001",
        location_id="LOC-42",
        project_ids=None,
        customer_ids=None,
        install_date="2026-03-01",
        lat=27.9506,
        long=-82.4572,
        extra_fields=None,
    ):
        fields = {
            "Pole Number": pole_number,
            "Location ID": location_id,
            "Contracting Entity": project_ids if project_ids is not None else ["recProject123"],
            "Customer ID": customer_ids if customer_ids is not None else ["recCustomer456"],
            "Field Installed": install_date,
            "LAT": lat,
            "LONG": long,
        }
        if extra_fields:
            fields.update(extra_fields)

        return {
            "id": record_id,
            "createdTime": created_time,
            "fields": fields,
        }

    return _make


# --------------------------------------------------------------------------
# Leadsun / PoleRawData fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def mock_requests_get_leadsun(mocker):
    """Patches requests.get inside shared.leadsun_client and returns the mock."""
    return mocker.patch("shared.leadsun_client.requests.get")


@pytest.fixture
def patch_get_connection_pole_raw_data(mocker, mock_conn):
    """Patches shared.pole_raw_data_loader.get_connection to return mock_conn."""
    return mocker.patch(
        "shared.pole_raw_data_loader.get_connection", return_value=mock_conn
    )


@pytest.fixture
def patch_fetch_lamps(mocker):
    """Patches shared.pole_raw_data_loader.fetch_lamps (already imported by name)."""
    return mocker.patch("shared.pole_raw_data_loader.fetch_lamps")


@pytest.fixture
def make_lamp_record():
    """
    Factory for building a raw Leadsun lamp record dict, matching a real
    confirmed /lamps API response exactly (field names, nesting, and even
    the stray trailing space on lightingState).
    """

    def _make(
        product_name="12009-1000",
        last_upload="2026-07-15T12:35:30.000+00:00",
        extra_fields=None,
    ):
        record = {
            "batteryVoltage1": 13.52,
            "batteryVoltage2": 13.527,
            "batteryElecCurrent1": 100,
            "batteryElecCurrent2": 100,
            "lampPower1": 0,
            "lampPower2": 0,
            "solarBoardVoltage": 20.875,
            "solarBoardElecCurrent": 0.0,
            "dcInVoltage": 0.089,
            "batteryOutElecCurrent": 0.0,
            "batteryTemperature1": 0,
            "batteryTemperature2": 0,
            "mcuTemperature": 32.0,
            "envTemperature": 31.0,
            "lightingState": "lighting-off ",
            "dcInState": 3,
            "dcOutState": 1,
            "solarBoardState": 0,
            "battery1State": 2,
            "battery2State": 0,
            "lamp1State": 0,
            "lamp2State": 0,
            "controllerCode": "A3P70LA323110598",
            "productId": "AE3SAP7323113143",
            "createTime": None,
            "solarBoardDcStatus": "00000111",
            "lampBatteryStatus": "00000010",
            "userName": "12009-brevard",
            "id": 10358,
            "groupId": 1149,
            "groupName": "Chaparral Ph3 12009-1030",
            "gatewayCode": "GT18L94A25082883",
            "projectId": 482,
            "projectName": "Chaparral",
            "productName": product_name,
            "modelId": 135,
            "isOnline": True,
            "lastUpload": last_upload,
            "timeoutFlag": 3780,
            "longitude": -80.7236,
            "latitude": 27.99507,
            "controlModelCode": "55f50f5a48504b6aab6c7e4c709633cd",
            "controlModelName": "Storwatch 3",
        }
        if extra_fields:
            record.update(extra_fields)
        return record

    return _make


@pytest.fixture
def make_project_record():
    """Factory for building a raw Airtable 'Project Tracking' record dict
    with sane defaults, using the real Airtable field names."""

    def _make(
        record_id="rec_proj_0000001",
        created_time="2026-07-02T18:00:00.000Z",
        name="Downtown Fiber Rollout",
        pole_numbers=None,
        pole_ids=None,
        customer_ids=None,
        poles_under_contract=25,
        effective_date="2026-01-15",
        install_dates=None,
        extra_fields=None,
    ):
        fields = {
            "Executed Project": name,
            "PoleNumbers": pole_numbers if pole_numbers is not None else ["P-100", "P-101"],
            "Streetleaf Poles": pole_ids if pole_ids is not None else ["pole1", "pole2"],
            "Contracting Entity": customer_ids if customer_ids is not None else ["recCustomer123"],
            "Lights Under Contract": poles_under_contract,
            "Effective Date": effective_date,
            "Install Date(S)": install_dates if install_dates is not None else ["2026-03-01"],
        }
        if extra_fields:
            fields.update(extra_fields)

        return {
            "id": record_id,
            "createdTime": created_time,
            "fields": fields,
        }

    return _make
