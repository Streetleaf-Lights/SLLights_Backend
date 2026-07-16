"""
Schema / integration-style checks.

A note on scope: this sandbox has no network path to Azure SQL or a real
ODBC driver for SQL Server, so there is no way to run true integration
tests against a live database from here. What this file does instead:

1. Extracts the column names actually referenced by customers_loader.py's
   SQL strings and compares them against an explicit, documented "expected
   schema" -- so if someone edits a SQL string and typos or drops a column,
   these tests catch the drift between code and schema *before* it reaches
   a real database.

2. Provides a real, opt-in end-to-end integration test (`TestLiveIntegration`
   below) that actually calls load_customers() against real Airtable +
   Azure SQL credentials. It's skipped unless you explicitly set
   RUN_LIVE_INTEGRATION_TESTS=1 and provide real AIRTABLE_API_KEY /
   AIRTABLE_BASE_ID / SQL_CONNECTION_STRING env vars pointing at a
   non-Prod environment. Run it from your own machine/CI where those
   credentials and network access actually exist -- not from here.

IMPORTANT DISCREPANCY FOUND WHILE WRITING THESE TESTS:
Earlier schema design work in this project created a `BatchId` column on
Customers with a plan to reference SP_Execution.Id. The current
customers_loader.py code instead reads/writes a column called `SP_ExecId`.
The tests below lock in what the *code* currently expects. Please confirm
your live Customers table actually has an `SP_ExecId` column (renamed from
`BatchId`, or added alongside it) -- otherwise the MERGE statement will
fail at runtime with an invalid column name error.
"""

import os
import re

import pytest

from shared import customers_loader, projects_loader, poles_loader, pole_telemetry_loader, pole_models_loader


# --------------------------------------------------------------------------
# Expected schema, as inferred from what the code currently reads/writes.
# --------------------------------------------------------------------------

EXPECTED_SP_EXECUTION_COLUMNS = {
    "Id",
    "Name",
    "Environment",
    "StartDateTime",
    "EndDateTime",
    "Source",
    "BatchCount",
    "IsFinalBatch",
    "TotalSuccessfulRecords",
    "TotalErrorRecords",
    "ErrorMessage",
}

EXPECTED_CUSTOMERS_COLUMNS = {
    "Id",
    "Name",
    "ProjectNames",
    "ProjectIds",
    "SP_ExecId",  # see discrepancy note in module docstring
    "Address",
    "City",
    "State",
    "Zip",
    "Phone",
    "AirTableCreatedDateTime",
}

EXPECTED_PROJECTS_COLUMNS = {
    "Id",
    "Name",
    "PoleNumbers",
    "PoleIds",
    "SP_ExecId",
    "CustomerId",
    "PolesUnderContract",
    "EffectiveDate",
    "InstallDates",
    "AirTableCreatedDateTime",
}

EXPECTED_POLES_COLUMNS = {
    "Id",
    "PoleNumber",
    "LocationId",
    "ProjectId",
    "CustomerId",
    "InstallDate",
    "Lat",
    "Long",
    "SP_ExecId",
    "AirTableCreatedDateTime",
}

# Confirmed against a real Leadsun API response. Sourced directly from
# pole_telemetry_loader._ALL_COLUMNS rather than duplicated here by hand --
# with 46 columns, a hand-copied list is itself a drift risk. What these
# tests actually verify is that the SQL strings stay in sync with that
# single source of truth, not that the list itself is "correct" (that's
# covered by the DDL cross-check in test_pole_telemetry_loader.py).
EXPECTED_POLE_TELEMETRY_COLUMNS = set(pole_telemetry_loader._ALL_COLUMNS)

# Same reasoning as EXPECTED_POLE_TELEMETRY_COLUMNS above -- sourced
# directly from pole_models_loader._ALL_COLUMNS rather than duplicated by
# hand.
EXPECTED_POLE_MODELS_COLUMNS = set(pole_models_loader._ALL_COLUMNS)


def _columns_in_insert_into(sql: str, table: str) -> set:
    match = re.search(rf"INSERT INTO {table}\s*\(([^)]+)\)", sql)
    return {c.strip() for c in match.group(1).split(",")} if match else set()


def _columns_in_set_clause(sql: str) -> set:
    match = re.search(r"SET\s+(.+?)\s+WHERE", sql, re.DOTALL)
    if not match:
        return set()
    assignments = match.group(1).split(",")
    return {a.split("=")[0].strip() for a in assignments}


