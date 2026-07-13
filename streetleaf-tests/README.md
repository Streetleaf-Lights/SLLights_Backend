# LightsApp Test Suite

Tests for the Airtable → Azure SQL sync pipeline (`function_app.py` +
`shared/`). 56 tests, all passing against the code as of July 13, 2026.

## What's covered

| File | Focus |
|---|---|
| `tests/test_airtable_client.py` | Pagination (single/multi-page), offset handling, rate-limit sleep, auth header, HTTP error propagation |
| `tests/test_sql_client.py` | Connection string from env, missing env var, pyodbc error passthrough |
| `tests/test_customers_loader.py` | `_to_dto_string` offset formatting, `_airtable_created_time_to_eastern` (winter/summer DST), `_map_record_to_customer` field mapping, full `load_customers()` flow (success, partial row failure, top-level failure + `ErrorMessage` update, cleanup-on-error), MERGE SQL structural checks |
| `tests/test_function_app.py` | Timer trigger fires only at 6 AM/6 PM Eastern (tested across DST via freezegun), `past_due` handling, manual HTTP trigger's `Prod` block, synchronous (non-threaded) execution |
| `tests/test_schema_integration.py` | Column-name consistency between code's SQL and documented schema; opt-in **real** end-to-end test |

## Running

```bash
pip install -r requirements-dev.txt --break-system-packages   # or in a venv, drop that flag
# pyodbc needs the unixODBC runtime to import:
sudo apt-get install -y unixodbc
pytest -v
```

All tests run fully mocked — no real Airtable or Azure SQL calls, no
credentials needed. `test_schema_integration.py::TestLiveIntegration` is
skipped by default.

### Running the real integration test (optional)

To actually exercise `load_customers()` against real Airtable + Azure SQL
(useful before/after a deploy), from an environment with real network
access and credentials:

```bash
export RUN_LIVE_INTEGRATION_TESTS=1
export AIRTABLE_API_KEY=...
export AIRTABLE_BASE_ID=...
export SQL_CONNECTION_STRING=...
export ENVIRONMENT=Dev   # test refuses to run if this is "Prod"
pytest -v -m integration
```

This **writes real rows** to your `SP_Execution` and `Customers` tables.
Point it at Dev, never Prod.

## ⚠️ Schema discrepancy found while writing these tests

`shared/customers_loader.py`'s MERGE statement reads/writes a `Customers`
column called **`SP_ExecId`**. The original schema design in our earlier
conversations created that column as **`BatchId`** (with a planned FK to
`SP_Execution.Id`, later dropped per your request).

The tests here lock in what the **code** currently expects (`SP_ExecId`).
Please confirm your live `Customers` table actually has an `SP_ExecId`
column — if it's still named `BatchId`, the MERGE will fail at runtime
with an invalid column name error. If you've already renamed it, this is
just documentation catching up to reality and you can ignore it.

## Other things worth knowing

- The manual HTTP trigger (`loadAirTableDataManual`) currently calls
  `load_customers()` **synchronously** in the request path — no
  `threading.Thread` fire-and-forget. If you're still relying on that
  pattern to dodge Azure's 230-second gateway timeout on Flex Consumption,
  it looks like it's not in this version of the code; `test_function_app.py`
  has a test (`test_is_synchronous_exception_propagates_to_caller`) that
  will start failing the moment threading is reintroduced, as a tripwire.
- `load_customers()`'s error-handling has a subtle edge case: `if sp_exec_id:`
  treats an `Id` of `0` the same as `None`/failure-before-insert, silently
  skipping the `ErrorMessage` update. Harmless in practice since SQL Server
  `IDENTITY` starts at 1, but there's a test
  (`test_sp_exec_id_of_zero_is_falsy_and_skips_error_update`) documenting it
  in case that assumption ever changes.
