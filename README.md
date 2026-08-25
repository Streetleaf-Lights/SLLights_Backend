# LightsApp — Airtable + Leadsun → Azure SQL Sync (Python Azure Functions)

A Python Azure Functions project (v2 programming model) that syncs pole,
project, and customer data from Airtable, plus device-model specs and raw
lamp telemetry from the Leadsun API, into Azure SQL Database on a
schedule, running on a Flex Consumption (Linux) plan. Also derives rolling
Hour/Day health-metric averages (`PoleVitals`) from that telemetry,
bucketed in each pole's own local timezone (resolved from its
coordinates, cached in `PoleTimeZones`) rather than one hardcoded zone for
every pole — including a live/recent-connectivity flag (`IsOnline`) and a
per-pole health classification (`LightStatus`: `Working`/`DayLight`/`Not
Working`) that compares each reading's actual behavior against whether it
should be daylight at that pole's location and moment; and exposes
read-only `getCustomers`/`getProjects`/`getPoleVitals`/`getPoles`/`getUsers`
HTTP API endpoints (meant for API Management + a website, not part of the
ETL pipeline).

## Project structure

```
Backend/
├── function_app.py         # Function definitions (v2 model, all triggers live here)
│                            #   - loadAirTableData: timer trigger, fires 6 AM/6 PM Eastern
│                            #     runs load_poles() -> load_projects() -> load_customers()
│                            #   - loadAirTableDataManual: manual HTTP trigger, blocked in Prod
│                            #   - loadLeadsunData: SEPARATE timer trigger, fires every 10 minutes,
│                            #     runs load_pole_models() -> load_pole_telemetry() ->
│                            #     load_pole_timezones() -> load_pole_daylight_flags() ->
│                            #     load_pole_vitals() -- unrelated to the above (was called
│                            #     loadPoleRawData before it covered more than one loader)
│                            #   - loadLeadsunDataManual: manual HTTP trigger, blocked in Prod
│                            #   - Both timer triggers skip entirely when ENVIRONMENT == "Dev"
│                            #   - getCustomers / getProjects / getPoleVitals / getPoles /
│                            #     getUsers: read-only
│                            #     endpoints, NOT part of the ETL pipeline -- meant for API
│                            #     Management + a website. No timer, no SP_Execution tracking, no
│                            #     Dev-skip -- see the Notes section below for what they do (and
│                            #     deliberately don't) enforce
├── shared/
│   ├── airtable_client.py       # Paginated Airtable fetch (fetch_all_records)
│   ├── leadsun_client.py        # Mutual-TLS fetch from the Leadsun API (fetch_lamps, fetch_models)
│   ├── sql_client.py            # Azure SQL connection helper (get_connection)
│   ├── datetime_utils.py        # Shared Eastern-time / DATETIMEOFFSET helpers
│   ├── timezone_utils.py        # Lat/long -> IANA -> Windows timezone resolution (timezonefinder)
│   ├── daylight_utils.py        # Lat/long + moment -> is the sun up (astral) -- used by
│   │                             # pole_daylight_flags_loader.py, see the Notes section below
│   ├── customers_loader.py      # Airtable → Customers upsert logic (load_customers)
│   ├── projects_loader.py       # Airtable → Projects upsert logic (load_projects)
│   ├── poles_loader.py          # Airtable → Poles upsert logic (load_poles)
│   ├── pole_models_loader.py    # Leadsun → PoleModels upsert logic (load_pole_models)
│   ├── pole_telemetry_loader.py # Leadsun → PoleTelemetry upsert + retention (load_pole_telemetry)
│   ├── pole_timezones_loader.py # PoleTelemetry coords → PoleTimeZones cache (load_pole_timezones)
│   ├── pole_daylight_flags_loader.py # Caches PoleTelemetry.IsDaylight per reading (load_pole_daylight_flags)
│   ├── pole_vitals_loader.py    # PoleTelemetry+PoleModels+PoleTimeZones → PoleVitals
│   ├── api_utils.py             # Shared helpers for the read-only query APIs (json_safe, clamp_limit)
│   ├── customers_api.py         # Read-only Customers query logic for getCustomers (get_customers)
│   ├── projects_api.py          # Read-only Projects query logic for getProjects (get_projects)
│   ├── pole_vitals_api.py       # Customer→Project pole-health rollup for getPoleVitals (get_pole_vitals)
│   ├── poles_api.py             # Flat pole listing for getPoles (get_poles) -- reuses pole_vitals_api.py's per-pole SQL directly
│   └── users_api.py             # Read-only Users query logic for getUsers (get_users), joined with Customers for customerName
├── scripts/
│   └── run_pole_vitals_backfill.py # One-off PoleVitals full-history backfill runner
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
│   │   ├── Select tbl PoleTelemetry.sql
│   │   ├── Select tbl PoleTelemetry joined with PoleModels.sql  # computed vitals, for spot-checks
│   │   └── Add IsDaylight column to PoleTelemetry.sql  # migration for pre-existing environments
│   ├── PoleTimeZones/
│   │   ├── Create tbl PoleTimeZones.sql
│   │   └── Select tbl PoleTimeZones.sql
│   ├── PoleVitals/
│   │   ├── Create tbl PoleVitals.sql
│   │   └── Select tbl PoleVitals.sql
│   ├── Users/                # Application login accounts -- NOT part of the ETL
│   │   │                      # pipeline (no Source/SP_ExecId, no loader)
│   │   ├── Create tbl Users.sql
│   │   ├── Select tbl Users.sql
│   │   └── Select tbl Users joined with Customers.sql
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

708 tests, fully mocked — no real Airtable, Leadsun, or Azure SQL calls,
no credentials needed for the default run.

| File | Focus |
|---|---|
| `tests/test_airtable_client.py` | Pagination (single/multi-page), offset handling, adaptive rate-limit pacing (sleeps only the remaining gap, skips it entirely when a request was already slow), optional `fields[]` payload restriction, auth header, HTTP error propagation |
| `tests/test_leadsun_client.py` | Cert materialized to a temp file with the right content, temp file cleaned up on both success and failure, correct URL/timeout, HTTP error propagation, `verify=` resolution (default/pinned CA/skip-verify precedence), the hostname-check-bypass adapter — including a real (non-mocked) check that `assert_hostname=False` actually reaches urllib3's pool config, not just the `SSLContext` — `fetch_models()` hitting the `/models` endpoint via the same shared `_get()`, and the fail-fast PEM validation (missing certificate/private-key blocks raise a clear error instead of a deep OpenSSL failure) |
| `tests/test_sql_client.py` | Connection string from env, missing env var, pyodbc error passthrough, `_decode_datetimeoffset()` (round-trip against hand-built wire-format bytes, positive/negative offsets, fraction-to-microsecond truncation), and that `get_connection()` actually registers it on the connection instance it returns |
| `tests/test_datetime_utils.py` | `to_dto_string` offset formatting, `airtable_created_time_to_eastern` (winter/summer DST) |
| `tests/test_customers_loader.py` | `_map_record_to_customer` field mapping, full `load_customers()` flow (success, partial row failure, top-level failure + `ErrorMessage` update, cleanup-on-error), MERGE SQL structural checks, `ntext`-cast regression check, fetch/upsert phase-timing logs |
| `tests/test_projects_loader.py` | Same shape as `test_customers_loader.py`, for `load_projects()` — including the linked-Customer-id mapping, the NULL-safe `INTERSECT` diff check, the `ntext`-cast fix regression check, and fetch/upsert phase-timing logs |
| `tests/test_poles_loader.py` | Same shape again, for `load_poles()` — including the linked-Project-id mapping, the LAT/LONG error-string/whitespace cleanup, the staging-table bulk MERGE (with chunk-level fallback to row-by-row on a failed chunk), and the Airtable `fields[]` restriction |
| `tests/test_pole_models_loader.py` | `_capitalize_key`, `_parse_numeric_string` (int vs. float vs. non-numeric passthrough) against the real confirmed `/models` sample, the deliberate `LampsUsing` exception (bitmask string, not converted), `ModelId` needing no rename/conversion (native int, and *is* the real PK here unlike PoleTelemetry's `id`→`LeadsunId`), staging-table bulk MERGE + fallback |
| `tests/test_pole_telemetry_loader.py` | `_capitalize_key` (PascalCase, not `str.capitalize()`'s behavior), `_parse_iso_datetime`, `_map_lamp_record` against the real confirmed sample (productName→LocationId, id→LeadsunId, projectId/projectName→LeadsunProjectId/Name renames, string trimming, `ExtraFieldsJson` capture for unexpected fields), the missing-`LastUpload` sentinel (stable across calls, never eligible for retention purge, distinct from a genuine parse failure), staging-table bulk MERGE + fallback, retention purge logging |
| `tests/test_timezone_utils.py` | `resolve_iana_timezone()`/`resolve_windows_timezone()` against real coordinates for every mapped US zone (including the Arizona/Mountain-time distinction, Anchorage, Honolulu, Puerto Rico), `None` input handling, an unmapped IANA zone returning `(iana, None)` with a logged error rather than guessing, the `(0.0, 0.0)` "Null Island" placeholder-GPS-fix case returning `(None, None)` with a distinct warning instead of misreporting `Etc/GMT` as needing a new mapping (and that only the *exact* pair triggers it, not nearby coordinates), out-of-range coordinates — including the real `-82533519.0` micro-degrees longitude seen in production — caught by an explicit range check before `timezonefinder` is even called, a `try/except` catching anything else `timezonefinder` itself rejects, a valid-range-but-genuinely-unresolvable coordinate logging its own distinguishable message, confirmation that none of these paths double-log on top of each other, and `IANA_TO_WINDOWS`'s own internal sanity (no duplicate keys, every value looks like a real Windows zone name) |
| `tests/test_daylight_utils.py` | `is_daylight()` against real astral-computed sunrise/sunset for a real project coordinate (noon/2am, and 5-minute-either-side-of-boundary checks), literal polar day/night at a real Alaska coordinate (confirming it correctly returns `True`/`False` rather than raising, unlike astral's own `sunrise()`/`sunset()`), a naive `datetime` raising `ValueError`, the same UTC instant agreeing regardless of which timezone was used to express it, and `use_civil_twilight`'s broader window differing from the (default) stricter sunrise/sunset definition right at the boundary |
| `tests/test_pole_timezones_loader.py` | The find-unresolved-locations query (`LEFT JOIN ... WHERE ptz.LocationId IS NULL`, excludes missing coordinates), the upsert `MERGE`'s shape, full-flow tests (`SP_Execution` open/close, per-location resolve+upsert, an unmapped Windows timezone still upserting with `NULL` rather than blocking, per-location failure isolation) |
| `tests/test_pole_daylight_flags_loader.py` | The find-unflagged-readings query (`INNER JOIN PoleTimeZones` — not `LEFT`, and requires `WindowsTimeZone IS NOT NULL`, since a reading whose location can't be trusted can't have its daylight status computed at all), full-flow tests (`SP_Execution` open/close, per-reading `is_daylight()` call using `PoleTimeZones`' cached coordinates — not `PoleTelemetry`'s own — batched `UPDATE` with a per-row fallback, per-reading failure isolation); **a real bug caught in production**: `LastUpload` must be written back as a `_to_dto_string()`-formatted string, not the raw `datetime` read back from the `SELECT` — binding it raw hit the established pyodbc + `DATETIMEOFFSET` write-parameter gotcha, silently matching zero rows on every `UPDATE` with no exception raised, while `SP_Execution` kept reporting success regardless; also covers the new "zero rows affected" warning added specifically to surface that exact failure mode loudly if it ever recurs |
| `tests/test_pole_vitals_loader.py` | `_compute_cutoff()`'s lookback-window math (per period type, and the wider `backfill=True` window) — pure and unit-tested since the aggregation SQL itself can't be executed in this sandbox; structural checks on both period types' `MERGE` SQL (formulas, `NULLIF` guards, join conditions, bucketing expressions, the `_MISSING_LAST_UPLOAD_SENTINEL` exclusion, the per-pole `PoleTimeZones` join with its `ISNULL(...,'Eastern Standard Time')` fallback); dedicated `TimeZoneName`-propagation tests confirming it survives the `GROUP BY` for every period type; **`IsOnline`/`LightStatus`'s classification logic** (offline → `Working` not `Not Working`, unresolved daylight excluded not guessed, priority-ordered bucket aggregation — `Not Working` beats `Working` beats the `DayLight` default); **`TestDayRecentActivityWindow`** — confirms Day's "last 6 hours" window is computed via `DATEADD(HOUR, -6, ...)` relative to *that bucket's own end* and does **not** use an externally-bound `?` parameter — guarding against a real bug caught before shipping, where an earlier version computed this relative to "now" (the loader's execution time) instead, silently giving every already-completed historical bucket the wrong window every time it was recomputed — plus a regression test for a second bug only caught in production (SQLSTATE `42000`, `DATEADD(HOUR,...)` rejecting a plain `DATE` value), both an exact-match check and a looser structural one meant to catch the same class of mistake even if the exact expression changes later (this class used to also cover Week and Month, back when those period types existed — see the Notes section for why they were removed); **`TestLoadPoleVitalsPerPeriodTypeCommits`** — confirms `load_pole_vitals()` commits after each period type individually rather than once at the end (exact `execute`/`commit`/`rollback` ordering, tracked via `side_effect` call-order lists rather than `mock_calls`, which doesn't reliably propagate calls on this project's explicitly-named `mock_cursor`/`mock_conn` fixtures — confirmed directly before relying on it), that a benign SQLSTATE `01003` warning still commits (the underlying `MERGE` did succeed), that a genuine failure rolls back *and* doesn't block the other period type from still being attempted and committed, and specifically that an earlier period type's commit has already happened before a later one's failure — the actual property this change exists for; `_is_benign_null_aggregate_warning()` (SQLSTATE `01003` treated as success, `22007` and other genuine errors still treated as failures); full-flow tests (`SP_Execution` open/close, per-period-type failure isolation, rowcount aggregation) |
| `tests/test_function_app.py` | Timer trigger fires only at 6 AM/6 PM Eastern (verified across the DST boundary with freezegun), `past_due` handling, manual HTTP trigger's `Prod` block, synchronous (non-threaded) execution, **Poles runs before Projects runs before Customers** in both triggers, a failure in an earlier loader blocks the later ones, **`loadLeadsunData` runs unconditionally** (no hour-gating) otherwise, runs **Models → Telemetry → TimeZones → DaylightFlags → Vitals in order** (a failure in an earlier one blocks the later ones here too), and never touches the Airtable loaders — and **both timer triggers skip entirely when `ENVIRONMENT == "Dev"`**, before even checking `past_due`, while both manual triggers are unaffected by that guard; separately, **`getCustomers`** and **`getProjects`**'s array-vs-single-object-vs-404 response shaping, non-numeric `limit` → `400` without querying the DB at all, a query failure surfacing as `500` with a JSON error body rather than a raw exception, and **`getProjects?customerId=X`**'s distinct list-query semantics (empty array + `200`, not `404`, when a customer has no projects) vs. `projectId`'s single-object-or-404, including when both params are combined; **`getPoleVitals`** gets the same response-shaping/error-handling coverage, but with a genuinely different contract worth its own tests — unlike `getProjects`, `customerId` here is ALSO a single-entity lookup (404-on-not-found), not a collection filter (empty-array), since `get_pole_vitals()` returns `None` rather than `[]` for "doesn't exist" in either the `projectId` or `customerId` case; **`getPoles`** gets the same response-shaping/error-handling coverage too, but with yet another distinct contract — here `poleId` is the single-entity lookup (404-on-not-found), while `projectId`/`customerId` alone are back to being collection filters (empty array + `200`, matching `getProjects?customerId=X`'s convention, not `getPoleVitals`'s), and `poleId` combined with `projectId`/`customerId` is confirmed to pass every given param through together rather than one silently overriding another; **`?summary=`** — parsed case-insensitively (`"true"`/`"TRUE"`/`"1"` all treated as `True`, `"false"`/absent both `False`), and confirmed to actually flow through as a real keyword argument to `get_poles()` on every call, not just when truthy; **`getUsers`** follows `getCustomers`/`getProjects`' array-vs-single-object-vs-404 shaping and `customerId`-as-collection-filter convention (not `getPoleVitals`'s single-entity one), with `userId`+`customerId` combined confirmed to pass both through together and correctly 404 when a real user belongs to a different customer than the one specified |
| `tests/test_run_pole_vitals_backfill.py` | `refuse_if_prod()` (blocks `"Prod"`, allows everything else), `load_local_settings_into_env()` (missing file, missing `Values` key, doesn't clobber an already-set env var) |
| `tests/test_api_utils.py` | `json_safe()` (datetime/date/unknown-type coercion), `clamp_limit()` (default equals max, cap, negative) — extracted from `test_customers_api.py` once `getProjects` needed the exact same logic |
| `tests/test_customers_api.py` | `get_customers()` query shape (by-id vs. `TOP (?)`, `SP_ExecId` never selected, camelCase key mapping, connection cleanup on success and failure) |
| `tests/test_projects_api.py` | Same shape as `test_customers_api.py`, for `get_projects()` — including that a plain `DATE` column (`EffectiveDate`, not just `DATETIMEOFFSET`) also gets the `json_safe()` treatment — plus the `customerId` filter: alone (list query, `WHERE CustomerId = ?`, still respects `limit`, can return multiple rows) and combined with `projectId` (`WHERE Id = ? AND CustomerId = ?`) |
| `tests/test_pole_vitals_api.py` | `_working_percentage()`'s divide-by-zero guard (0 total lights → `0.0`, not a crash, not `None`) and rounding; SQL structural checks (`COUNT(*)` — not `COUNT(LightStatus)` — for `TotalLights`, so an unclassified pole still counts; every `LEFT JOIN`, confirming a pole with zero `Hour` `PoleVitals` rows in the recent window, a project with zero poles, and a customer with zero projects all still appear rather than being silently dropped by an `INNER JOIN`; the `Working`+`DayLight` grouping, the `Not Working`-only fault definition, and the separate `NoTelemetryCount` aggregate (`LightStatus IS NULL`) confirming an unclassified pole is counted on its own, folded into neither the fault count nor the working percentage; the `RecentPoleStats` CTE's rolling-window filter (`PeriodStart >= DATEADD(HOUR, -{hours_window}, SYSDATETIMEOFFSET())`, confirmed via the raw `{hours_window}` placeholder text so the SQL can't drift from `_RECENT_HOURS_WINDOW`, plus a separate test confirming it formats to the real constant) and its priority-based `LightStatus` aggregation (`MAX(CASE...)` per `LocationId`), replacing the earlier single-row `ROW_NUMBER()` approach entirely (`ROW_NUMBER()` confirmed absent from both templates); the Python-side grouping into nested `Customer → [Project]` (multiple projects correctly grouped under one customer, customer order preserved from the SQL's own `ORDER BY`, not reshuffled by dict grouping); the `customerId`-with-zero-projects case specifically — a real gap found by writing these tests, not a hypothetical: the original query used `INNER JOIN Projects`, meaning a customer with zero projects was indistinguishable from a nonexistent one, fixed by switching to `LEFT JOIN Projects` plus detecting the resulting all-NULL "phantom" project row in Python; `projectId`'s flat (not nested) single-object shape, including customer context merged onto it directly; **`_sum_pole_stats()`/`_customer_rollup_fields()`** — dedicated coverage for the customer-level rollup, most importantly confirming it's a genuine pole-weighted aggregate (sum of working ÷ sum of total across a customer's own projects) and not a naive average of each project's own percentage — a tiny project at 80% and a huge one at 100% must roll up close to 100%, not to a misleading 90%; confirms this rollup (including `totalNonTelemetryAvailable`) appears on the customer object in both the unfiltered and `customerId`-filtered cases, and deliberately does *not* leak onto the flat `projectId`-filtered single-project response; **`optimisticWorkingPercentage`** — confirms it treats each unclassified pole as working (numerator becomes `workingCount + noTelemetryCount`), differs from the conservative `workingPercentage` exactly when unclassified poles exist and matches it exactly when they don't, and is itself a pole-weighted aggregate at the customer level (summed before dividing, same principle as `workingPercentage`); **the per-project `"poles"` list** — the second, separate SQL query's structure (plain `INNER JOIN`s, no phantom-row handling needed, since a project with zero matching poles just yields zero rows and an empty list falls out naturally; its own `RecentPoleStats` additionally averaging the three `avg*Percentage` fields via `ROUND(AVG(...), 2)`, and casting `IsOnline`'s `MAX(CASE...)` explicitly to `BIT` — confirmed directly, since without that cast pyodbc would hand the aggregated value back as a plain Python `int`, serializing as `1`/`0` in JSON instead of `true`/`false`); `_pole_row_to_dict()`'s `lightStatus: null`/`isOnline: null`/all-three-percentages-`null` (not an invented string, not `False`, not `0`) for an unclassified pole, and that both queries use the *identical* rolling-window pattern, not a different/inconsistent definition of "current status" between them; that both queries are actually issued (`mock_cursor.execute.call_count == 2`) and reuse the *identical* `where_clause`/params (confirmed directly by comparing both calls' bound arguments, not assumed); and that poles from different projects never cross-contaminate when grouped, across all three filtering branches (unfiltered, `customerId`, `projectId`); **`installDate`/`lat`/`long`/`lastUpdate`/`batteryVoltage1`/`batteryVoltage2`** — SQL structural checks confirming `installDate`/`lat`/`long` are selected directly from `Poles` (not derived), and the `OUTER APPLY`'s exact shape (`SELECT TOP 1 ... ORDER BY LastUpload DESC`, correlated on `LocationId`, `OUTER` specifically rather than `CROSS`/`INNER`); `_pole_row_to_dict()` tests confirming all three `PoleTelemetry`-sourced fields are `null` (not `0` or a fabricated timestamp) for a pole with no matching row, while `installDate`/`lat`/`long` pass through independently of whether any telemetry or vitals data exists at all; **`CustomerId`** — confirms `c.Id AS CustomerId` is selected (the `Customers` join already existed for the `where_clause`'s own filtering, so this needed no new join), and that `_pole_row_to_dict()` still excludes both `ProjectId` and `CustomerId` from its own output even when both are given real, non-null values (an exact-dict-equality check, not just a coincidental absence from defaulting to `None`) — `poles_api.py` reads both straight from the row itself instead; **`_RECENT_POLE_STATS_CTE`** — a permanent byte-for-byte regression test confirming `_POLE_DETAILS_SQL_TEMPLATE`'s fully-assembled text is *identical* to what it was before this CTE was factored out into its own constant (not just "looks the same" after the refactor), plus a separate test confirming the template still actually embeds that shared constant — together guarding against `shared/poles_api.py`'s lighter summary query (added later, reusing this same CTE so its `LightStatus`/`IsOnline`/`avg*Percentage` classification logic can't drift out of sync with the original) silently diverging from what `getPoleVitals` and `getPoles`' full-detail mode both still rely on |
| `tests/test_poles_api.py` | `_pole_row_to_dict_with_parents()` (renamed from `_pole_row_to_dict_with_project()` once it started adding both parents, not just one) confirming it adds `projectId` *and* `customerId` on top of every field `pole_vitals_api._pole_row_to_dict()` already produces, without altering any of them, including for a fully unclassified pole; `get_poles()`'s four filtering branches — unfiltered (limit-based `TOP (?) Id FROM Poles ORDER BY PoleNumber` subquery, mirroring `pole_vitals_api.py`'s own "limit via a subquery" pattern even though there's no grouping concern to protect against here), `poleId` alone (single-object-or-404, matching `getCustomers`/`getProjects`'s convention), `projectId`/`customerId` alone (collection filters — empty array, not `404`/`None`, when nothing matches, matching `projects_api.get_projects()`'s `customerId` convention), and `projectId`+`customerId` combined; **`TestGetPolesCombinedWithPoleId`** — a real bug caught by writing these tests, not a hypothetical: the first version used an `if`/`elif` chain, so `poleId` combined with `projectId` silently ignored `projectId` entirely, directly contradicting the function's own docstring (which claims `poleId` "can be combined with `projectId` and/or `customerId` to also verify the pole belongs to that project/customer"); fixed by building the `WHERE` clause from whichever conditions were actually given, joined with `AND`, rather than a mutually-exclusive chain — covers `poleId`+`projectId`, `poleId`+`customerId`, and all three together, confirming each combination still returns a single dict (not a list) and correctly returns `None` for a real pole that belongs to a *different* project than the one specified; a full-flow test confirming `customerId` (like `projectId`) flows all the way through `get_poles()`'s unfiltered case onto each pole in the result, not just at the `_pole_row_to_dict_with_parents()` unit level; **summary mode** — `_clamp_summary_limit()` (defaults/caps against `_SUMMARY_MAX_LIMIT`, confirmed well above `api_utils.MAX_LIMIT`, which is the whole reason this separate ceiling exists); `_POLE_SUMMARY_SQL_TEMPLATE`'s structure (no `OUTER APPLY`, no `LastUpload`/`BatteryVoltage1`/`BatteryVoltage2` columns, embeds the shared `_RECENT_POLE_STATS_CTE` rather than a second copy, still selects `InstallDate`/`Lat`/`Long`/`CustomerId`); `_summary_row_to_dict()`'s field set (matches the full-detail shape minus exactly the three telemetry fields, `null`-not-`0` for an unclassified pole); and `get_poles(summary=True)`'s full-flow behavior — the unfiltered case switches to the lighter template and the higher limit ceiling, an explicit `limit` is still respected within that raised ceiling, and `summary=True` combined with `poleId`/`projectId` still switches which query and dict-mapping function is used, not just in the unfiltered case |
| `tests/test_users_api.py` | Structural checks on `get_users()`'s SQL: the `LEFT JOIN Customers` (not `INNER`, so a customer-less user like a Streetleaf Admin still appears, with `customerName` `NULL` rather than being dropped), and — a hard security requirement, not an incidental check — that `PasswordHash`/`ResetToken`/`ResetTokenExpiresAt` never appear in the `SELECT` list under any circumstance; camelCase field mapping (`_COLUMN_TO_JSON_KEY`), confirming PascalCase keys never leak through; `userId`/`customerId` filtering matching `customers_api.get_customers()`'s "always returns a list, 0 or 1 elements for an id lookup" contract; **`TestGetUsersCombinedFilter`** — confirms `userId`+`customerId` combine with `AND` from the start (built this way deliberately, avoiding the exact `if`/`elif`-silently-ignores-a-filter bug that was found and fixed in `poles_api.py` earlier, rather than repeating it and fixing it again later) |
| `tests/test_schema_integration.py` | Column-name consistency between the code's SQL and a documented expected schema (all eight loader-backed tables); two opt-in **real** end-to-end tests (Airtable+SQL, and separately Leadsun+SQL, the latter now covering Models → Telemetry → TimeZones → DaylightFlags → Vitals) |

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

  **This schedule has been through a widen-then-revert cycle, both
  changes for real reasons, not experimentation**. Originally 10 minutes.
  Widened to 30 minutes after a production finding: with `Week`/`Month`
  still part of `loadPoleVitals` at the time, this cycle typically took
  ~20-25 minutes end to end (`Week` alone was ~15+ minutes of that — see
  `pole_vitals_loader.py`'s own notes on the database contention/query-
  plan investigation that uncovered this) — a 10-minute schedule can't
  keep up with a 20-25 minute run. Once `Week`/`Month` were removed
  entirely (same notes), a normal cycle should comfortably fit back
  within 10 minutes, so the schedule was reverted to `10 * * * * *`.

  **`use_monitor` was turned off at the same time as the original
  widening, and stays off now regardless of the schedule interval** —
  this part isn't tied to the 10-vs-30-minute question. Azure Functions'
  Timer Trigger uses a Singleton Lock that already guarantees only one
  instance of this function runs at a time — a slow run was never at
  risk of a second, *overlapping* run starting. But `use_monitor=True`
  (the original setting) controls something different: it tracks missed
  scheduled occurrences and immediately re-invokes the function to
  "catch up" once it becomes free, rather than just waiting for the next
  natural tick. At the original 10-minute schedule against the ~20-25
  minute `Week`/`Month`-era run, that meant Azure was very likely
  detecting 1-2 missed occurrences on almost every cycle and immediately
  kicking off another run the moment the previous one finished —
  plausibly a real, ongoing contributor to sustained database CPU
  pressure at the time, independent of any single query's own cost.
  "Catching up" on a missed tick has no real value for this workload
  regardless of schedule interval: every loader's own lookback window
  (`_DEFAULT_LOOKBACK` in `pole_vitals_loader.py`, and similar bounds
  elsewhere) already covers everything since the last successful run
  regardless of how many scheduled ticks were skipped in between —
  there's nothing a "catch up" run would compute that the next natural
  one wouldn't anyway. Keeping `use_monitor=False` means that if some
  future cycle ever runs long for an unrelated reason, Azure just waits
  for the
  next natural tick rather than risking that same catch-up-flurry pattern
  again.

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

- **`PoleVitals` — rolling Hour/Day averages of pole health metrics,
  derived from `PoleTelemetry` + `PoleModels` + `PoleTimeZones`.** Unlike
  every other loader in this project, this one doesn't fetch from an
  external API — it runs a pure SQL aggregation against tables already
  loaded earlier in the same cycle (`load_pole_models()` →
  `load_pole_telemetry()` → `load_pole_timezones()` → `load_pole_vitals()`,
  in that order, since Vitals depends on all three being current).

  **`Week` and `Month` period types were removed entirely** (they used to
  exist alongside Hour/Day). The short version: `Week`'s join against the
  `Workweek` table (a `BETWEEN`-based range condition, not a simple
  equality) turned out to explode into a genuine row-multiplication bug
  — confirmed via a real query profile, roughly an 18× row-count blowup
  — which got properly diagnosed and fixed with a covering index. But
  even after that fix, and after scaling the database up (2 → 4 vCores),
  `Week` remained the dominant cost of every run by a wide margin — 15+
  minutes out of a ~20-25 minute total, against Day/Hour's combined
  under-a-minute — through a long live debugging session covering
  database CPU contention, `sys.dm_exec_requests` states
  (`runnable`/`suspended`/`running`), parallel-plan vs. serial-plan
  execution, and more. Rather than continue chasing diminishing returns
  on a period type whose cost never came down proportionally to the
  effort spent on it, the decision was made to stop computing `Week`/
  `Month` altogether. If a wider-than-Day rollup is wanted again later,
  the removed SQL templates and their tests are recoverable from git
  history (or ask to rebuild them) — nothing about the current Hour/Day
  design would need to change to reintroduce them. The `Workweek` table
  itself sat unused after that removal (nothing else in this project
  ever read from it) and has since been dropped entirely too, along with
  its generator script and tests — see "sql/Workweek/Drop tbl
  Workweek.sql" for that later, separate cleanup; reintroducing `Week`
  would mean rebuilding `Workweek` from scratch as well, not just the
  period type's own SQL.

  **Per-reading formulas** (computed row-by-row, then averaged within
  each bucket):
  ```
  BatteryPercentage = (BatteryElecCurrent1 + BatteryElecCurrent2) / 2
  PanelPercentage   = (SolarBoardVoltage * SolarBoardElecCurrent) / SunboardPower * 100
  LightPercentage   = (LampPower1 + LampPower2) / LightPower * 100
  ```
  `SunboardPower`/`LightPower` come from `PoleModels`, joined on
  `PoleTelemetry.ModelId = PoleModels.ModelId` — confirmed present in the
  real `/lamps` sample used to build `PoleTelemetry`, not a guess. A
  reading whose model can't be found (`LEFT JOIN`), or whose
  `SunboardPower`/`LightPower` is `0`, gets `NULL` for that specific
  percentage via `NULLIF(..., 0)` — `AVG()` ignores `NULL`s, so one bad
  reading doesn't skew or error out the rest of that bucket.

  **Time zone — per-pole, not one hardcoded zone for every pole.**
  Hour/Day bucket boundaries are computed in *each pole's own* local
  wall-clock time, resolved from that pole's own coordinates via
  `PoleTimeZones` (see below), rather than assuming every pole is in
  Eastern time regardless of where it actually is — the original design,
  before this was built out. Each period type's `TelemetryWithVitals` CTE
  now does `LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern
  Standard Time')`, `LEFT JOIN`ing `PoleTimeZones` on `LocationId` — a
  location `PoleTimeZones` doesn't have an entry for yet (or one whose
  timezone couldn't be resolved) falls back to Eastern, same as the
  original hardcoded behavior, rather than erroring.

  **`PoleTimeZones` — caches each pole's resolved timezone, from its own
  `PoleTelemetry` coordinates.** `timezonefinder` (a point-in-polygon
  lookup library, see `shared/timezone_utils.py`) is a Python library --
  there is no way to run that computation inside a T-SQL query, which is
  why timezone resolution is a separate cached table/loader rather than
  folded into `PoleVitals`' aggregation SQL directly. `load_pole_timezones()`
  only resolves `LocationId`s not already in `PoleTimeZones` -- poles are
  stationary, so a location's timezone never changes once resolved,
  making this a one-time-per-pole cost rather than something to redo
  every 10-minute cycle (unlike the staging-table-bulk-merge pattern used
  for Poles/PoleTelemetry, this loader is deliberately NOT batched that
  way, since it only ever processes brand-new locations each run --
  typically zero to a handful after the initial backfill).

  **A real gotcha, worth understanding if this ever needs touching**:
  `timezonefinder` returns IANA/Olson names (`"America/New_York"`), but
  SQL Server's `AT TIME ZONE` expects **Windows** timezone names
  (`"Eastern Standard Time"`) — these are two different naming systems,
  and using an IANA name directly in a T-SQL `AT TIME ZONE` clause would
  fail or behave unpredictably depending on the SQL Server build.
  `shared/timezone_utils.py`'s `IANA_TO_WINDOWS` dict maps between them,
  deliberately scoped to just the US + territories (matching this
  project's business context — Eastern-time scheduling, Brevard County/FL
  sample data, etc. — not a full global mapping of hundreds of zones). A
  coordinate resolving to an IANA zone outside that mapping gets stored
  with `WindowsTimeZone = NULL` (a logged warning, not a crash) and falls
  back to Eastern in `PoleVitals`, same as an unresolved location.

  **A second gotcha, found on the first real run**: `lat=0.0, lng=0.0` —
  "Null Island", where the equator meets the prime meridian — is a
  well-known placeholder value GPS hardware reports when it hasn't
  acquired a real fix yet, not a genuine location any pole would actually
  be at. `timezonefinder` resolves it (correctly, but unhelpfully) to
  `"Etc/GMT"`, which would otherwise show up as "needs a new
  `IANA_TO_WINDOWS` mapping" — the wrong fix for what's actually a
  data-quality issue (a device without a GPS fix, e.g. a gateway rather
  than an installed pole), not a missing mapping. `resolve_windows_timezone()`
  checks for this exact coordinate pair and skips resolution entirely,
  logging a message that says what it actually is rather than implying
  the mapping table needs extending.

  **A third gotcha, also found in real production data**: a `LocationId`
  reported `Longitude = -82533519.0` — `-82.533519 × 1,000,000`, which
  strongly suggests a device reporting GPS coordinates in
  **micro-degrees** (a common compact wire format) without converting
  back to plain decimal degrees before it reaches `PoleTelemetry`.
  `timezonefinder` raises `ValueError` outright for an out-of-range
  value like this, which — unlike Null Island — was **not** being caught
  anywhere, meaning that `LocationId` never got a row in `PoleTimeZones`
  at all and was silently retried (and re-failed, and re-logged) every
  single cycle, forever. `resolve_iana_timezone()` now validates
  latitude/longitude are within their real valid ranges (`-90..90`,
  `-180..180`) *before* calling `timezonefinder` at all, logging a clear
  explanation and returning `None` — which flows through to a stored
  `PoleTimeZones` row with `WindowsTimeZone = NULL`, same as Null Island,
  so it's resolved (as "unresolvable") exactly once, not every cycle.
  Deliberately does **not** attempt to auto-correct the value (e.g.
  guessing it's off by `1,000,000` and dividing) — a wrong guess would
  silently assign the wrong timezone, which is worse than failing safely
  and falling back to Eastern. There's also a defense-in-depth
  `try/except` around the actual `timezonefinder` call itself, for any
  *other* input it might reject that isn't anticipated by the explicit
  range check.

  **`PeriodEnd` is exclusive** (the start of the next period) for both
  period types — e.g. an Hour bucket's `PeriodEnd` is exactly
  `PeriodStart + 1 hour` — chosen since exclusive bounds are simpler for
  range queries (`WHERE ts >= PeriodStart AND ts <
  PeriodEnd`) at hour/day granularity.

  **Scale**: `PoleTelemetry` is retained for 6 months and could grow to
  millions of rows, so re-aggregating the *entire* history every 30
  minutes would eventually become a real performance problem — the exact
  trap already hit (and fixed) once for `Poles`/`PoleTelemetry` itself.
  Each normal run only recomputes **recent buckets** (current + previous,
  sized per period type — 3 hours for Hour, 2 days for Day — via
  `pole_vitals_loader._DEFAULT_LOOKBACK`), bounded by the existing
  `IX_PoleTelemetry_LastUpload` index, rather than scanning the full
  retention window. For the **initial backfill** of whatever telemetry
  already exists, run:
  ```bash
  python3 scripts/run_pole_vitals_backfill.py
  ```
  from the project root — this reuses `local.settings.json`'s values (the
  same file `func start` reads) to call `load_pole_vitals(backfill=True)`
  directly, no Azure Functions runtime needed. It widens the lookback to
  400 days (comfortably covering the full 6-month retention window) for
  every period type in that one call, and refuses to run if `ENVIRONMENT`
  resolves to `"Prod"` — same safety convention as this project's manual
  HTTP triggers and live integration tests. If your local machine can't
  reach the same Azure SQL Server (e.g. firewall rules only allow
  Azure-to-Azure traffic), run the same script instead from the deployed
  Function App's Kudu/SSH console (Advanced Tools in the Portal), where
  all the same values are already set as real App Settings.

  **Testing note**: both `MERGE` statements' *structure* (formulas,
  joins, bucketing expressions, `NULLIF` guards) is covered by
  `tests/test_pole_vitals_loader.py`, but the actual **aggregation
  arithmetic** can't be executed in this sandbox — there's no real SQL
  Server available to run a `MERGE`/`AT TIME ZONE`/`AVG()` query against.
  Worth spot-checking the first real run: pick one `LocationId`, manually
  average a handful of its `PoleTelemetry` rows for a given hour, and
  confirm it matches the corresponding `PoleVitals` row.

  **Two issues surfaced on the first real Azure run, both now fixed**:
  - **`SQLSTATE 22007` ("Adding a value to a 'date' column caused an
    overflow") on Day** (and, back when it existed, Month too) — caused
    by `PoleTelemetry`'s `_MISSING_LAST_UPLOAD_SENTINEL`
    (`9999-12-31 23:59:59.999 +00:00`, used for readings with a
    genuinely-missing `lastUpload`). That sentinel is always `>= cutoff`
    for any reasonable lookback window (it's deliberately far in the
    future, so retention never purges it), so it always passed the
    `WHERE t.LastUpload >= ?` filter. Bucketing it into a Day
    (`9999-12-31`) and then computing `PeriodEnd` as `+1 day` tried to
    produce a date in the year 10000, past `DATE`'s max value. Fixed by
    explicitly excluding the sentinel (`AND t.LastUpload <> ?`, imported
    directly from `pole_telemetry_loader._MISSING_LAST_UPLOAD_SENTINEL`
    — one source of truth, not a duplicated magic string) in both
    period types' `WHERE` clauses. Devices that have never reported a
    real timestamp legitimately have nothing to contribute to a
    time-bucketed average anyway.
  - **`SQLSTATE 01003` ("Warning: Null value is eliminated by an
    aggregate...") on Hour** — not actually an error. It's SQL Server's
    informational notice that `AVG()` skipped a `NULL`, which is exactly
    what the `NULLIF`-guarded Panel/Light formulas are *designed* to
    produce for a reading with a missing model or zero
    `SunboardPower`/`LightPower`. pyodbc still raises it as a Python
    exception, though, so without special-casing it a `MERGE` that
    actually completed successfully got logged and counted as a failure.
    `pole_vitals_loader._is_benign_null_aggregate_warning()` detects this
    specific SQLSTATE and treats it as a success (logged at `INFO`, not
    `ERROR`) — while a genuinely different SQLSTATE (like the `22007`
    above) still correctly counts as a real failure.

- **`Workweek`** — a static calendar reference table this project used to
  keep around for the `Week` period type (see the note under `PoleVitals`
  above). Once that period type was removed, `Workweek` had no remaining
  purpose and has since been dropped from the schema entirely, along with
  its generator script and tests — see "sql/Workweek/Drop tbl
  Workweek.sql" for the removal migration and its own comment for the
  full week-numbering convention this table used to follow, preserved
  there for anyone rebuilding it later.

- **`shared/sql_client.py`'s `get_connection()` registers a `DATETIMEOFFSET`
  output converter on every connection it returns.** Discovered via a real
  error from `getCustomers`'s first production run:
  `('ODBC SQL type -155 is not yet supported.  column-index=9  type=-155', 'HY106')`.
  ODBC type `-155` is `SQL_SS_TIMESTAMPOFFSET`, SQL Server's wire type for
  `DATETIMEOFFSET` — pyodbc has no built-in decoder for it, so any query
  that reads a `DATETIMEOFFSET` column back via `fetchall()`/`fetchone()`
  fails outright, unless a converter function is explicitly registered.
  This hadn't come up before `getCustomers` because every other loader in
  this project only ever *writes* `DATETIMEOFFSET` values (as bound `?`
  parameters going into `INSERT`/`MERGE`) — `getCustomers` was the first
  plain `SELECT` that actually reads one back into Python. It also
  couldn't have been caught by this project's test suite as it stood,
  since every test mocks the cursor entirely — this specific real-pyodbc
  decoding behavior was never actually exercised until it hit a live SQL
  Server. `tests/test_sql_client.py`'s decoder tests build the exact wire
  bytes by hand (round-tripped through the same struct format SQL
  Server's ODBC driver uses) specifically to close that gap without
  needing a real database.

  **One easy mistake worth flagging explicitly, since I made it myself
  while building this fix**: `add_output_converter` is a **method on the
  `Connection` object** (`conn.add_output_converter(-155, fn)`), not a
  module-level `pyodbc.add_output_converter(...)` setting — the latter
  doesn't exist and raises `AttributeError` immediately on import if
  called that way. Because it's per-connection, it has to be registered
  inside `get_connection()` on the connection object being returned, not
  once somewhere at module import time.

- **`getCustomers` / `getProjects` — read-only HTTP API endpoints, not
  part of the Airtable/Leadsun ETL pipeline at all.** Meant to be
  imported into Azure API Management and called by a website, not run on
  a schedule — so unlike every other function in this project, neither
  has a timer trigger, `SP_Execution` tracking (they don't load or sync
  anything, just serve what's already been loaded), or an
  `ENVIRONMENT == "Dev"` skip. `getProjects` is built identically to
  `getCustomers` in every respect below — everything said about one
  applies equally to the other unless noted.

  **`shared/api_utils.py`** holds the logic genuinely shared between
  them (`json_safe()`, `clamp_limit()`, `DEFAULT_LIMIT`/`MAX_LIMIT`) —
  extracted out once `getProjects` needed the exact same behavior
  `getCustomers` already had, rather than duplicating it a second time.
  `shared/customers_api.py` and `shared/projects_api.py` each keep only
  what's genuinely table-specific: their own `_COLUMN_TO_JSON_KEY`
  mapping and their own `get_<table>()` query function.

  **Security boundary, worth understanding before wiring either up**:
  neither endpoint enforces any row-level access control. Neither will
  automatically restrict a caller to one customer's data just because (if
  you're using the `Users` table from elsewhere in this project) a
  `Customer Admin`'s `Role`/`CustomerId` implies they should only see
  their own customer's data — each returns whatever `customerId`/
  `projectId` is asked for, no questions asked. If per-user scoping is
  needed, it has to happen **either**:
  - in an **API Management policy** — e.g. validate a JWT, extract the
    caller's `CustomerId` claim, and rewrite/restrict the
    `customerId`/`projectId` query param before the request ever reaches
    the function, or
  - in the **calling website** itself, before it ever sends the request.

  Neither function has any visibility into who's actually calling it
  beyond whether they have a valid function key. I built the filtering
  *mechanism* (an `Id` param) since scoping needs *something* to filter
  on, but deliberately didn't guess at *who's allowed to use it*, since
  that's a real security decision I don't have enough context to make
  silently.

  **`auth_level=FUNCTION`, not `ANONYMOUS`**: API Management would call
  these with the function key attached (as a named value / backend
  credential in its policy), so the Function App itself still isn't
  reachable by anyone who doesn't go through APIM or doesn't have the
  key. `ANONYMOUS` would only be safe here if the Function App were also
  network-isolated so APIM is the sole path to it (e.g. via a Private
  Endpoint) — absent that, `FUNCTION` is the safer default.

  **Response shape**: JSON, camelCase keys rather than the PascalCase SQL
  column names directly — typical REST/JS API convention, since these
  are meant for a website to consume.
  - `getCustomers`: `id`, `name`, `projectNames`, `address`, `city`,
    `state`, `zip`, `phone`, `createdAt`.
  - `getProjects`: `id`, `name`, `poleNumbers`, `poleIds`, `customerId`,
    `polesUnderContract`, `effectiveDate`, `installDates`, `createdAt`.
    `effectiveDate` is a plain `DATE` column, not `DATETIMEOFFSET` like
    the others — `json_safe()` handles both the same way (neither is
    natively JSON-serializable via `json.dumps()`), so this needed no
    special-casing.

  `SP_ExecId` is deliberately excluded from both responses — internal
  ETL batch-tracking metadata that a consuming website has no reason to
  see.
  - No filter param given → returns a JSON **array** of up to `limit`
    rows (default 1000 — i.e. everything up to the ceiling, not an
    arbitrarily lower default; `getCustomers` originally defaulted to
    100 and that turned out to be too conservative the moment a real
    customer roster exceeded it in production — fixed once, in
    `api_utils.py`, so `getProjects` starts from the corrected default
    rather than repeating that mistake), ordered by name.
  - `getCustomers?customerId=X` and `getProjects?projectId=X` → single
    JSON **object** (not wrapped in an array), or `404` with a JSON
    error body if that Id doesn't exist.
  - `getProjects?customerId=X` (without `projectId`) is the one
    exception to that — see the dedicated note further down. It's a
    list query (array, `200`, possibly empty), not single-object-or-404.
  - A non-numeric `limit` → `400`, without touching the database at all.
  - A query failure → `500` with a JSON error body, not a raw stack
    trace or an unhandled exception.

  **Getting these into API Management** (brief, since it's a standard
  Azure Portal flow, not something this project's code handles): in the
  API Management instance, **APIs → Add API → Function App**, pick this
  Function App, and it'll offer to import `getCustomers`/`getProjects`
  (and every other HTTP-triggered function here) as operations
  automatically, wiring up the function key as a backend credential.
  From there, APIM policies (rate limiting, JWT validation, CORS,
  request/response transformation) layer on top without touching this
  code at all — CORS in particular is usually handled at the APIM layer
  for exactly this kind of website-calls-an-API setup, not in the
  Function itself.

  **`getProjects?customerId=X`** — filters to every project belonging to
  that customer, a genuine list query (0, some, or all of that customer's
  projects, still subject to `limit`), **not** single-object-or-404
  semantics — an empty array (`200`) means "this customer has no
  projects", which is a valid state, not an error. This is the one place
  `getProjects` isn't a pure mirror of `getCustomers`: it's the only
  cross-table filter currently in either endpoint. `customerId` can also
  be combined *with* `projectId` (`?projectId=X&customerId=Y`) to verify
  a specific project actually belongs to a given customer — in that
  combination, `projectId`'s single-object-or-404 semantics take over
  (a project that exists but belongs to a *different* customer still
  correctly comes back as `404`, same as a genuinely nonexistent Id).

  **Sort order also deliberately differs here**: the `customerId`-filtered
  list sorts by `EffectiveDate` **descending** (newest first) — this is a
  genuinely different `ORDER BY` from the unfiltered "all projects" list,
  which still sorts by `Name` (`tests/test_projects_api.py` locks in that
  distinction explicitly, so a future edit can't accidentally apply one
  sort to both paths without a test failing). `EffectiveDate` is
  nullable, and the direction affects where those land: SQL Server's
  default `NULL` handling sorts `NULL`s *last* for `DESC` (it was *first*
  when this was briefly `ASC`), so a project with no `EffectiveDate` set
  yet now appears at the bottom of the list, behind every project with a
  known date, rather than ahead of them.

  This filter also benefits from an existing index —
  `IX_Projects_CustomerId` was already there, so `WHERE CustomerId = ?`
  gets an index seek rather than a table scan, independent of anything
  added for this endpoint.

  **Extending this pattern to another table** later (e.g. `getPoles`) is
  mechanical: a new `shared/<table>_api.py` importing `json_safe`/
  `clamp_limit` from `shared/api_utils.py`, with its own
  `_COLUMN_TO_JSON_KEY` and `get_<table>()` following
  `projects_api.py`'s shape, plus a matching `@app.route(...)` wrapper in
  `function_app.py` — no need to ask for this again if the need comes
  up.

- **`getPoleVitals` — a genuinely different shape from `getCustomers`/
  `getProjects`**: not a straight table read, a `Customer → [Project]`
  rollup of pole-health stats (`totalLights`, `workingPercentage`,
  `totalFaults` per project), computed from `Poles` joined against each
  pole's own `PoleVitals` rows from the last `_RECENT_HOURS_WINDOW`
  hours (currently 6), not just its single most recent row.

  **Three business-rule choices this is built on, all explicit
  requirements, not guesses**:
  - **"Working" means `LightStatus IN ('Working', 'DayLight')`** —
    `DayLight` isn't a third, separate state here; it's folded into
    "working" because a light not being lit during the day isn't a
    fault, it's expected behavior. Only `'Not Working'` counts as a
    genuine fault.
  - **Which `PoleVitals` period type drives the classification: `Hour`**
    — the finest-grained signal available, matching the whole "live
    status" framing `IsOnline`/`LightStatus` were originally built for
    (see `pole_vitals_loader.py`'s own notes). This is a single named
    constant (`_STATUS_PERIOD_TYPE` in `pole_vitals_api.py`), not buried
    in the SQL string, specifically so it's easy to find and change if a
    different period type ever turns out to be the better fit.
  - **How far back to roll those Hour rows up: `_RECENT_HOURS_WINDOW`,
    currently 6 hours** — not just the single most recent Hour bucket.
    A lone Hour bucket is a noisier signal than it might seem: one
    transient blip (a brief connectivity drop, a momentary sensor glitch)
    can otherwise swing a pole's reported status on its own. Rolling up
    several hours' worth of buckets, using the same priority-based
    aggregation `PoleVitals`' own bucket-level aggregation already uses
    (see the next paragraph), gives a steadier "how's this pole actually
    doing" signal than any single bucket alone. Also a single named
    constant, easy to change if a different window turns out better.

  **"Total lights" comes from `Poles`, not from `PoleVitals`** — a pole
  that's installed but hasn't had any telemetry processed yet is still a
  real light, and would be invisible if this counted `PoleVitals` rows
  instead. A pole with zero `Hour` `PoleVitals` rows in the recent window
  counts toward `totalLights`, but not toward `workingPercentage`'s
  numerator or `totalFaults` — it's counted on its own, in
  **`totalNonTelemetryAvailable`** (its own explicit `SUM(CASE WHEN
  LightStatus IS NULL THEN 1 ELSE 0 END)`, not something silently folded
  into another field, or left as an invisible gap between `totalLights`
  and `workingCount + totalFaults` the way it was before this field
  existed). "We don't have data on this one yet" is a genuinely
  different state from "confirmed broken", and deserves its own visible
  count rather than quietly deflating the working percentage with no way
  to tell why. Concretely: `COUNT(*)` (not `COUNT(LightStatus)`) for the
  total, and three separate `SUM(CASE WHEN LightStatus = ...)`
  expressions for the three possible classified/unclassified outcomes,
  over a `LEFT JOIN` from `Poles` to each pole's rolled-up `RecentPoleStats`
  row (rather than an `INNER JOIN`, which would silently drop any
  unclassified pole from `totalLights` entirely) — `RecentPoleStats`
  itself aggregates every `Hour` row within the window per `LocationId`
  (`GROUP BY LocationId`), not a single most-recent row picked via
  `ROW_NUMBER()` the way this used to work before the window was
  introduced.

  **`RecentPoleStats` rolls `LightStatus` up across the window using the
  exact same priority logic `PoleVitals`' own bucket-level aggregation
  already uses** (see `pole_vitals_loader.py`'s module docstring): if
  *any* `Hour` row in the window is `'Not Working'`, the whole window is
  `'Not Working'` — a single confirmed fault within the last 6 hours
  shouldn't get averaged away by several otherwise-fine hours. Otherwise,
  if *any* row is `'Working'`, the window is `'Working'`; only if
  neither ever occurred does it fall back to `'DayLight'`. A row with
  `LightStatus IS NULL` (that particular hour's daylight status was
  unresolved) doesn't count toward either `MAX()` check, matching the
  same "excluded, not guessed" treatment already used at the
  single-bucket level. This is a genuinely different signal than "no
  data at all": a pole with real `Hour` rows in the window, all of them
  `NULL`, still resolves to `'DayLight'` (a real, if uninformative,
  classification) — only a pole with *zero* `Hour` rows in the window at
  all (the `LEFT JOIN` producing no match) ends up `NULL`/unclassified.

  **`optimisticWorkingPercentage` is the same percentage, computed as if
  every unclassified pole WERE working** — `(workingCount +
  noTelemetryCount) / totalLights`, alongside `workingPercentage`'s more
  conservative reading (which excludes unclassified poles from the
  numerator entirely, only counting confirmed-working ones). The two
  are identical whenever a project/customer has zero unclassified
  poles, and diverge exactly in proportion to how many poles are still
  unclassified — giving a best-case-vs-worst-case pair rather than
  forcing a single assumption about poles with no data yet.

  **The Customer itself carries the same five rollup fields**
  (`totalLights`/`workingPercentage`/`optimisticWorkingPercentage`/
  `totalFaults`/`totalNonTelemetryAvailable`), summed across all of
  that customer's own projects — in both the unfiltered list and the
  `customerId`-filtered single-customer response. **Deliberately a true
  pole-weighted aggregate for both percentage fields** (`sum(workingCount)
  / sum(totalLights)`, or `sum(workingCount + noTelemetryCount) /
  sum(totalLights)` for the optimistic one, across every project,
  computed in `_customer_rollup_fields()`/`_sum_pole_stats()`), **not an
  average of each project's own percentage** — averaging percentages
  would give a 2-pole project and a
  2,000-pole project equal weight, which would badly misrepresent a
  customer's actual overall pole health. Concretely: a customer with a
  10-light project at 80% working and a 90-light project at 100% working
  rolls up to 98% (98 working out of 100 total), not the naive-average
  90%. This is computed in Python from the raw per-project rows (the
  same rows already being grouped into the nested `projects` list), not
  as a separate SQL aggregation — no new query needed, just summing
  columns already being fetched. The `projectId`-filtered flat response
  deliberately does **not** carry its project's customer's rollup
  totals — that's a single-project view, and pulling in the parent
  customer's aggregate would blur what the response is actually about.

  **A real gap, found by writing the tests for this, not a hypothetical
  edge case**: a project with zero poles was already handled (`LEFT JOIN
  ProjectAgg`, so it still appears with everything at `0`) — but the
  *customer* level wasn't originally handled the same way. The first
  version used `JOIN Projects proj ON proj.CustomerId = c.Id` (an inner
  join), which meant a customer with zero projects produced zero rows
  entirely — indistinguishable from a customer that doesn't exist at
  all. Writing a test for "customer exists, has no projects yet"
  surfaced this immediately. Fixed by switching to `LEFT JOIN Projects`
  and detecting the resulting all-NULL "phantom" project row on the
  Python side (`row[2] is None`, `ProjectId`) — that customer still gets
  a real entry, just with `"projects": []` and its own rollup fields all
  at `0`/`0.0`, distinct from `None` (customer genuinely not found).

  **Each project also carries a `"poles"` list** — one entry per Pole
  belonging to that project (`id`, `poleNumber`, `locationId`,
  `lightStatus`, `isOnline`, `avgBatteryPercentage`, `avgPanelPercentage`,
  `avgLightPercentage`), so a consumer can see *which specific* poles are
  driving a project's fault count, not just the aggregate number, and
  what each pole's underlying metrics actually look like. `lightStatus`
  and `isOnline` are rolled up across the same `_RECENT_HOURS_WINDOW`
  window as the project/customer-level counts (see above) — `isOnline`
  as `CAST(MAX(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END) AS BIT)` ("was
  any reading in the window online"), matching `PoleVitals`' own
  bucket-level `IsOnline` semantics exactly. The three `avg*Percentage`
  fields are a plain average (`ROUND(AVG(...), 2)`) of that same
  window's `Hour` rows' own `AvgBatteryPercentage`/`AvgPanelPercentage`/
  `AvgLightPercentage` columns — an average of averages, which is a
  reasonable simplification here since every `Hour` bucket already
  represents a comparable amount of underlying telemetry (nothing
  further needs to be pole-weighted the way the project/customer-level
  `workingPercentage` rollup does, since this is a single pole's own
  data, not a mix of differently-sized populations). All five fields are
  a plain `null` for an unclassified pole (zero `Hour` rows in the
  window) — deliberately not an invented string like `"No Telemetry"`
  for `lightStatus`, `false` for `isOnline`, or `0` for the
  percentages, since none of those is a real, confirmed value in that
  case — `null` more accurately mirrors what's actually in the database:
  nothing, not a known-bad status, a known-offline device, or a
  confirmed-zero reading.

  **The `CAST(...AS BIT)` on `IsOnline` matters, not decorative**:
  `MAX(CASE WHEN IsOnline = 1 THEN 1 ELSE 0 END)` produces a plain `INT`
  (`0`/`1`), and without the explicit cast, pyodbc would hand that back
  as a Python `int` rather than a `bool` — meaning `isOnline` would
  serialize as `1`/`0` in the JSON response instead of `true`/`false`,
  a real, easy-to-miss regression from reading a genuine `BIT` column
  directly (which pyodbc already converts to Python `bool` natively).

  **Six more fields, from two more sources beyond `PoleVitals`**:
  `installDate`/`lat`/`long` come straight from `Poles` — static,
  install-time facts, unrelated to any telemetry or vitals data, present
  even for a pole with neither. `lastUpdate`/`batteryVoltage1`/
  `batteryVoltage2` come from that pole's own single most recent
  `PoleTelemetry` row — the **raw reading itself**, genuinely different
  from `avgBatteryPercentage` just above, which is `PoleVitals`' own
  aggregate of a *different* pair of `PoleTelemetry` columns
  (`BatteryElecCurrent1`/`BatteryElecCurrent2`, not
  `BatteryVoltage1`/`BatteryVoltage2` — both pairs are real, separate
  columns on that table). Fetched via `OUTER APPLY` (not a plain `JOIN`
  or another CTE) — `PoleTelemetry`'s own primary key is `(LocationId,
  LastUpload)`, `LocationId` leading, so `SELECT TOP 1 ... WHERE
  LocationId = @x ORDER BY LastUpload DESC` seeks directly into that one
  pole's own rows in the clustered index rather than scanning the table;
  driving that per-pole from `Poles` (a small, bounded table, unlike
  `PoleTelemetry`'s own multi-million-row scale) is the natural way to
  express "correlated per-row top-1 lookup" in T-SQL. `OUTER`, not
  `CROSS`: a pole with no `LocationId`, or one with zero matching
  `PoleTelemetry` rows, must still appear in the result (with these three
  columns `null`) rather than being dropped — same "still appears, just
  unclassified" philosophy used throughout this endpoint.

  **This is a genuinely separate SQL query** (`_POLE_DETAILS_SQL_TEMPLATE`),
  not merged into the existing aggregate query — mixing per-pole detail
  rows and per-project aggregate rows into one T-SQL result set is
  awkward without `FOR JSON`/`STRING_AGG` tricks that would complicate
  the already-tested aggregate query for no real benefit. It reuses the
  *exact same* `where_clause` text and bound parameters the aggregate
  query already builds for whichever of the three filtering branches is
  active (unfiltered/`customerId`/`projectId`) — both queries alias
  `Projects` as `proj` and `Customers` as `c`, so the same `WHERE proj.Id
  = ?` / `WHERE c.Id = ?` text works unchanged against either query's
  `FROM`/`JOIN` structure, keeping both queries scoped identically
  without duplicating the branching logic. Plain `INNER JOIN`s
  throughout, unlike the aggregate query's `LEFT JOIN`s — no phantom-row
  handling is needed here specifically, since a project or customer with
  zero matching poles simply returns zero rows for *this* query, and an
  empty `"poles"` list falls out naturally when the results are grouped
  by `ProjectId` in Python (a project that isn't a key in the resulting
  dict just gets `[]` when looked up) — the aggregate query already
  handles reporting `totalLights: 0` correctly for that same case.

  **Single-entity lookups are flat-object-or-404, matching `getCustomers`/
  `getProjects`'s convention** — but note the contract is different from
  `getProjects` specifically: there, `customerId` alone is a *collection*
  filter (empty array, not `404`, for a customer with no projects).
  Here, `customerId` alone is a *single-entity* lookup (`404` if that
  customer doesn't exist at all) since the whole point is returning one
  customer's nested project list, not a flat list of projects.
  `projectId` returns a single **flat** dict (customer context —
  `customerId`/`customerName` — merged directly onto it, not nested)
  rather than a project wrapped inside its customer, since a single-
  project lookup has no real use for the wrapping. `projectId` combined
  with `customerId` verifies the project actually belongs to that
  customer, same as `projects_api.get_projects()`.

  **`limit` applies to customers, the top-level entity in the
  unfiltered case** — not to individual project or pole counts. Can't
  just `TOP (?)` the raw per-project query directly, since that would
  risk truncating one customer's *projects* partway through rather than
  dropping whole customers; instead the limit is applied via a subquery
  selecting the first N customer Ids first (`WHERE c.Id IN (SELECT TOP
  (?) Id FROM Customers ORDER BY Name)`), and every project for each of
  those customers is still returned in full.

- **`getPoles` — a flat listing, explicitly built to give each pole the
  exact same fields it already carries inside `getPoleVitals`'s
  `"poles"` list**, plus two additions beyond that literal field set:
  `projectId` and `customerId`, since a flat, unfiltered pole list
  otherwise has no way to trace a given pole back to its project or
  customer the way nesting under both already does in `getPoleVitals`.

  **`customerId` was added after the fact, reusing a `JOIN` that was
  already there** — `_POLE_DETAILS_SQL_TEMPLATE` already joins
  `Customers c` (needed for the `where_clause`'s own `c.Id = ?`
  filtering), it just wasn't selecting `c.Id` as an output column yet.
  Added as `c.Id AS CustomerId`, appended at the very *end* of the
  `SELECT` list rather than inserted alongside `ProjectId` near the top
  — deliberately, to avoid shifting every other column's position and
  forcing an update to every existing row-shape assumption across both
  `pole_vitals_api.py` and `poles_api.py`'s tests. `_pole_row_to_dict()`
  discards this new trailing column the same way it already discarded
  `ProjectId` (the leading column) — `getPoleVitals`'s own nested output
  is unchanged by this addition, confirmed directly by re-running its
  existing "poles" list against the wider row shape. `poles_api.py`'s
  `_pole_row_to_dict_with_parents()` (renamed from
  `_pole_row_to_dict_with_project`, since it now adds both parents, not
  just one) reads `row[-1]` for it directly, the same way it already
  read `row[0]` for `projectId`.

  **Deliberately reuses `pole_vitals_api.py`'s own SQL and field-mapping
  directly** (`_POLE_DETAILS_SQL_TEMPLATE`, `_pole_row_to_dict()`,
  `_STATUS_PERIOD_TYPE`, `_RECENT_HOURS_WINDOW`), rather than a second,
  independently-maintained copy of the same query — reaching into
  `pole_vitals_api.py`'s leading-underscore ("private") names on
  purpose, not by accident: this project treats "two copies of the same
  SQL slowly drifting apart" as the bigger real risk for code this
  tightly coupled. If `getPoleVitals`'s per-pole shape ever changes
  later (a new field, a different rollup window), `getPoles`'s shape
  changes with it automatically, which is the explicit intent — a
  consequence worth knowing about if either module is touched later.

  **Query params, matching `getProjects`'s established contract as
  closely as the shape allows**: `poleId` is the single-entity lookup
  (a real, single pole — or `404` if not found), while `projectId`/
  `customerId` alone are collection filters (an array, empty but `200`
  if nothing matches — matching `getProjects?customerId=X`'s convention,
  *not* `getPoleVitals`'s, where `customerId` alone is itself a
  single-entity lookup because it addresses a whole nested customer
  object). `projectId` and `customerId` can combine (verifies the
  project belongs to that customer, same as `projects_api.get_projects()`),
  and either or both can *also* combine with `poleId` (verifies the pole
  belongs to that project/customer).

  **A real bug, found by writing the tests for this, not a hypothetical
  edge case**: the first version built the `WHERE` clause with an
  `if`/`elif` chain (`poleId` first, then `projectId`+`customerId`, then
  `projectId` alone, then `customerId` alone) — meaning `poleId` combined
  with `projectId` silently used *only* `poleId` for filtering,
  discarding `projectId` entirely, directly contradicting this
  function's own docstring (which explicitly claims `poleId` "can be
  combined with `projectId` and/or `customerId` to also verify the pole
  belongs to that project/customer"). Writing a test for exactly that
  combination surfaced the mismatch immediately. Fixed by building the
  `WHERE` clause from whichever of the three conditions were actually
  given, joined with `AND`, rather than a mutually-exclusive chain — so
  `poleId`+`projectId` together now correctly means "this pole, AND
  verify it belongs to this project" (returning `None`/`404` if it
  belongs to a *different* one), not "`poleId` wins, `projectId` is
  quietly ignored."

  **`limit` applies to poles, the top-level entity in the unfiltered
  case** — via a subquery on `Poles`' own `Id`
  (`WHERE p.Id IN (SELECT TOP (?) Id FROM Poles ORDER BY PoleNumber)`),
  mirroring the exact same "limit via a subquery, not a bare `TOP()` on
  the joined query" pattern `pole_vitals_api.py` uses for its own
  unfiltered case — even though there's no grouping concern to actually
  protect against here (each pole is exactly one row in this query,
  unlike Customer→Project). Kept consistent with that pattern anyway,
  rather than simplified to a bare `TOP()`, so this doesn't silently
  become wrong if the query's shape ever changes later to produce more
  than one row per pole again.

  **`?summary=true` — a deliberately lighter mode, built for a "give me
  every pole" consumer** (e.g. a map rendering all ~14K poles at once,
  which is the concrete use case this was built for) rather than a
  paginated table. The motivating concern: the full-detail query's
  `OUTER APPLY` (each pole's single most recent `PoleTelemetry` row —
  `lastUpdate`/`batteryVoltage1`/`batteryVoltage2`) runs once *per pole*.
  Each individual seek is cheap (`PoleTelemetry`'s own clustered index is
  `(LocationId, LastUpload)`, `LocationId` leading, so it's a direct
  index seek, not a scan) — but doing that ~14,000 times in a single
  query execution is a real, structural cost a map-style consumer
  doesn't actually need to pay, since a map pin needs a location and a
  status to plot and color, not the raw telemetry reading behind it.
  `summary=true` uses a separate, lighter query
  (`_POLE_SUMMARY_SQL_TEMPLATE` in `shared/poles_api.py`) that drops the
  `OUTER APPLY` entirely — `lastUpdate`/`batteryVoltage1`/
  `batteryVoltage2` are simply absent from each pole in the result,
  everything else (`lightStatus`, `isOnline`, the three
  `avg*Percentage` fields, `installDate`/`lat`/`long`, `projectId`/
  `customerId`) stays exactly the same, since none of that requires the
  expensive per-row lookup — `lightStatus`/`isOnline`/the percentages
  all come from one aggregation pass over `PoleVitals`
  (`RecentPoleStats`), not a per-pole correlated subquery. A consumer
  that wants the telemetry detail for one specific pole (e.g. after a
  user clicks a pin on the map) can still get it cheaply via
  `?poleId=X` — that always uses the full query regardless of
  `summary`, paying the per-row cost for exactly one row, not
  thousands.

  **`_RECENT_POLE_STATS_CTE` was factored out of
  `pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE` specifically to make this
  possible** — rather than a second, independently-maintained copy of
  the same `CASE`/`MAX()` classification logic that could quietly drift
  out of sync with the original over time, `_POLE_SUMMARY_SQL_TEMPLATE`
  embeds the *exact same* CTE text. Confirmed the refactor didn't change
  any existing behavior by asserting the reassembled
  `_POLE_DETAILS_SQL_TEMPLATE` is byte-for-byte identical to what it was
  before the extraction, not just "looks the same" — a real risk worth
  guarding against directly, given this template is shared by both
  `getPoleVitals` and `getPoles` already.

  **Summary mode also raises the unfiltered case's limit ceiling** —
  `_SUMMARY_MAX_LIMIT` (20,000) instead of `api_utils.MAX_LIMIT` (1,000)
  — since the whole point of this mode is making "every pole in one
  call" practical at the current pole count (~14K) and comfortable room
  to grow. An explicit `limit` is still respected and still capped, just
  against this higher ceiling rather than the default one.

- **`getUsers` — named plural to match every other read endpoint here
  (`getCustomers`/`getProjects`/`getPoles`), rather than the singular
  originally asked for** — easy to rename back if a singular name was
  actually wanted. Structurally the simplest of the read endpoints:
  closer to `customers_api.py`/`projects_api.py`'s shape (a single
  table, one light join, no CTEs or per-pole correlated lookups) than
  `pole_vitals_api.py`/`poles_api.py`'s machinery.

  **Fields**: `id`, `name`, `email`, `role`, `status`, `customerId`, and
  `customerName` (via `LEFT JOIN Customers` — `LEFT`, not `INNER`,
  since `Users.CustomerId` is nullable: a "Streetleaf Admin" isn't
  scoped to one customer, per that column's own notes in
  `sql/Users/Create tbl Users.sql`, so that user must still appear here,
  just with `customerName` `null`, rather than being dropped). This
  join was already sketched out as a ready-to-run scratch query in
  `sql/Users/Select tbl Users joined with Customers.sql` — `get_users()`
  follows that exact join shape.

  **`PasswordHash`, `ResetToken`, and `ResetTokenExpiresAt` are
  deliberately excluded** — authentication-sensitive fields that have no
  place in a read-only API response, regardless of who's asking or how
  convenient it might seem to include them. This is a hard line in
  `shared/users_api.py`'s own `_COLUMN_TO_JSON_KEY` list, not a default
  that happens to currently be unset — a test asserts directly that none
  of the three ever appear in the generated SQL's `SELECT` list.

  **Query params, matching `getCustomers`/`getProjects`' established
  contract**: `userId` is the single-entity lookup (a real, single user
  — or `404` if not found), `customerId` alone is a collection filter
  (an array, empty but `200` if that customer has no users — matching
  `getProjects`/`getPoles`' `customerId` convention, not
  `getPoleVitals`'s different one), and the two can combine (verifies
  the user actually belongs to that customer). Built as an `AND`
  combination from the very first version, not an `if`/`elif` chain —
  deliberately avoiding, by construction, the exact bug that was found
  and fixed in `poles_api.py` earlier (where `poleId` combined with
  `projectId` silently ignored `projectId` until that was caught by a
  test and fixed). No reason to make the same mistake twice in a second
  module when the fix from the first one was already known.

- **`shared/daylight_utils.py`'s `is_daylight(dt, latitude, longitude,
  use_civil_twilight=False)`** — answers "is the sun up at this exact
  location and moment". Used by `shared/pole_daylight_flags_loader.py`
  (see below) to compute `PoleTelemetry.IsDaylight`, which in turn feeds
  `PoleVitals.LightStatus`.

  **Built on solar elevation angle, not a `sunrise()`/`sunset()` boundary
  lookup** — `astral.sun.elevation()` is well-defined for every
  moment/location, including places with literal polar day/night, which
  matters here specifically because `shared/timezone_utils.py`'s
  `IANA_TO_WINDOWS` mapping already covers Alaska, and Alaska has real
  locations (e.g. Utqiagvik/Barrow, ~71°N) with literal midnight sun and
  polar night. `astral.sun.sunrise()`/`sunset()` raise `ValueError`
  outright on a day where the sun never rises or never sets ("Sun is
  always above/below the horizon on this day") — confirmed directly
  against that real coordinate while building this — which would have
  meant handling that failure mode explicitly for every caller.
  `elevation()` sidesteps it entirely: "is the sun above or below the
  horizon right now" is always answerable, even on a day when "when does
  it rise/set" genuinely has no answer.

  **Thresholds are the standard astronomical ones**, not arbitrary round
  numbers: sunrise/sunset is elevation > **-0.833°** (not 0° — the usual
  textbook adjustment for atmospheric refraction plus the sun's own
  apparent radius), and civil twilight (`use_civil_twilight=True`) is >
  **-6°**. **Open item, flagged rather than glossed over**: checking the
  actual elevation angle at `astral`'s own computed sunrise/sunset
  moments for a real project coordinate gave values around -0.37° to
  -0.65°, not a tight match to -0.833° — close enough that boundary cases
  are misclassified by at most a couple of minutes, but not confirmed as
  exact. A quick look at `astral`'s source found
  `SUN_APPARENT_RADIUS = 32.0 / (60.0 * 2.0)` (≈0.267°), suggesting its
  internal refraction adjustment may differ slightly from the -0.833°
  textbook value used here — this hasn't been fully run down. Worth
  revisiting if the exact sunrise/sunset boundary ever needs to be
  precise; for `LightStatus`'s purposes (was a lamp lit at night, broadly
  speaking) a couple of minutes of boundary imprecision isn't consequential.

  **Requires a timezone-aware `datetime`** and raises `ValueError`
  otherwise — solar position depends on the actual UTC instant, not a
  naive wall-clock reading with an assumed timezone, so guessing one
  silently would risk a wrong answer near a boundary. Confirmed this is
  correctly instant-based (not representation-based) with a test showing
  the same UTC moment gives the same answer regardless of which timezone
  was used to express it.

- **`PoleTelemetry.IsDaylight`** (cached, via `shared/pole_daylight_flags_loader.py`'s
  `load_pole_daylight_flags()`) **and `PoleVitals.IsOnline`/`LightStatus`**
  — together, these compare each reading's actual behavior against
  whether it should be daylight at that pole's location and moment,
  surfacing a genuine anomaly (a light that's dark when it should be lit)
  as a queryable status rather than something you'd have to notice by eye
  in a chart.

  **Why `IsDaylight` is a separate cached column, not computed inline in
  `PoleVitals`' aggregation SQL**: `is_daylight()` needs Python (`astral`)
  — there's no way to run it inside a T-SQL query, the same reasoning
  that motivated `PoleTimeZones` existing as its own cached table instead
  of calling `timezonefinder` from SQL. But unlike a pole's timezone
  (one value, constant forever), daylight status is a function of the
  *reading's own moment* — it's not a per-pole constant, so it can't be
  cached per-`LocationId` the way `PoleTimeZones` is. Instead, it's cached
  **per reading**: computed once per `PoleTelemetry` row (a reading's
  timestamp never changes once recorded, so "was it daylight then, at
  that pole's location" is a fact that never changes either), using
  `PoleTimeZones`' `Latitude`/`Longitude` — **not** `PoleTelemetry`'s own
  raw coordinates — per an explicit instruction, and for good reason:
  `PoleTelemetry`'s per-reading GPS can be corrupted (Null Island,
  micro-degree scaling errors — see `shared/timezone_utils.py`'s notes),
  while `PoleTimeZones` holds one already-vetted coordinate per pole.
  `load_pole_daylight_flags()` only processes rows with `IsDaylight IS
  NULL`, capped at 10,000 per run — unbounded after the column is first
  added (needs several runs to fully backfill existing history), but only
  ever a handful of newly-arrived rows per ordinary 10-minute cycle
  afterward. It `INNER JOIN`s `PoleTimeZones` (not `LEFT`) and requires
  `WindowsTimeZone IS NOT NULL`, since a location whose coordinates
  couldn't be trusted can't have its daylight status computed at all —
  those rows are simply left for a later cycle, once (if ever)
  `PoleTimeZones` resolves them.

  **A real, silent bug caught in production**: the `UPDATE`'s
  `WHERE LocationId = ? AND LastUpload = ?` was being bound with the
  *raw* `datetime` object read straight back from the earlier `SELECT`
  (itself already timezone-aware via `sql_client.py`'s `DATETIMEOFFSET`
  output converter). That's exactly the established pyodbc +
  `DATETIMEOFFSET` gotcha this project already documents elsewhere
  (pyodbc silently mishandles a timezone-aware `datetime` bound as a
  *write* parameter) — every other loader here formats these as an
  explicit offset string via `_to_dto_string()` first; this was the one
  place that didn't. The consequence was worse than a crash: the `WHERE`
  clause silently matched **zero rows on every single update**, with no
  exception raised (a `WHERE` clause matching nothing isn't a SQL error),
  while `total_success += len(updates)` happened unconditionally
  afterward — so `SP_Execution` kept reporting full success the entire
  time, with `PoleTelemetry.IsDaylight` never actually getting set for a
  single row. Confirmed by a `SELECT ... WHERE IsDaylight IS NOT NULL`
  against the real database returning nothing, despite `SP_Execution`
  showing successful runs. Fixed by formatting `last_upload` via
  `_to_dto_string()` before it's ever used as a write parameter — same
  as every other loader already does. Also added a defensive check: a
  clean `cursor.rowcount == 0` after the batch `UPDATE` now logs a loud
  warning, specifically so this exact failure mode (no exception, zero
  actual effect) can't silently recur unnoticed again for some other
  reason.

  **This bug likely also explains, or at least contributed to, separate
  database performance symptoms observed around the same time**: since
  the backlog of unflagged rows never actually shrank (nothing was ever
  really being written), `load_pole_daylight_flags()` was very likely
  reprocessing the *same* ~10,000-row backlog every single cycle,
  indefinitely, rather than converging over a few runs the way it
  was designed to — real, unnecessary, recurring database load stacked
  on top of `PoleVitals`' own aggregation queries every cycle.

  **`LightStatus`'s per-reading classification** (computed entirely in
  `PoleVitals`' own SQL, once `IsDaylight` is available as a plain cached
  column — no further Python needed at aggregation time):
  ```
  IsOnline = 0                      -> 'Working'   (no data to judge a
                                        malfunction from, so this does
                                        NOT flag it as broken)
  IsDaylight IS NULL                -> NULL         (excluded from the
                                        bucket aggregation, same as the
                                        NULLIF-guarded Avg* percentages)
  IsDaylight = 1                    -> 'DayLight'   (lamp isn't expected
                                        to be lit, not a working/not-
                                        working judgment at all)
  LampPower1 > 0 OR LampPower2 > 0  -> 'Working'    (confirmed lit at
                                        night, as expected)
  otherwise (online, night, dark)   -> 'Not Working' (the genuine
                                        anomaly case)
  ```
  The `IsOnline = 0 → 'Working'` branch is deliberately lenient, not a
  typo: an offline reading gives no evidence either way, so it's treated
  as non-alarming by default rather than flagged as a malfunction on
  thin evidence.

  **Bucket-level aggregation is priority-based, not a majority vote**: if
  *any* reading in the bucket was `'Not Working'`, the whole bucket is
  `'Not Working'` — a single confirmed fault shouldn't get averaged away
  by a bunch of otherwise-fine readings. Otherwise, if *any* reading was
  `'Working'`, the bucket is `'Working'`; only if neither ever occurred
  does it fall back to `'DayLight'`. `IsOnline` at the bucket level is
  simply "was any reading in scope online".

  **Hour** uses the whole hour's data for both columns — no extra
  restriction needed, since the bucket itself is already narrow. **Day**
  (and, back when they existed, Week/Month too) deliberately uses a much
  narrower window than the rest of that period's data: the **last 6
  hours of that bucket's own end** — "was this pole alive/healthy toward
  the end of this period", not "was it online at any point during it"
  (which would be true for nearly every actively-reporting pole almost
  all the time, making the flag far less useful). This window is
  expressed as a **pure SQL date-arithmetic expression relative to each
  bucket's own boundary** (`DATEADD(HOUR, -6, DATEADD(DAY, 1,
  BucketStart))`), computed inside the query itself.

  **A real bug, caught and fixed before this shipped**: an earlier
  version computed this window as `NOW - 6 hours` — a single value,
  computed once per `load_pole_vitals()` run and shared across every
  bucket being recomputed. That's subtly wrong: it only agrees with "the
  last 6 hours of that bucket's own end" for whichever single bucket
  happens to be currently in progress. Every other bucket touched in the
  same run (the incremental design always recomputes *both* the current
  and the immediately-previous bucket) would get windowed relative to
  *whenever the job happened to run*, not relative to its own actual
  end — meaning a historical bucket's `IsOnline`/`LightStatus` could
  silently change on every future recompute, depending on what time of
  day the loader happened to execute, rather than staying a fixed fact
  about that historical period. Moving the window to a same-query SQL
  expression relative to each bucket's own boundary fixed this, and
  simplified the Python side too — the "last 6 hours" parameter that used
  to be computed and passed in separately for Day (and, back when they
  existed, Week/Month) is gone entirely; every period type now shares
  the exact same 4-parameter shape. `tests/test_pole_vitals_loader.py`'s
  `TestDayRecentActivityWindow` class (originally
  `TestDayWeekMonthRecentActivityWindow`, renamed when Week/Month were
  removed) exists specifically to guard against this regressing.

  **A second real bug, this one only caught in production** (SQLSTATE
  `42000`, `"The datepart hour is not supported by date function dateadd
  for data type date"`): the fix above needed `DATEADD(HOUR, -6, ...)`
  applied around each bucket's own end, but Day's own end
  (`DATEADD(DAY, 1, CAST(LocalTime AS DATE))`) and (back when it existed)
  Month's own end (`DATEADD(MONTH, 1, DATEFROMPARTS(...))`) both evaluate
  to a plain `DATE` value — and `DATE` has no time component, so
  `DATEADD(HOUR, ...)` on one fails outright rather than silently doing
  something wrong. **Week's equivalent expression was already correct**
  (it cast to `DATETIME2(3)` before the day arithmetic — `CAST(w.EndDate
  AS DATETIME2(3))`), which is exactly why only Day (and, had the run not
  failed on a connection issue first, Month too) hit this — the fix was
  to add that same missing `CAST(... AS DATETIME2(3))` to Day's and
  Month's expressions, matching Week's already-working pattern.
  `TestDayRecentActivityWindow` (Day's slice of the original
  `TestDayWeekMonthRecentActivityWindow` class) still has both an
  exact-match regression test for the fixed expression and a looser
  structural check (every `DATEADD(HOUR, ...)` in Day's SQL must have a
  `DATETIME2` cast somewhere before it) intended to catch the same
  *class* of mistake even if the exact expression text changes later.

  **`load_pole_vitals()` commits after each period type individually,
  not once at the end for all of them** — a real production incident
  motivated this, not a hypothetical: severe Azure SQL CPU contention
  (confirmed via `sys.dm_exec_requests` showing `status = 'runnable'`,
  not `suspended`/blocked — the request was simply queued behind other
  work, not deadlocked) left `Week`'s `MERGE` stuck for over 20 minutes
  while `Day`'s had already succeeded (`Week` has since been removed
  entirely — see the note under `PoleVitals` above — but the commit
  design this incident motivated still applies to whatever period types
  remain). Under the old one-commit-at-the-end design, `Day`'s
  already-computed rows were sitting in the *same* open transaction as
  the stuck `Week` query — meaning if `Week` never finished (or the Flex
  Consumption plan's own `functionTimeout`, 30 minutes by default per
  `host.json` having no override, killed the whole invocation first),
  `Day`'s work would roll back along with everything else, needing to be
  redone from scratch on the next cycle. Each period type now
  succeeds-and-commits or fails-and-rolls-back entirely independently —
  a slow or failing period type can no longer put an already-finished
  one's results at risk. `TestLoadPoleVitalsPerPeriodTypeCommits`
  verifies the exact `execute`/`commit`/`rollback` ordering (via
  `side_effect`-based call-order tracking, not `mock_conn.mock_calls` —
  confirmed directly that `mock_calls` doesn't reliably propagate calls
  made on this project's explicitly-named `mock_cursor`/`mock_conn`
  fixtures before
  relying on it for these tests), and specifically confirms an earlier
  period type's `commit()` has already happened before a later one's
  failure — the actual property this change exists for.

