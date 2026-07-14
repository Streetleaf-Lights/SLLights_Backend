# LightsApp — Airtable → Azure SQL Sync (Python Azure Functions)

A Python Azure Functions project (v2 programming model) that syncs project
and customer data from Airtable into Azure SQL Database on a schedule,
running on a Flex Consumption (Linux) plan.

## Project structure

```
LightsApp/
├── function_app.py         # Function definitions (v2 model, all triggers live here)
│                            #   - loadAirTableData: timer trigger, fires 6 AM/6 PM Eastern
│                            #     runs load_projects() then load_customers(), in that order
│                            #   - loadAirTableDataManual: manual HTTP trigger, blocked in Prod
├── shared/
│   ├── airtable_client.py   # Paginated Airtable fetch (fetch_all_records)
│   ├── sql_client.py        # Azure SQL connection helper (get_connection)
│   ├── datetime_utils.py    # Shared Eastern-time / DATETIMEOFFSET helpers
│   ├── customers_loader.py  # Airtable → Customers upsert logic (load_customers)
│   └── projects_loader.py   # Airtable → Projects upsert logic (load_projects)
├── sql/
│   └── create_projects_table.sql  # Guarded CREATE TABLE for Projects
├── tests/                   # pytest suite — see "Running the tests" below
├── host.json                 # Runtime configuration
├── local.settings.json       # Local dev settings (not committed to git)
├── requirements.txt          # Runtime dependencies
├── requirements-dev.txt      # Test-only dependencies
└── .gitignore
```

## Prerequisites

