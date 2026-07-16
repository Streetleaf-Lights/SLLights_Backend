# LightsApp — Airtable + Leadsun → Azure SQL Sync (Python Azure Functions)

A Python Azure Functions project (v2 programming model) that syncs pole,
project, and customer data from Airtable, plus device-model specs and raw
lamp telemetry from the Leadsun API, into Azure SQL Database on a
schedule, running on a Flex Consumption (Linux) plan.

## Project structure

```
Backend/
├── function_app.py         # Function definitions (v2 model, all triggers live here)
│                            #   - loadAirTableData: timer trigger, fires 6 AM/6 PM Eastern
│                            #     runs load_poles() -> load_projects() -> load_customers()
│                            #   - loadAirTableDataManual: manual HTTP trigger, blocked in Prod
│                            #   - loadLeadsunData: SEPARATE timer trigger, fires every 10 minutes,
│                            #     runs load_pole_models() -> load_pole_telemetry() -- unrelated to
│                            #     the above (was called loadPoleRawData before it covered two
│                            #     loaders instead of one)
│                            #   - loadLeadsunDataManual: manual HTTP trigger, blocked in Prod
├── shared/
│   ├── airtable_client.py       # Paginated Airtable fetch (fetch_all_records)
│   ├── leadsun_client.py        # Mutual-TLS fetch from the Leadsun API (fetch_lamps, fetch_models)
│   ├── sql_client.py            # Azure SQL connection helper (get_connection)
│   ├── datetime_utils.py        # Shared Eastern-time / DATETIMEOFFSET helpers
│   ├── customers_loader.py      # Airtable → Customers upsert logic (load_customers)
│   ├── projects_loader.py       # Airtable → Projects upsert logic (load_projects)
│   ├── poles_loader.py          # Airtable → Poles upsert logic (load_poles)
│   ├── pole_models_loader.py    # Leadsun → PoleModels upsert logic (load_pole_models)
│   └── pole_telemetry_loader.py # Leadsun → PoleTelemetry upsert + retention (load_pole_telemetry)
├── sql/                     # One folder per table; each has a guarded CREATE
│   │                        # and a scratch SELECT for querying/debugging in SSMS/ADS
│   ├── Customers/
│   │   ├── Create tbl Customers.sql
│   │   └── Select tbl Customers.sql
│   ├── Poles/
│   │   ├── Create tbl Poles.sql
│   │   └── Select tbl Poles.sql
│   ├── Projects/
│   │   ├── Create tbl Projects.sql
│   │   └── Select tbl Projects.sql
│   ├── PoleModels/
│   │   ├── Create tbl PoleModels.sql
│   │   └── Select tbl PoleModels.sql
│   ├── PoleTelemetry/
│   │   ├── Create tbl PoleTelemetry.sql
│   │   └── Select tbl PoleTelemetry.sql
│   ├── Rename PoleModel to PoleModels and PoleRawData to PoleTelemetry.sql
│   │                        # One-time migration for environments where these
│   │                        # tables already exist under their old names
│   └── SP_Execution/
│       ├── Create tbl SP_Execution.sql
│       └── Select tbl SP_Execution.sql
├── tests/                   # pytest suite — see "Running the tests" below
├── .vscode/                  # Editor settings, launch/task configs
├── .funcignore               # Files excluded from the deployment zip
├── host.json                 # Runtime configuration
├── local.settings.json       # Local dev settings (not committed to git)
├── requirements.txt          # Runtime dependencies
├── requirements-dev.txt      # Test-only dependencies
├── Backend.code-workspace    # VS Code workspace file
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
       "ENVIRONMENT": "Dev",
       "LEADSUN_CLIENT_CERT_PEM": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
     }
   }
   ```
   `LEADSUN_CLIENT_CERT_PEM` is the **entire contents** of the combined
   cert+key `.pem` file, as a single JSON string with real newlines escaped
   to `\n`. **Do not commit the actual `.pem` file to the repo** — it's a
   private key. See the Leadsun section under Notes below for why this is
   stored as a setting instead of a file, and how to get it into Azure.