class TestSpExecutionSchemaConsistency:
    def test_insert_statement_columns_are_known(self):
        # Pulled straight from customers_loader.load_customers()'s opening INSERT.
        insert_sql = """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
        """
        cols = _columns_in_insert_into(insert_sql, "SP_Execution")
        assert cols.issubset(EXPECTED_SP_EXECUTION_COLUMNS)

    def test_success_update_columns_are_known(self):
        update_sql = """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = ?,
                IsFinalBatch = 1
            WHERE Id = ?
        """
        cols = _columns_in_set_clause(update_sql)
        assert cols.issubset(EXPECTED_SP_EXECUTION_COLUMNS)

    def test_error_update_columns_are_known(self):
        update_sql = """
            UPDATE SP_Execution
            SET EndDateTime = ?, ErrorMessage = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?
            WHERE Id = ?
        """
        cols = _columns_in_set_clause(update_sql)
        assert cols.issubset(EXPECTED_SP_EXECUTION_COLUMNS)


class TestCustomersSchemaConsistency:
    def test_merge_insert_columns_are_known(self):
        sql = customers_loader._UPSERT_SQL
        match = re.search(r"INSERT \(([^)]+)\)", sql)
        cols = {c.strip() for c in match.group(1).split(",")}
        assert cols == EXPECTED_CUSTOMERS_COLUMNS

    def test_merge_update_set_columns_are_known(self):
        sql = customers_loader._UPSERT_SQL
        match = re.search(r"THEN UPDATE SET\s*(.+?)\s*WHEN NOT MATCHED", sql, re.DOTALL)
        assignments = match.group(1).strip().rstrip(",").split(",")
        cols = {a.split("=")[0].strip() for a in assignments}
        # Update path intentionally never touches Id or AirTableCreatedDateTime
        assert cols == EXPECTED_CUSTOMERS_COLUMNS - {"Id", "AirTableCreatedDateTime"}

    def test_merge_match_key_is_id(self):
        sql = customers_loader._UPSERT_SQL
        assert "ON target.Id = source.Id" in sql


class TestProjectsSchemaConsistency:
    def test_merge_insert_columns_are_known(self):
        sql = projects_loader._PROJECT_UPSERT_SQL
        match = re.search(r"INSERT \(([^)]+)\)", sql)
        cols = {c.strip() for c in match.group(1).split(",")}
        assert cols == EXPECTED_PROJECTS_COLUMNS

    def test_merge_update_set_columns_are_known(self):
        sql = projects_loader._PROJECT_UPSERT_SQL
        match = re.search(r"THEN UPDATE SET\s*(.+?)\s*WHEN NOT MATCHED", sql, re.DOTALL)
        assignments = match.group(1).strip().rstrip(",").split(",")
        cols = {a.split("=")[0].strip() for a in assignments}
        # Update path intentionally never touches Id or AirTableCreatedDateTime
        assert cols == EXPECTED_PROJECTS_COLUMNS - {"Id", "AirTableCreatedDateTime"}

    def test_merge_match_key_is_id(self):
        assert "ON target.Id = source.Id" in projects_loader._PROJECT_UPSERT_SQL

    def test_no_fk_reference_to_customers(self):
        """
        Locks in the deliberate design choice: Projects.CustomerId has no
        FK back to Customers, because load_projects() runs before
        load_customers() in function_app.py. If a REFERENCES Customers
        clause shows up here later, it needs to come with a load-order
        change too (or the MERGE will start failing on new Projects whose
        Customer hasn't loaded yet).
        """
        assert "REFERENCES Customers" not in projects_loader._PROJECT_UPSERT_SQL