- **Python 3.10–3.11** — the codebase uses `str | None`-style union type
  hints (PEP 604), which need Python 3.10+; Azure Functions doesn't yet
  support 3.12+ in production, so this is also the practical ceiling
  (check [current supported versions](https://learn.microsoft.com/azure/azure-functions/functions-versions) in Azure docs).
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
- **unixODBC runtime + ODBC Driver for SQL Server** — `pyodbc` needs these
  to even import, not just to connect. On Ubuntu/Debian:
  ```bash
  sudo apt-get install -y unixodbc
  # plus Microsoft's "ODBC Driver 18 for SQL Server" for real connections
  ```
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (for deployment)
- An Azure account with an active subscription (for deployment)

## Local setup

1. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # on Windows: .venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt   # only needed if you're running tests
   ```

3. Fill in `local.settings.json` with:
   ```json
   {
     "Values": {
       "AIRTABLE_API_KEY": "...",
       "AIRTABLE_BASE_ID": "...",
       "SQL_CONNECTION_STRING": "...",
       "ENVIRONMENT": "Dev"
     }
   }
   ```

4. Start the Functions host locally:
   ```bash
   func start
   ```

5. Test the manual HTTP trigger:
   ```bash
   curl -X POST http://localhost:7071/api/loadAirTableDataManual
   ```
   > If you've since set `routePrefix: ""` in `host.json` (as discussed
   > when trimming the `/api/` prefix off the deployed URL), drop `/api`
   > from the path above — the comment in `function_app.py` still shows
   > the old `/api/`-prefixed path, so double check against your actual
   > `host.json`.

## Running the tests

83 tests, fully mocked — no real Airtable or Azure SQL calls, no
credentials needed for the default run.

| File | Focus |
|---|---|
| `tests/test_airtable_client.py` | Pagination (single/multi-page), offset handling, rate-limit sleep, auth header, HTTP error propagation |
| `tests/test_sql_client.py` | Connection string from env, missing env var, pyodbc error passthrough |
| `tests/test_datetime_utils.py` | `to_dto_string` offset formatting, `airtable_created_time_to_eastern` (winter/summer DST) |
| `tests/test_customers_loader.py` | `_map_record_to_customer` field mapping, full `load_customers()` flow (success, partial row failure, top-level failure + `ErrorMessage` update, cleanup-on-error), MERGE SQL structural checks |
| `tests/test_projects_loader.py` | Same shape as `test_customers_loader.py`, for `load_projects()` — including the linked-Customer-id mapping and the NULL-safe `INTERSECT` diff check (needed since `EffectiveDate`/`InstallDate` are `DATE` columns) |
| `tests/test_function_app.py` | Timer trigger fires only at 6 AM/6 PM Eastern (verified across the DST boundary with freezegun), `past_due` handling, manual HTTP trigger's `Prod` block, synchronous (non-threaded) execution, **Projects runs before Customers** in both triggers |
| `tests/test_schema_integration.py` | Column-name consistency between the code's SQL and a documented expected schema (both tables); opt-in **real** end-to-end test |

```bash
pytest -v
```

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

## Adding more functions

With the Python v2 model, add new functions directly in `function_app.py`
using decorators, e.g.:

```python
@app.route(route="another-endpoint")
def another_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("Hello from another function!")
```

Other trigger types you can add similarly:
- `@app.timer_trigger(schedule="0 */5 * * * *", arg_name="myTimer")` — Timer trigger
- `@app.blob_trigger(arg_name="myblob", path="mycontainer/{name}", connection="AzureWebJobsStorage")` — Blob trigger
- `@app.queue_trigger(arg_name="msg", queue_name="myqueue", connection="AzureWebJobsStorage")` — Queue trigger

The commented-out `load_projects()` / `load_poles()` / `load_pole_statuses()`
calls in `loadAirTableData` are presumably where the next set of loaders
will plug in as they're built out.

## Deploying to Azure

The walkthrough below is the standard Consumption-plan flow via the `func`
CLI. **This isn't how LightsApp actually gets deployed** — it runs on
Flex Consumption and is deployed via zip upload with a manual `chmod 644`
(files) / `chmod 755` (directories) pass before packaging, to avoid the
Unix-permissions-in-the-zip issue that caused the `host.json`/`function_app.py`
"Permission denied" errors previously. Use this section as generic
reference for a from-scratch project, not as the LightsApp deploy runbook.

1. Log in:
   ```bash
   az login
   ```

2. Create the required Azure resources (resource group, storage account, and function app):
   ```bash
   az group create --name my-functions-rg --location eastus

   az storage account create \
     --name mystorageacct$RANDOM \
     --location eastus \
     --resource-group my-functions-rg \
     --sku Standard_LRS

   az functionapp create \
     --resource-group my-functions-rg \
     --consumption-plan-location eastus \
     --runtime python \
     --runtime-version 3.11 \
     --functions-version 4 \
     --name my-unique-function-app-name \
     --storage-account mystorageacctXXXX \
     --os-type Linux
   ```
   (For a Flex Consumption app specifically, the plan-creation flags differ —
   check `az functionapp create --help` for the current Flex Consumption
   options rather than assuming the `--consumption-plan-location` flag above
   applies.)

3. Deploy your code:
   ```bash
   func azure functionapp publish my-unique-function-app-name
   ```

## Notes

- `local.settings.json` holds secrets/connection strings for local
  development only — it's excluded from git via `.gitignore` and is never
  deployed.
- `host.json` controls the Functions runtime behavior; app-level settings
  (env vars) belong in `local.settings.json` locally and in the Function
  App's Configuration blade in Azure once deployed. Remember
  `WEBSITE_TIME_ZONE` has no effect on Flex Consumption's Linux hosts —
  that's why the Eastern-hour gating lives in `function_app.py` code
  instead.
- The manual HTTP trigger's default auth level is `FUNCTION`, meaning a
  function key is required when calling the deployed endpoint. It's also
  hard-blocked (403) when `ENVIRONMENT == "Prod"`, regardless of auth level.
- **Manual trigger runs synchronously** — `loadAirTableDataManual` calls
  `load_customers()` directly in the request path, no
  `threading.Thread` fire-and-forget. If a long Airtable sync risks
  hitting Azure's 230-second HTTP gateway timeout on Flex Consumption
  again, that's the pattern to reach for — `tests/test_function_app.py`
  has a tripwire test that'll fail the moment threading is reintroduced.
- **Schema discrepancy to verify**: `customers_loader.py`'s MERGE statement
  reads/writes a `Customers` column called `SP_ExecId`. Earlier schema
  design work in this project created that column as `BatchId` instead.
  Confirm your live table actually has `SP_ExecId` (renamed, or added
  alongside `BatchId`) — otherwise the MERGE fails at runtime with an
  invalid column name error.
- **Projects table field mappings — confirmed vs. still-guessed**:
  Airtable table is `Project Tracking`. Confirmed field mappings: `Executed Project` → `Name`, `Streetleaf Poles` → `PoleIds`, `Contracting Entity` → `CustomerId`, `Lights Under Contract` → `PolesUnderContract`, `Effective Date` → `EffectiveDate`, `Install Date(S)` → `InstallDates`. Still guessed
  (unconfirmed): the Airtable field name for `PoleNumbers`, plus the
  assumption that `Contracting Entity` returns a list of linked record ids
  (first one taken) the same way the old `CustomerId` guess did.
- **Fixed: `ntext` / `INTERSECT` error on some Project rows** — pyodbc
  binds string parameters as the legacy `ntext` type once they cross a
  length threshold, which happened for records with enough poles/install
  dates that the JSON-encoded `PoleNumbers`/`PoleIds`/`InstallDates` got
  long. `ntext` can't be used as an operand to `INTERSECT` (the diff-check
  Projects' MERGE relies on), so only those larger records failed with
  `The data type ntext cannot be used as an operand to the UNION,
  INTERSECT or EXCEPT operators`. Fixed by explicitly
  `CAST(? AS NVARCHAR(MAX))`-ing those three columns in the MERGE's
  `USING` subquery, so the driver's length-based type guess never matters.
  **Also preemptively applied the same cast to Customers' `ProjectNames`/
  `ProjectIds`** — same JSON-list-gets-long mechanism, just hasn't hit a
  large enough customer yet to surface there.
- **`InstallDates` is plural/multi-valued** — a Project can have more than
  one install date, so it's stored the same way as `PoleNumbers`/`PoleIds`:
  JSON-encoded text in an `NVARCHAR(MAX)` column, not a native `DATE`. This
  changed from the original single `InstallDate DATE` column.
- **Projects.CustomerId has no FK to Customers, on purpose** —
  `loadAirTableData` runs `load_projects()` before `load_customers()`, so
  the Customer a Project points at may not exist in the table yet at
  insert time. An FK constraint would make that insert fail. If you add
  one later, either flip the load order or make it deferred/not-enforced.