4. Start the Functions host locally:
   ```bash
   func start
   ```

5. Test the manual HTTP triggers:
   ```bash
   curl -X POST http://localhost:7071/api/loadAirTableDataManual
   curl -X POST http://localhost:7071/api/loadLeadsunDataManual
   ```
   (Confirmed against `host.json` — no custom `routePrefix` is set, so the
   `/api/` prefix is correct as shown.)

## Running the tests

288 tests, fully mocked — no real Airtable, Leadsun, or Azure SQL calls,
no credentials needed for the default run.

| File | Focus |
|---|---|
| `tests/test_airtable_client.py` | Pagination (single/multi-page), offset handling, adaptive rate-limit pacing (sleeps only the remaining gap, skips it entirely when a request was already slow), optional `fields[]` payload restriction, auth header, HTTP error propagation |
| `tests/test_leadsun_client.py` | Cert materialized to a temp file with the right content, temp file cleaned up on both success and failure, correct URL/timeout, HTTP error propagation, `verify=` resolution (default/pinned CA/skip-verify precedence), the hostname-check-bypass adapter — including a real (non-mocked) check that `assert_hostname=False` actually reaches urllib3's pool config, not just the `SSLContext` — `fetch_models()` hitting the `/models` endpoint via the same shared `_get()`, and the fail-fast PEM validation (missing certificate/private-key blocks raise a clear error instead of a deep OpenSSL failure) |
| `tests/test_sql_client.py` | Connection string from env, missing env var, pyodbc error passthrough |
| `tests/test_datetime_utils.py` | `to_dto_string` offset formatting, `airtable_created_time_to_eastern` (winter/summer DST) |
| `tests/test_customers_loader.py` | `_map_record_to_customer` field mapping, full `load_customers()` flow (success, partial row failure, top-level failure + `ErrorMessage` update, cleanup-on-error), MERGE SQL structural checks, `ntext`-cast regression check, fetch/upsert phase-timing logs |
| `tests/test_projects_loader.py` | Same shape as `test_customers_loader.py`, for `load_projects()` — including the linked-Customer-id mapping, the NULL-safe `INTERSECT` diff check, the `ntext`-cast fix regression check, and fetch/upsert phase-timing logs |
| `tests/test_poles_loader.py` | Same shape again, for `load_poles()` — including the linked-Project-id mapping, the LAT/LONG error-string/whitespace cleanup, the staging-table bulk MERGE (with chunk-level fallback to row-by-row on a failed chunk), and the Airtable `fields[]` restriction |
| `tests/test_pole_models_loader.py` | `_capitalize_key`, `_parse_numeric_string` (int vs. float vs. non-numeric passthrough) against the real confirmed `/models` sample, the deliberate `LampsUsing` exception (bitmask string, not converted), `ModelId` needing no rename/conversion (native int, and *is* the real PK here unlike PoleTelemetry's `id`→`LeadsunId`), staging-table bulk MERGE + fallback |
| `tests/test_pole_telemetry_loader.py` | `_capitalize_key` (PascalCase, not `str.capitalize()`'s behavior), `_parse_iso_datetime`, `_map_lamp_record` against the real confirmed sample (productName→LocationId, id→LeadsunId, projectId/projectName→LeadsunProjectId/Name renames, string trimming, `ExtraFieldsJson` capture for unexpected fields), the missing-`LastUpload` sentinel (stable across calls, never eligible for retention purge, distinct from a genuine parse failure), staging-table bulk MERGE + fallback, retention purge logging |
| `tests/test_function_app.py` | Timer trigger fires only at 6 AM/6 PM Eastern (verified across the DST boundary with freezegun), `past_due` handling, manual HTTP trigger's `Prod` block, synchronous (non-threaded) execution, **Poles runs before Projects runs before Customers** in both triggers, a failure in an earlier loader blocks the later ones, **`loadLeadsunData` runs unconditionally** (no hour-gating) otherwise, runs **Model before RawData**, and never touches the Airtable loaders — and **both timer triggers skip entirely when `ENVIRONMENT == "Dev"`**, before even checking `past_due`, while both manual triggers are unaffected by that guard |
| `tests/test_schema_integration.py` | Column-name consistency between the code's SQL and a documented expected schema (all five tables); two opt-in **real** end-to-end tests (Airtable+SQL, and separately Leadsun+SQL) |

```bash
pytest -v
```

### Running the real integration tests (optional)

Two separate opt-in flags, since Airtable/SQL and Leadsun/SQL are
independent pipelines with different credentials.

**Airtable → SQL** (`load_poles()` → `load_projects()` → `load_customers()`):
```bash
export RUN_LIVE_INTEGRATION_TESTS=1
export AIRTABLE_API_KEY=...
export AIRTABLE_BASE_ID=...
export SQL_CONNECTION_STRING=...
export ENVIRONMENT=Dev   # test refuses to run if this is "Prod"
pytest -v -m integration
```

**Leadsun → SQL** (`load_pole_models()` → `load_pole_telemetry()`):
```bash
export RUN_LIVE_LEADSUN_INTEGRATION_TEST=1
export LEADSUN_CLIENT_CERT_PEM=...
export SQL_CONNECTION_STRING=...
export ENVIRONMENT=Dev   # test refuses to run if this is "Prod"
pytest -v -m integration
```

The Airtable test **writes real rows** to `SP_Execution`, `Poles`,
`Projects`, and `Customers`. The Leadsun test writes to `SP_Execution`,
`PoleModels`, and `PoleTelemetry`. Point either at Dev, never Prod.

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
  `load_poles()`, `load_projects()`, and `load_customers()` directly in the
  request path, no `threading.Thread` fire-and-forget. If a long Airtable
  sync risks hitting Azure's 230-second HTTP gateway timeout on Flex
  Consumption again, that's the pattern to reach for —
  `tests/test_function_app.py` has a tripwire test that'll fail the moment
  threading is reintroduced.
- **Both timer triggers skip entirely when `ENVIRONMENT == "Dev"`** —
  `loadAirTableData` and `loadLeadsunData` both check this first, before
  even looking at `myTimer.past_due`, and just log and return if it's
  `"Dev"`. This means running `func start` locally no longer fires real
  Airtable/Leadsun/SQL work on a schedule just because the host is up —
  the **manual triggers are unaffected by this check** and remain the only
  way to trigger a run while `ENVIRONMENT=Dev` (they already only block in
  `"Prod"`, unchanged). Set `ENVIRONMENT` to anything else (`"Staging"`,
  unset defaults to `"Dev"` though, so this needs to be explicit) to get
  the timers actually firing again, e.g. for a deployed Dev *slot* that
  should still run on schedule.
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
- **Poles table field mappings — all confirmed**: Airtable table is
  `Streetleaf Poles`. `Pole Number`→`PoleNumber`, `Location ID`→`LocationId`
  (plain scalar), `Field Installed`→`InstallDate`, `LAT`/`LONG`→`Lat`/`Long`.
  `ProjectId` and `CustomerId` are both linked-record fields (`Contracting
  Entity` and `Customer ID` respectively) — both stored as a list of ids in
  Airtable, first one taken. Note: `Contracting Entity` is the same-looking
  label used in `Project Tracking` (where it maps to `CustomerId` there) —
  confirmed as a coincidental naming reuse, not a shared meaning, so no
  action needed there.
- **Poles.ProjectId/CustomerId have no FK either, same reasoning** —
  `load_poles()` now runs before both `load_projects()` and
  `load_customers()`, so neither referenced row exists yet at insert time.
- **Fixed: `LAT`/`LONG` error strings failing to load** — Airtable returns
  literal error strings for these fields when the underlying formula/lookup
  can't resolve (e.g. an ungeocoded address, or a divide-by-zero in the
  formula), which don't fit `Poles.Lat`/`Poles.Long` (`FLOAT`).
  `_map_record_to_pole()` normalizes any of `'#NA'`, `'#ERROR!'`, or
  `'#DIV/0!'` (whitespace-trimmed) to `0` before the value reaches the
  MERGE. The set lives in `poles_loader._COORDINATE_ERROR_STRINGS` — add to
  it if other error strings turn up in the wild.
- **Fixed: `LAT`/`LONG` with leading/trailing whitespace failing to load**
  — `_clean_coordinate()` now `.strip()`s any string value for these two
  fields before anything else happens to it (including the error-string
  check above), so a value like `' 27.9506 '` loads as `'27.9506'` instead
  of failing.
- **`load_poles()` performance — three rounds of optimization, in order:**
  1. *Round-trip batching* (14k+ poles was taking ~12 minutes). Poles were
     switched from one `cursor.execute()` per row to `cursor.executemany()`
     with `cursor.fast_executemany = True`, cutting ~14,000 round trips to
     Azure SQL down to ~28. That alone got it to ~2 minutes.
  2. *Set-based bulk MERGE* — `fast_executemany` only cuts network round
     trips; the server still runs each MERGE statement in a batch
     individually, and that per-statement execution cost was assumed to be
     the remaining bottleneck. `load_poles()` stages each chunk into a
     local temp table (`#PolesStaging`, see `poles_loader._STAGING_TABLE_SQL`)
     via `executemany()`, then runs **one** set-based `MERGE ... USING
     #PolesStaging` per chunk (`poles_loader._MERGE_FROM_STAGING_SQL`)
     instead of one MERGE execution per row. Chunk size is
     `poles_loader._UPSERT_BATCH_SIZE` (2000).

     **Tradeoff**: a single bad row can now fail an entire chunk's
     set-based MERGE, not just that row. `load_poles()` handles this by
     falling back to the original row-by-row `_POLE_UPSERT_SQL` for any
     chunk that fails this way — so the "blast radius" of one bad pole is
     at most one chunk (2000 rows), not the whole run, and not a single
     row either. Re-running already-applied rows during that fallback is
     safe since MERGE is idempotent.

  3. *Measured data corrected the diagnosis, then fixed the real
     bottleneck.* `load_poles()` logs how long the Airtable fetch and the
     upsert phase each took (`loadPoles: fetched N record(s) ... in X.Xs`
     / `loadPoles: upsert phase took X.Xs`). A real run showed **fetch:
     86.5s, upsert: 51.7s** — the fetch, not the SQL writes, turned out to
     be the bigger piece (the earlier "~30-60s" estimate for it was wrong).
     Two fixes followed from that:
     - **`shared/airtable_client.py`'s pacing is now adaptive** instead of
       a flat `time.sleep(0.2)` after every page. It tracks elapsed time
       since the last request *started* and only sleeps whatever's left of
       `MIN_REQUEST_INTERVAL_SECONDS` (0.2s) — measured production
       latency was ~0.39s/request, already over that floor, so the fixed
       sleep was pure waste (~29s of the 86.5s was literally just
       `sleep()`). This is a pure win with no real downside: it still
       guarantees the same minimum spacing between requests, just doesn't
       double up on top of naturally-slow ones.
     - **`fetch_all_records()` now accepts an optional `fields` list** to
       restrict the Airtable API response to just the columns a loader
       actually reads (`poles_loader.AIRTABLE_POLES_FIELDS`), shrinking
       each page's payload. This one's payoff is less certain than the
       pacing fix — it depends on how many other fields the live
       `Streetleaf Poles` table has that `_map_record_to_pole()` doesn't
       use.

     **Measured result**: fetch dropped from 86.5s to **39.2s** — more
     than the ~29s expected from the pacing fix alone, so the `fields[]`
     restriction pulled real weight too (smaller per-page JSON payloads,
     not just less wasted sleep). Upsert held steady at ~50-52s (expected —
     nothing in this round touched the SQL side). Total run time: ~138s →
     **~90s (~1:30)**.

     Further gains from here would need the fetch and upsert phases to
     overlap instead of running sequentially — Airtable's cursor-based
     pagination means page N+1's request can't be sent until page N's
     response reveals the next offset, so the fetch itself can't be
     parallelized, but a genuinely concurrent (threaded/async) rewrite
     could let SQL writes for earlier pages happen while later pages are
     still being fetched. That's a bigger, riskier change for a smaller
     remaining gain, so it hasn't been done here.

  The batching/staging-table optimizations above aren't applied to
  `load_projects()`/`load_customers()`, since neither has anywhere near
  enough rows for them to matter yet — both can be lifted over if that
  changes. The **fetch/upsert phase-timing logs**, however, are: all three
  loaders now log `"load<X>: fetched N record(s) ... in X.Xs"` and
  `"load<X>: upsert phase took X.Xs for N record(s)"`, so the same
  before/after visibility is available everywhere, not just for Poles.

- **`loadLeadsunData` — separate pipeline (Leadsun → `PoleModels` +
  `PoleTelemetry`)**: runs on its own timer trigger every 10 minutes
  (`schedule="0 */10 * * * *"`), completely independent of
  `loadAirTableData` — different source, different cadence, no dependency
  between the two. Has its own manual HTTP trigger
  (`loadLeadsunDataManual`), same `Prod`-blocking convention as the others.
  This pipeline has been through two renames, in order:
  1. The **Azure Function** itself was renamed from `loadPoleRawData` to
     `loadLeadsunData` once it started orchestrating two loaders
     (`load_pole_models()` → `load_pole_telemetry()`, in that order)
     instead of one — mirrors `loadAirTableData`'s naming (source name +
     "Data" as the umbrella, individual `load_<x>()` functions
     underneath). Since this renamed a live, already-deployed Azure
     Function, redeploying it meant Azure treated it as a new function —
     no data loss, but a clean break in Application Insights' invocation
     history under the old name, and the manual trigger's URL changed
     (`/api/loadPoleRawDataManual` → `/api/loadLeadsunDataManual`).
  2. The **tables** `PoleModel` → `PoleModels` and `PoleRawData` →
     `PoleTelemetry` were renamed for consistency (`PoleModel` was
     singular where every other reference table here — `Customers`,
     `Projects`, `Poles` — is plural; `PoleTelemetry` is a more accurate,
     industry-standard name now that the schema is fully enumerated
     rather than the JSON-blob "raw" landing zone it started as). This
     cascaded to the Python side too: `pole_model_loader.py` →
     `pole_models_loader.py` (`load_pole_model()` → `load_pole_models()`),
     `pole_raw_data_loader.py` → `pole_telemetry_loader.py`
     (`load_pole_raw_data()` → `load_pole_telemetry()`), the `sql/`
     folders, and the `SP_Execution.Name` values these loaders log under
     (`"loadPoleModel"` → `"loadPoleModels"`,
     `"loadPoleRawData"` → `"loadPoleTelemetry"`). **Since these tables
     already existed live with real data**, `sql/Rename PoleModel to
     PoleModels and PoleRawData to PoleTelemetry.sql` has the one-time
     `sp_rename` migration (tables + their indexes) — run it once per
     environment *before* deploying this renamed code, or the MERGE/
     DELETE statements will fail with "invalid object name" against a
     database that still has the old table names. Safe to run more than
     once; each rename is guarded to only fire if the old name still
     exists.

  **`PoleModels` — new table, a device-model reference/catalog, not
  per-device telemetry.** Confirmed against a real Leadsun `/models`
  response (20 columns). Unlike `PoleTelemetry`, this is a simple
  (non-composite) primary key: **`ModelId` alone** — there's no
  `LocationId`/`LastUpload` concept here, just specs per device model.
  `ModelId` arrives as a real JSON integer already (not a string, unlike
  most of this table's other fields) and needs no rename either — unlike
  `PoleTelemetry`'s `id` → `LeadsunId`, a bare `ModelId` column here
  genuinely *is* this table's primary key, so there's no confusing
  collision with this project's conventions to avoid.

  **Numeric-string conversion** (`pole_models_loader._parse_numeric_string()`):
  several fields arrive from the API as numeric-looking strings (`"80"`,
  `"12.8"`, ...) and are converted to real `int`/`float` values rather
  than stored as text — tries `int()` first (no decimal point), then
  `float()`, leaving genuinely non-numeric or missing values as `None`/
  as-is. Stored uniformly as `FLOAT` columns (safe for both whole and
  fractional values) rather than picking `INT` vs `FLOAT` per column.
  **One deliberate exception**: `LampsUsing` (`"00000001"` in the sample)
  looks numeric but is treated as a bitmask-style string and left
  unconverted — same reasoning as `PoleTelemetry`'s `SolarBoardDcStatus`/
  `LampBatteryStatus`, where leading zeros are meaningful and would be
  silently lost by converting to an int. Worth double-checking this is
  the intended behavior for that field.

  Same `ExtraFieldsJson` safety net as `PoleTelemetry` (any field Leadsun
  sends that isn't a known column lands there, capitalized, instead of
  being dropped), and the same staging-table bulk MERGE pattern (batches
  of `pole_models_loader._UPSERT_BATCH_SIZE`, 2000, with row-by-row
  fallback on a failed chunk) — kept for consistency with the rest of the
  Leadsun pipeline even though `PoleModels` is a small reference table and
  doesn't need the performance benefit the way `Poles`/`PoleTelemetry` do.

  **`PoleTelemetry`** (the renamed `PoleRawData`): same pipeline and
  schema as before, just renamed for the reasons above.
  **Schema is confirmed against a real Leadsun `/lamps` response** (46
  columns) — every field is promoted to its own typed column, matching
  "consistent with our tables" directly rather than the JSON-blob fallback
  this started as. All field names are capitalized via `_capitalize_key()`
  (PascalCase — not Python's `str.capitalize()`, which would wrongly
  lowercase the rest of each name), with three deliberate exceptions:
  - `productName` → `LocationId` (the one rename explicitly requested)
  - Leadsun's own `id` → **`LeadsunId`**, not `Id` — a bare `Id` column
    would look like this table's primary key, but it isn't
    (`LocationId`+`LastUpload` is)
  - Leadsun's own `projectId`/`projectName` → **`LeadsunProjectId`**/
    **`LeadsunProjectName`**, not `ProjectId`/`ProjectName` — those would
    otherwise look like a reference to *our* Airtable-sourced `Projects`
    table; they're Leadsun's own internal project grouping, unrelated to
    ours. (`ProductId`, a *different* field from `productName`, has no
    such collision and keeps its plain capitalized name.)

  A small `ExtraFieldsJson` column remains as a safety net — any field
  Leadsun sends that *isn't* one of the 46 known columns (e.g. added in a
  future firmware/API update) lands there (capitalized, JSON-encoded)
  instead of being silently dropped. It's empty/`NULL` for every record in
  the confirmed sample, since all of its fields are now accounted for.

  **String fields are trimmed on the way in** — the confirmed sample had
  `lightingState` come back as `"lighting-off "` with a trailing space;
  every string value gets `.strip()`'d now, the same lesson already
  applied to Poles' `Lat`/`Long`.

  The single source of truth for the column list (order included) is
  `pole_telemetry_loader._ALL_COLUMNS` — the staging table DDL, the
  `MERGE`'s `INSERT`/`UPDATE` column lists, and the Python param-tuple
  order are all built from it (or cross-checked against it in tests), so
  there's one place to change if a column ever needs to move.

  **Confirmed, not assumed, from the real response**: single GET with no
  pagination; plain JSON array (not wrapped in an envelope); `lastUpload`
  as ISO-8601 with an explicit offset (e.g.
  `"2026-07-15T12:35:30.000+00:00"`) — matches what `_parse_iso_datetime()`
  already expected. `createTime` uses the same parser and was `null` in
  the sample; records with an unparseable/missing `LastUpload` (or
  `LocationId`) are still counted as row-level errors and skipped, since
  both are part of the primary key.

  **Upsert key = `(LocationId, LastUpload)`**, directly as the table's
  composite `PRIMARY KEY` — matches "upsert is based on the productName
  and lastUpload" literally. Uses the same staging-table bulk MERGE pattern
  as Poles (batches of `pole_telemetry_loader._UPSERT_BATCH_SIZE`, 2000,
  with row-by-row fallback on a failed chunk) rather than starting naive,
  since that pattern's already proven out.

  **Retention (6 months, based on `LastUpload`)** runs as a plain
  `DELETE ... WHERE LastUpload < DATEADD(MONTH, -6, SYSDATETIMEOFFSET())`
  at the end of every invocation (every 10 minutes) — not a separate
  scheduled job or partitioning scheme, since the loader already runs
  frequently enough that a simple indexed delete is plenty. Change
  `RETENTION_MONTHS` in `pole_telemetry_loader.py` to adjust the window.

  **Records with a genuinely missing `lastUpload`** (a handful of real
  devices report this — presumably ones that haven't uploaded yet) get a
  stable placeholder, `pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL`
  (`9999-12-31 23:59:59.999 +00:00`), instead of being dropped —
  `LastUpload` is half of the composite primary key, so it can never
  actually be `NULL`. The sentinel is deliberately: (1) the *same* value
  every run, so a device that keeps reporting `lastUpload: null` gets its
  one row updated in place each cycle rather than a new row inserted every
  10 minutes; and (2) far enough in the future that it's never
  `< 6 months ago`, so the retention purge above naturally never deletes
  it — no special-case exclusion needed. A `lastUpload` that's *present*
  but fails to parse (a real format surprise, not a legitimately-missing
  value) is left as a genuine row-level error rather than silently
  sentineled over, so an actual bug doesn't get masked. One accepted
  tradeoff: if a device later starts reporting a real `lastUpload`, that
  lands in a new row (real timestamp ≠ sentinel), and the old sentinel row
  is orphaned harmlessly — it just sits there indefinitely since it never
  ages past the retention cutoff. Not handled automatically; worth a
  manual cleanup pass if it ever becomes clutter.

  **Credential handling — `LEADSUN_CLIENT_CERT_PEM`**: the API uses mutual
  TLS (client certificate + private key), not a bearer token like
  Airtable. The combined cert+key `.pem` file is **not committed to the
  repo** — same reasoning as `local.settings.json` already being
  git-ignored for `AIRTABLE_API_KEY`/`SQL_CONNECTION_STRING`. Instead, the
  entire PEM content is stored as a single app setting
  (`LEADSUN_CLIENT_CERT_PEM`), and `leadsun_client._write_client_cert_to_temp_file()`
  materializes it to a temp file fresh on every call (cheap — a few KB),
  since `requests`' `cert=` parameter needs an actual filesystem path, not
  raw PEM text. The temp file is deleted immediately after the request,
  success or failure.

  **To set this up in Azure, prefer the CLI over pasting into the Portal
  UI**:
  ```bash
  az functionapp config appsettings set \
    --name <function-app-name> --resource-group <resource-group> \
    --settings "LEADSUN_CLIENT_CERT_PEM=$(cat leadsun_clean.pem)"
  ```
  This reads the file's raw bytes directly — pasting multi-line PEM text
  into the Portal's Configuration blade text box has actually mangled the
  value in practice (surfacing as `SSLError: [SSL] PEM lib` deep inside
  urllib3/OpenSSL when `context.load_cert_chain()` tries to parse a
  truncated/flattened cert). Verify what actually landed with:
  ```bash
  az functionapp config appsettings list \
    --name <function-app-name> --resource-group <resource-group> \
    --query "[?name=='LEADSUN_CLIENT_CERT_PEM'].value" -o tsv | wc -l
  ```
  (should match the local file's line count). For local dev, put it in
  `local.settings.json` with real newlines escaped to `\n` (see the local
  setup section above) —
  `python3 -c "import json; print(json.dumps(open('leadsun.pem').read()))"`
  will do that escaping correctly rather than editing it by hand.

  **Fail-fast validation**: since a mangled `LEADSUN_CLIENT_CERT_PEM` (or
  `LEADSUN_SERVER_CA_CERT`) otherwise fails deep inside urllib3/OpenSSL
  with an unhelpful `[SSL] PEM lib` error that doesn't say which setting
  or what's wrong, `leadsun_client._validate_pem_has_certificate()` checks
  for a `-----BEGIN CERTIFICATE-----` block up front (and
  `_write_client_cert_to_temp_file()` additionally checks for a private
  key block), raising a clear `ValueError` naming the setting and the
  likely cause instead.

  **Separate issue — verifying the *server's* certificate**: distinct from
  the client cert above, `leadsunedge-us.com` presents a **self-signed**
  server certificate, which fails against the public CA bundle
  `requests`/`certifi` trusts by default
  (`SSLCertVerificationError: self-signed certificate`). Two ways to
  handle it, both optional app settings:
  - **`LEADSUN_SERVER_CA_CERT`** (preferred) — PEM text of the server's
    cert (or its issuing CA) to trust specifically, same storage pattern
    as `LEADSUN_CLIENT_CERT_PEM`. To grab the cert the server is actually
    presenting:
    ```bash
    openssl s_client -connect leadsunedge-us.com:8550 -showcerts </dev/null 2>/dev/null \
      | openssl x509 -outform PEM > leadsun_server.pem
    ```
    then JSON-escape it the same way as the client cert and set it as
    `LEADSUN_SERVER_CA_CERT`.
  - **`LEADSUN_SKIP_TLS_VERIFY=true`** (escape hatch, insecure) — disables
    server certificate verification entirely, leaving the connection open
    to tampering. `leadsun_client._resolve_verify_option()` logs a warning
    every time this is active. Only reach for this if the real
    cert/CA genuinely isn't obtainable and the risk is accepted; if both
    settings are present, this one wins (deterministic, not silently
    picked).
  Leave both unset to keep the default `verify=True` behavior (fails
  against Leadsun's self-signed cert until one of the above is set).

  **A third issue can surface even after `LEADSUN_SERVER_CA_CERT` is set**:
  the pinned cert's Common Name/SAN may not actually match
  `leadsunedge-us.com` (`SSLCertVerificationError: Hostname mismatch...`)
  — common for lightweight self-signed certs on IoT gateways that get
  reused across deployments without customizing that field. Rather than
  jumping straight to `LEADSUN_SKIP_TLS_VERIFY` (which drops chain
  validation too), there's a middle ground:
  - **`LEADSUN_SKIP_HOSTNAME_CHECK=true`** — keeps validating that the
    server presents the *exact* certificate pinned via
    `LEADSUN_SERVER_CA_CERT` (chain validation stays on,
    `verify_mode=CERT_REQUIRED`), but stops requiring its name to match
    the connection hostname. Since `requests`' plain `verify=` kwarg can't
    express "validate the chain, skip just the hostname," this routes
    through a `requests.Session` with a custom `HTTPAdapter`
    (`leadsun_client._NoHostnameCheckAdapter`). That adapter has to
    disable hostname checking in **two separate places**, not one: the
    `ssl.SSLContext`'s own `check_hostname` flag, *and* urllib3's
    independent `assert_hostname` pool-level setting, which runs its own
    hostname check underneath `requests`' `verify=` handling regardless of
    what the SSLContext says. Setting only the SSLContext flag looks right
    but still fails with a hostname-mismatch error from urllib3 itself —
    both have to be off. Meant to be used **together with**
    `LEADSUN_SERVER_CA_CERT` — set alone, it falls back to the system's
    default trust store for chain validation, which still rejects a
    self-signed cert (a warning is logged if this happens).
  - If both `LEADSUN_SKIP_TLS_VERIFY` and `LEADSUN_SKIP_HOSTNAME_CHECK`
    are set, `LEADSUN_SKIP_TLS_VERIFY` wins — the fully-open path already
    covers it, no need for the custom adapter too.