class TestPolesSchemaConsistency:
    def test_merge_insert_columns_are_known(self):
        sql = poles_loader._POLE_UPSERT_SQL
        match = re.search(r"INSERT \(([^)]+)\)", sql)
        cols = {c.strip() for c in match.group(1).split(",")}
        assert cols == EXPECTED_POLES_COLUMNS

    def test_merge_update_set_columns_are_known(self):
        sql = poles_loader._POLE_UPSERT_SQL
        match = re.search(r"THEN UPDATE SET\s*(.+?)\s*WHEN NOT MATCHED", sql, re.DOTALL)
        assignments = match.group(1).strip().rstrip(",").split(",")
        cols = {a.split("=")[0].strip() for a in assignments}
        # Update path intentionally never touches Id or AirTableCreatedDateTime
        assert cols == EXPECTED_POLES_COLUMNS - {"Id", "AirTableCreatedDateTime"}

    def test_merge_match_key_is_id(self):
        assert "ON target.Id = source.Id" in poles_loader._POLE_UPSERT_SQL

    def test_no_fk_references(self):
        """
        Locks in the deliberate design choice: Poles.ProjectId/CustomerId
        have no FK, because load_poles() runs before both load_projects()
        and load_customers() in function_app.py.
        """
        sql = poles_loader._POLE_UPSERT_SQL
        assert "REFERENCES Projects" not in sql
        assert "REFERENCES Customers" not in sql

    def test_staging_table_columns_match_expected_schema(self):
        sql = poles_loader._STAGING_TABLE_SQL
        match = re.search(r"CREATE TABLE #PolesStaging \((.+)\);", sql, re.DOTALL)
        cols = {line.strip().split()[0] for line in match.group(1).strip().split(",")}
        assert cols == EXPECTED_POLES_COLUMNS

    def test_merge_from_staging_insert_columns_match_expected_schema(self):
        sql = poles_loader._MERGE_FROM_STAGING_SQL
        match = re.search(r"INSERT \(([^)]+)\)", sql)
        cols = {c.strip() for c in match.group(1).split(",")}
        assert cols == EXPECTED_POLES_COLUMNS

    def test_merge_from_staging_update_columns_match_expected_schema(self):
        sql = poles_loader._MERGE_FROM_STAGING_SQL
        match = re.search(r"THEN UPDATE SET\s*(.+?)\s*WHEN NOT MATCHED", sql, re.DOTALL)
        assignments = match.group(1).strip().rstrip(",").split(",")
        cols = {a.split("=")[0].strip() for a in assignments}
        assert cols == EXPECTED_POLES_COLUMNS - {"Id", "AirTableCreatedDateTime"}


class TestPoleTelemetrySchemaConsistency:
    def test_staging_table_columns_match_expected_schema(self):
        sql = pole_telemetry_loader._STAGING_TABLE_SQL
        match = re.search(r"CREATE TABLE #PoleTelemetryStaging \((.+)\);", sql, re.DOTALL)
        cols = {line.strip().split()[0] for line in match.group(1).strip().split(",")}
        assert cols == EXPECTED_POLE_TELEMETRY_COLUMNS

    def test_merge_from_staging_insert_columns_match_expected_schema(self):
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        match = re.search(r"INSERT \(([^)]+)\)", sql)
        cols = {c.strip() for c in match.group(1).split(",")}
        assert cols == EXPECTED_POLE_TELEMETRY_COLUMNS

    def test_merge_from_staging_update_columns_match_expected_schema(self):
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        match = re.search(r"THEN UPDATE SET\s*(.+?)\s*WHEN NOT MATCHED", sql, re.DOTALL)
        assignments = match.group(1).strip().rstrip(",").split(",")
        cols = {a.split("=")[0].strip() for a in assignments}
        # Update path never touches the match key (LocationId/LastUpload)
        assert cols == EXPECTED_POLE_TELEMETRY_COLUMNS - {"LocationId", "LastUpload"}

    def test_no_fk_references(self):
        """PoleTelemetry is a separate ingestion pipeline from the
        Airtable-sourced tables and isn't meant to reference them."""
        sql = pole_telemetry_loader._MERGE_FROM_STAGING_SQL
        assert "REFERENCES" not in sql


class TestPoleModelsSchemaConsistency:
    def test_staging_table_columns_match_expected_schema(self):
        sql = pole_models_loader._STAGING_TABLE_SQL
        match = re.search(r"CREATE TABLE #PoleModelsStaging \((.+)\);", sql, re.DOTALL)
        cols = {line.strip().split()[0] for line in match.group(1).strip().split(",")}
        assert cols == EXPECTED_POLE_MODELS_COLUMNS

    def test_merge_from_staging_insert_columns_match_expected_schema(self):
        sql = pole_models_loader._MERGE_FROM_STAGING_SQL
        match = re.search(r"INSERT \(([^)]+)\)", sql)
        cols = {c.strip() for c in match.group(1).split(",")}
        assert cols == EXPECTED_POLE_MODELS_COLUMNS

    def test_merge_from_staging_update_columns_match_expected_schema(self):
        sql = pole_models_loader._MERGE_FROM_STAGING_SQL
        match = re.search(r"THEN UPDATE SET\s*(.+?)\s*WHEN NOT MATCHED", sql, re.DOTALL)
        assignments = match.group(1).strip().rstrip(",").split(",")
        cols = {a.split("=")[0].strip() for a in assignments}
        # Update path never touches the match key (ModelId)
        assert cols == EXPECTED_POLE_MODELS_COLUMNS - {"ModelId"}

    def test_no_fk_references(self):
        """PoleModels is part of the Leadsun pipeline but isn't referenced
        by / doesn't reference any other table here."""
        sql = pole_models_loader._MERGE_FROM_STAGING_SQL
        assert "REFERENCES" not in sql


# --------------------------------------------------------------------------
# Opt-in real end-to-end integration test.
#
# Skipped by default everywhere (including CI) unless a human deliberately
# sets RUN_LIVE_INTEGRATION_TESTS=1 alongside real credentials. This will
# actually write rows to Airtable... no wait, it reads from Airtable and
# writes to your real Customers / SP_Execution tables. Point it at Dev, not Prod.
# --------------------------------------------------------------------------

_LIVE_TESTS_ENABLED = os.environ.get("RUN_LIVE_INTEGRATION_TESTS") == "1"


@pytest.mark.integration
@pytest.mark.skipif(
    not _LIVE_TESTS_ENABLED,
    reason=(
        "Live integration test skipped. Set RUN_LIVE_INTEGRATION_TESTS=1 "
        "plus real AIRTABLE_API_KEY / AIRTABLE_BASE_ID / SQL_CONNECTION_STRING "
        "env vars (pointed at a non-Prod environment) to run this for real."
    ),
)
class TestLiveIntegration:
    def test_load_poles_then_projects_then_customers_against_real_airtable_and_sql(self):
        assert os.environ.get("ENVIRONMENT", "Dev") != "Prod", (
            "Refusing to run the live integration test with ENVIRONMENT=Prod. "
            "Point this at a Dev/Staging environment."
        )
        # Reload so the modules pick up the real env vars instead of the
        # test defaults conftest.py sets with setdefault().
        import importlib

        importlib.reload(poles_loader)
        importlib.reload(projects_loader)
        importlib.reload(customers_loader)

        # Same order as function_app.py: Poles -> Projects -> Customers.
        poles_loader.load_poles()  # will raise on failure -- that's the assertion
        projects_loader.load_projects()
        customers_loader.load_customers()


# Separate opt-in gate from the Airtable one above: PoleTelemetry is an
# independent pipeline (different source, different credentials --
# LEADSUN_CLIENT_CERT_PEM, not Airtable/SQL), so it gets its own flag
# rather than piggybacking on RUN_LIVE_INTEGRATION_TESTS.
_LEADSUN_LIVE_TESTS_ENABLED = os.environ.get("RUN_LIVE_LEADSUN_INTEGRATION_TEST") == "1"


@pytest.mark.integration
@pytest.mark.skipif(
    not _LEADSUN_LIVE_TESTS_ENABLED,
    reason=(
        "Live Leadsun integration test skipped. Set "
        "RUN_LIVE_LEADSUN_INTEGRATION_TEST=1 plus a real LEADSUN_CLIENT_CERT_PEM "
        "and SQL_CONNECTION_STRING (pointed at a non-Prod environment) to run "
        "this for real."
    ),
)
class TestLeadsunLiveIntegration:
    def test_load_pole_models_then_pole_telemetry_against_real_leadsun_api_and_sql(self):
        assert os.environ.get("ENVIRONMENT", "Dev") != "Prod", (
            "Refusing to run the live integration test with ENVIRONMENT=Prod. "
            "Point this at a Dev/Staging environment."
        )
        import importlib

        importlib.reload(pole_models_loader)
        importlib.reload(pole_telemetry_loader)

        # Same order as function_app.py's loadLeadsunData: Model -> RawData.
        pole_models_loader.load_pole_models()  # will raise on failure -- that's the assertion
        pole_telemetry_loader.load_pole_telemetry()
