"""
process_provisioned_telemetry_events() -- handles a BATCH of Event Hub
messages (Streetleaf's own provisioned-poles telemetry, pushed in
real time from the ProvisionTestEventHub namespace's own
provisiontesteventhub Event Hub) and upserts them into PoleTelemetry,
same table (and same reusable staging/MERGE SQL, imported directly from
pole_telemetry_loader.py) Leadsun's own telemetry already writes to --
distinguished by Source = 'Provisioned', same convention already
established for PoleModels.

Architecturally different from every other loader in this project:
push, not pull. There's no separate fetch phase to hold a connection
open across (Event Hub hands the batch directly to the trigger as its
own input), so this doesn't need the fetch-then-write connection
discipline every timer-triggered loader here follows -- one connection
for the whole invocation is enough.

Real, confirmed message shape (one event):
    {
        "Timestamp": 1789391964,              -- Unix epoch SECONDS
        "PoleID": "0a10aced202194944a071358",  -- matches Poles.ProvisionedPoleId
        "LampPower1": 0,
        "BatterySoC": 100,                     -- NOT currently mapped -- see below
        "LightRatio": 0,                       -- NOT currently mapped -- see below
        "PanelPercentage": 38.2,                -- NOT currently mapped -- see below
        "SolarBoardVoltage": 38.9,
        "SolarBoardCurrent": 1.7,
        "BatteryElecCurrent": 2.2,
        "BatteryVoltage": 26.8,
        "LocationCoordinates": {"Latitude": 27.86201, "Longitude": -82.34443},
        "StreetlightMode": "Default",          -- NOT currently mapped -- see below
        "BatteryFault": 0,                     -- NOT currently mapped -- see below
        "MPPTFault": 0,                        -- NOT currently mapped -- see below
        "LEDFault": 0,                         -- NOT currently mapped -- see below
        "ControllerFault": 0                   -- NOT currently mapped -- see below
    }

Mapping, per explicit instruction:
    PoleID            -> PoleId (also the join key against
                         Poles.ProvisionedPoleId, for the Lat/Long
                         update below -- NOT looked up into that pole's
                         own separate PoleId first; PoleID itself
                         becomes PoleTelemetry.PoleId directly, the
                         same way Leadsun's own "productName" becomes
                         PoleId directly for its own rows)
    Timestamp         -> LastUpload, converted from Unix epoch seconds
                         to a UTC DATETIMEOFFSET string (same
                         to_dto_string()-based, UTC-offset convention
                         Leadsun's own LastUpload already uses -- see
                         pole_telemetry_loader._parse_iso_datetime()).
                         No timezone ambiguity here, unlike Poles.
                         ProvisionedPoleCreatedDateTime's own situation
                         -- Unix epoch time is unambiguous by
                         definition.
    (no source field)  -> IsOnline, always TRUE -- per explicit request.
                         The event itself carries no online/offline
                         signal of its own; simply receiving a
                         telemetry event at all is treated as proof
                         this pole is online right now. Unlike Leadsun's
                         own IsOnline (a real field the API reports),
                         this is a fixed assumption, not a read value --
                         a provisioned pole that's gone truly silent
                         just won't send an event at all, so there's
                         nothing here to ever report False.
    LampPower1        -> LampPower1 (LampPower2 always None -- see below)
    SolarBoardVoltage -> SolarBoardVoltage (name already matches)
    SolarBoardCurrent -> SolarBoardElecCurrent
    BatteryVoltage    -> BatteryVoltage1 (BatteryVoltage2 always None)
    BatteryElecCurrent -> BatteryElecCurrent1 (BatteryElecCurrent2 always None)
    LocationCoordinates.Latitude/Longitude -> BOTH this row's own
        PoleTelemetry.Latitude/Longitude (mirroring how Leadsun's own
        telemetry already populates those same two columns) AND,
        separately, per explicit instruction ("we actually trust these
        values"), a live UPDATE of Poles.Lat/Poles.Long for whichever
        Poles row has a matching ProvisionedPoleId -- overwriting
        whatever Airtable-sourced Lat/Long that pole currently has.
        Skipped (no UPDATE issued) if the coordinates are missing, or if
        no Poles row currently has that ProvisionedPoleId.

NOT currently mapped anywhere -- captured in ExtraFieldsJson instead of
being dropped, this project's own established safety-net convention:
BatterySoC, LightRatio, PanelPercentage, StreetlightMode, BatteryFault,
MPPTFault, LEDFault, ControllerFault. The four *Fault fields in
particular are worth flagging explicitly: Leadsun's own IsLedFault/
IsBatteryFault/IsPanelFault/IsPoleFault are CALCULATED downstream (in
pole_vitals_loader.py, from raw sensor thresholds), not read directly
off any single telemetry reading -- these provisioned-device fault
flags arrive as an entirely different shape (the device reports its own
fault state directly), and nothing here feeds them into that same
downstream calculation yet. Revisit if these should drive PoleVitals'
own fault flags for provisioned poles specifically, once that's wanted.

Also worth flagging: LampPower2/BatteryElecCurrent2 being permanently
None for every provisioned-sourced row is safe for lightStatusLabel
(api_utils.compute_pole_status_labels() already treats a None
individual reading as 0 when summing LampPower1+LampPower2), but NOT
safe for batteryStatusLabel/panelIdleReason's own "Full"/"Battery Full"
branch -- that threshold checks for BatteryElecCurrent1+
BatteryElecCurrent2 EXACTLY EQUAL TO 200, calibrated to Leadsun's own
two-battery-bank hardware (100+100). A provisioned pole's single-battery
reading summed with a permanent 0 for battery 2 will essentially never
equal exactly 200, even when that one battery genuinely IS full -- so
these two fields will likely never show a provisioned pole as "Full"/
"Battery Full", regardless of its actual state. Not fixed here since it
wasn't part of what was asked; worth a follow-up if accurate
battery-full detection matters for these poles.

IsOpenIssueFault is computed via a PoleOpenIssues join through
Poles.ProvisionedPoleId (same pattern as load_leadsun_pole_telemetry()
uses via Poles.PoleId) -- fetched once per invocation and checked
via set membership when building each row.
"""

import os
import json
import logging
import time
from datetime import datetime, timezone

from shared.sql_client import get_connection
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.daylight_utils import is_daylight as _is_daylight
from shared.pole_daylight_flags_loader import (
    _LED_FAULT_GRACE_PERIOD,
    _PANEL_FAULT_SUNRISE_WARMUP_PERIOD,
    _PANEL_FAULT_SUNSET_WINDDOWN_PERIOD,
    _UPDATE_IS_DAYLIGHT_SQL,
)
from shared.pole_telemetry_loader import (
    _ALL_COLUMNS,
    _chunked,
    _STAGING_TABLE_SQL,
    _STAGING_INSERT_SQL,
    _MERGE_FROM_STAGING_SQL,
    _TRUNCATE_STAGING_SQL,
    _ROW_UPSERT_SQL,
    _UPSERT_BATCH_SIZE,
)

ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")
SOURCE_NAME = "Provisioned"

# Every event field this module explicitly reads -- anything else in a
# given event lands in ExtraFieldsJson instead of being dropped. Kept as
# its own named constant (checked directly by tests) rather than
# inlined, so it's the one place this list needs updating if a new
# field ever gets mapped.
_KNOWN_EVENT_FIELDS = frozenset(
    {
        "Timestamp",
        "PoleID",
        "LampPower1",
        "SolarBoardVoltage",
        "SolarBoardCurrent",
        "BatteryElecCurrent",
        "BatteryVoltage",
        "LocationCoordinates",
        # ExtraFieldsJson fields now promoted to their own columns:
        "BatterySoC",
        "LightRatio",
        "PanelPercentage",
        "BatteryFault",
        "LEDFault",
        "ControllerFault",
        # MPPTFault and StreetlightMode are NOT promoted -- they remain
        # in ExtraFieldsJson as before.
    }
)

_UPDATE_POLES_LAT_LONG_SQL = "UPDATE Poles SET Lat = ?, Long = ? WHERE ProvisionedPoleId = ?"


def _epoch_seconds_to_dto_string(epoch_seconds) -> str:
    """
    Converts Unix epoch SECONDS (confirmed -- not milliseconds) to a UTC
    DATETIMEOFFSET string, matching pole_telemetry_loader._parse_iso_
    datetime()'s own UTC-offset convention for this exact same column.
    Unlike Poles.ProvisionedPoleCreatedDateTime's own ambiguous source
    timezone, Unix epoch time has no ambiguity to begin with -- it's
    already, by definition, seconds since the UTC epoch.
    """
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return _to_dto_string(dt)


def _is_azure_monitor_diagnostics_envelope(event: dict) -> bool:
    """
    Recognizes Azure Monitor's own diagnostic-data envelope shape --
    metrics ({"records": [{"metricName": ..., "resourceId": ..., ...}]})
    and resource/activity logs ({"records": [{"operationName": ...,
    "status": ..., "resourceId": ..., ...}]}) alike -- both confirmed
    landing on this SAME Event Hub in practice, from an upstream Azure
    resource's own Diagnostic Settings being pointed at it (most likely
    the IOTHUB-STREAM Stream Analytics job sitting ahead of this Event
    Hub in the pipeline) rather than at a dedicated diagnostics
    destination. This is NOT a code bug on this side to fix -- the real
    fix is reconfiguring that resource's own Diagnostic Settings to stop
    sending its metrics/logs into the same Event Hub real device
    telemetry also flows through.

    Recognizing this shape explicitly (rather than treating it as just
    another malformed telemetry event) matters for one reason: it lets
    this loader tell the difference between "this is known, harmless
    noise from an unrelated Azure Monitor feed" (log at INFO, don't
    count as an error) and "this looks like it was SUPPOSED to be real
    telemetry but came through malformed somehow" (still a genuine
    ERROR, still counted -- that distinction stays exactly as it was).

    A real telemetry event never has a top-level "records" list, and
    this diagnostic shape never has a "PoleID" -- checking for the
    combination (not either alone) keeps this from ever misclassifying
    a genuinely malformed telemetry event that happens to reuse one of
    these key names coincidentally.
    """
    records = event.get("records")
    return isinstance(records, list) and "PoleID" not in event


def _map_event_to_telemetry_row(event: dict) -> dict:
    """
    Maps one raw Event Hub message into PoleTelemetry's shape -- see
    this module's own docstring for the full field-by-field mapping and
    reasoning. Returns a dict keyed by PoleTelemetry column name (like
    pole_telemetry_loader._map_lamp_record()'s own contract), plus two
    extra keys this function's own caller needs afterward and pops back
    out before building the final row tuple: "PoleID" (the raw,
    unmapped join key, kept alongside "PoleId" even though they're
    currently the same value, so callers never have to assume that
    equivalence holds) and "_Latitude"/"_Longitude" (the SAME values
    already placed into this dict's own "Latitude"/"Longitude" keys,
    duplicated under a leading-underscore name specifically so
    set(mapped) minus the underscore-prefixed keys reflects exactly
    the real PoleTelemetry columns being populated, for tests that
    check that boundary).
    """
    location_coordinates = event.get("LocationCoordinates") or {}
    latitude = location_coordinates.get("Latitude")
    longitude = location_coordinates.get("Longitude")

    extra_fields = {k: v for k, v in event.items() if k not in _KNOWN_EVENT_FIELDS}

    timestamp = event.get("Timestamp")

    return {
        "PoleId": event.get("PoleID"),
        "LastUpload": _epoch_seconds_to_dto_string(timestamp) if timestamp is not None else None,
        "IsOnline": True,  # assumed, per explicit request -- the event
                           # itself carries no online/offline signal of
                           # its own; simply RECEIVING a telemetry event
                           # at all is treated as proof this pole is
                           # online right now.
        "LampPower1": event.get("LampPower1"),
        "LampPower2": None,
        "SolarBoardVoltage": event.get("SolarBoardVoltage"),
        "SolarBoardElecCurrent": event.get("SolarBoardCurrent"),
        "BatteryVoltage1": event.get("BatteryVoltage"),
        "BatteryVoltage2": None,
        "BatteryElecCurrent1": event.get("BatteryElecCurrent"),
        "BatteryElecCurrent2": None,
        "Latitude": latitude,
        "Longitude": longitude,
        "ExtraFieldsJson": json.dumps(extra_fields) if extra_fields else None,
        # Device-reported percentages and fault flags, promoted from
        # ExtraFieldsJson to their own columns so the provisioned vitals
        # MERGE can reference them directly without JSON_VALUE() in SQL.
        # BatteryFault/LEDFault/ControllerFault: cast to bool so pyodbc
        # writes them as BIT (1/0); None if the key is absent.
        "BatterySoC":      event.get("BatterySoC"),
        "LightRatio":      event.get("LightRatio"),
        "PanelPercentage": event.get("PanelPercentage"),
        "BatteryFault":    bool(event["BatteryFault"]) if "BatteryFault" in event else None,
        "LEDFault":        bool(event["LEDFault"]) if "LEDFault" in event else None,
        "ControllerFault": bool(event["ControllerFault"]) if "ControllerFault" in event else None,
        "_PoleID": event.get("PoleID"),
    }


def _fetch_provisioned_pole_ids_with_open_issues(cursor) -> set:
    """
    Every ProvisionedPoleId whose pole has at least one row in
    PoleOpenIssues -- mirrors pole_telemetry_loader's own
    _fetch_pole_ids_with_open_issues() but joins via ProvisionedPoleId
    rather than PoleId, since that's the identifier provisioned poles
    use. Fetched once per process_provisioned_telemetry_events() invocation
    (cheap -- PoleOpenIssues only holds currently-open issues) and checked
    via set membership when building each row.
    """
    cursor.execute(
        """
        SELECT DISTINCT p.ProvisionedPoleId
        FROM Poles p
        JOIN PoleOpenIssues poi ON poi.PoleId = p.Id
        WHERE p.ProvisionedPoleId IS NOT NULL
        """
    )
    return {row[0] for row in cursor.fetchall()}


def _fetch_provisioned_pole_timezones(cursor) -> dict:
    """
    Returns {ProvisionedPoleId: (latitude, longitude)} for every
    provisioned pole that has a resolved PoleTimeZones row.

    Fetched once per process_provisioned_telemetry_events() invocation
    so _build_row() can compute IsDaylight, IsDaylightForLedFault, and
    IsDaylightForPanelFault inline -- same per-row-at-ingestion-time
    pattern as IsOpenIssueFault, avoiding a separate
    load_provisioned_pole_daylight_flags() call after every Event Hub push.

    Poles whose timezone hasn't been resolved yet (no PoleTimeZones row,
    or WindowsTimeZone IS NULL) are simply absent from the returned dict.
    _build_row() leaves their daylight flags as None in that case --
    load_provisioned_pole_daylight_flags() on the next scheduled cycle
    will backfill them once the timezone is resolved.
    """
    cursor.execute(
        """
        SELECT ProvisionedPoleId, Latitude, Longitude
        FROM PoleTimeZones
        WHERE ProvisionedPoleId IS NOT NULL
          AND WindowsTimeZone IS NOT NULL
          AND Latitude IS NOT NULL
          AND Longitude IS NOT NULL
        """
    )
    return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}


def _compute_daylight_flags(
    last_upload,
    latitude: float,
    longitude: float,
) -> tuple:
    """
    Computes (IsDaylight, IsDaylightForLedFault, IsDaylightForPanelFault)
    for a single telemetry reading. Mirrors the per-row logic in
    load_provisioned_pole_daylight_flags() exactly -- same grace periods,
    same triple-computation -- so rows flagged at ingestion time are
    consistent with rows backfilled by the scheduled loader.

    Returns (None, None, None) if last_upload is None or is_daylight()
    raises, so the caller can leave the flags unset rather than storing
    a wrong value.
    """
    if last_upload is None:
        return None, None, None
    try:
        is_day = _is_daylight(last_upload, latitude, longitude)
        is_day_led = (
            _is_daylight(last_upload - _LED_FAULT_GRACE_PERIOD, latitude, longitude)
            or is_day
            or _is_daylight(last_upload + _LED_FAULT_GRACE_PERIOD, latitude, longitude)
        )
        is_day_panel = (
            is_day
            and _is_daylight(last_upload - _PANEL_FAULT_SUNRISE_WARMUP_PERIOD, latitude, longitude)
            and _is_daylight(last_upload + _PANEL_FAULT_SUNSET_WINDDOWN_PERIOD, latitude, longitude)
        )
        return is_day, is_day_led, is_day_panel
    except Exception as ex:
        logging.warning(
            "loadProvisionedPoleTelemetry: failed to compute daylight flags "
            "for LastUpload=%s at (%.6f, %.6f): %s -- flags left NULL, "
            "will be backfilled by loadProvisionedPoleDaylightFlags.",
            last_upload, latitude, longitude, ex,
        )
        return None, None, None


def _build_row(
    mapped: dict,
    sp_exec_id,
    open_issue_provisioned_pole_ids: set,
    provisioned_pole_timezones: dict,
) -> tuple:
    """Assembles the final param tuple in _ALL_COLUMNS order. A small,
    near-identical twin of pole_telemetry_loader._build_row() rather
    than a direct import of it -- that function closes over ITS OWN
    module's SOURCE_NAME ('Leadsun'), so reusing it here would silently
    tag every provisioned row as Leadsun-sourced.

    IsOpenIssueFault is computed via open_issue_provisioned_pole_ids.
    IsDaylight/IsDaylightForLedFault/IsDaylightForPanelFault are NOT set
    here -- those columns are not in _ALL_COLUMNS and are written by a
    separate UPDATE after the MERGE (see process_provisioned_telemetry_events).
    provisioned_pole_timezones is accepted here so the signature is complete
    for callers, but daylight computation happens post-MERGE.
    """
    values = dict(mapped)
    values["Source"] = SOURCE_NAME
    values["SP_ExecId"] = sp_exec_id
    pole_id = mapped.get("_PoleID")
    values["IsOpenIssueFault"] = pole_id in open_issue_provisioned_pole_ids if pole_id else False
    return tuple(values.get(col) for col in _ALL_COLUMNS)


def process_provisioned_telemetry_events(events: list) -> None:
    """
    Processes one BATCH of Event Hub messages (this is what the
    event_hub_message_trigger in function_app.py calls directly, with
    whatever batch size Azure itself decided to deliver -- cardinality
    is "many", so this always receives a list, even a list of one).

    One connection for the whole invocation -- unlike every timer-
    triggered loader here, there's no separate fetch phase to avoid
    holding a connection open across (Event Hub already handed us the
    batch directly), so the usual fetch-then-write discipline doesn't
    apply.

    Writes an SP_Execution row per invocation (Name =
    'loadProvisionedPoleTelemetry'), same as every other loader -- with
    "many events per invocation" (cardinality="many"), this shouldn't
    produce meaningfully more SP_Execution rows than this project's own
    existing timer-triggered loaders already do, even though the
    triggering MECHANISM (continuous push vs. periodic pull) is
    different.
    """
    start_time = _to_dto_string(_now_eastern())
    sp_exec_id = None
    total_success = 0
    total_errors = 0
    conn = None
    cursor = None

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO SP_Execution (Name, Environment, StartDateTime, Source, BatchCount, IsFinalBatch)
            OUTPUT INSERTED.Id
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            "loadProvisionedPoleTelemetry",
            ENVIRONMENT,
            start_time,
            SOURCE_NAME,
        )
        sp_exec_id = cursor.fetchone()[0]
        conn.commit()

        upsert_start = time.perf_counter()
        valid_mapped_rows = []

        # Fetch once per invocation -- cheap, PoleOpenIssues is small
        # (only currently-open issues). Same pattern as load_leadsun_pole_telemetry().
        open_issue_provisioned_pole_ids = _fetch_provisioned_pole_ids_with_open_issues(cursor)

        # Fetch once per invocation -- PoleTimeZones for provisioned poles is
        # a small table (one row per provisioned pole). Used to compute daylight
        # flags inline at ingestion time rather than deferring to a separate
        # scheduled load_provisioned_pole_daylight_flags() call, which would
        # leave every newly-arrived telemetry row with NULL flags until the
        # next scheduled cycle. Poles with no resolved timezone yet are absent
        # from this dict and get NULL flags (backfilled by the scheduled loader
        # once their CountyFips is populated).
        provisioned_pole_timezones = _fetch_provisioned_pole_timezones(cursor)

        for event in events:
            mapped = _map_event_to_telemetry_row(event)
            if mapped["PoleId"] is None or mapped["LastUpload"] is None:
                if _is_azure_monitor_diagnostics_envelope(event):
                    # Known, harmless noise -- see
                    # _is_azure_monitor_diagnostics_envelope()'s own
                    # docstring. Not counted as an error: this Event
                    # Hub receiving this data at all is an Azure
                    # Diagnostic Settings configuration issue upstream,
                    # not a failure of anything this loader is
                    # responsible for.
                    logging.info(
                        "loadProvisionedPoleTelemetry: ignoring an Azure Monitor "
                        "diagnostics event (metrics/logs, not device telemetry) -- "
                        "this Event Hub likely has a Diagnostic Settings export "
                        "pointed at it that should be redirected elsewhere."
                    )
                    continue
                total_errors += 1
                logging.error(
                    "loadProvisionedPoleTelemetry: skipping event -- would produce a "
                    "NULL PoleId and/or LastUpload (missing/unusable PoleID or "
                    "Timestamp). Raw event: %s",
                    event,
                )
                continue
            valid_mapped_rows.append(mapped)

        # Telemetry upsert: same reusable staging/MERGE SQL as
        # pole_telemetry_loader.py's own Leadsun ingestion -- see this
        # module's own docstring for why this is safe to share directly.
        param_rows = [_build_row(mapped, sp_exec_id, open_issue_provisioned_pole_ids, provisioned_pole_timezones) for mapped in valid_mapped_rows]
        cursor.fast_executemany = True

        if param_rows:
            cursor.execute(_STAGING_TABLE_SQL)

        for batch in _chunked(param_rows, _UPSERT_BATCH_SIZE):
            try:
                cursor.executemany(_STAGING_INSERT_SQL, batch)
                cursor.execute(_MERGE_FROM_STAGING_SQL)
                cursor.execute(_TRUNCATE_STAGING_SQL)
                total_success += len(batch)
                logging.info(
                    "loadProvisionedPoleTelemetry: upserted %d row(s): %s",
                    len(batch),
                    [(row[0], row[1]) for row in batch],  # (PoleId, LastUpload) pairs
                )
            except Exception as batch_error:
                logging.warning(
                    "loadProvisionedPoleTelemetry: chunk of %d failed to bulk-merge (%s); "
                    "retrying row-by-row.",
                    len(batch),
                    batch_error,
                )
                cursor.execute(_TRUNCATE_STAGING_SQL)
                for row in batch:
                    try:
                        cursor.execute(_ROW_UPSERT_SQL, row)
                        total_success += 1
                        logging.info(
                            "loadProvisionedPoleTelemetry: upserted row: PoleId=%s, "
                            "LastUpload=%s",
                            row[0],
                            row[1],
                        )
                    except Exception as row_error:
                        total_errors += 1
                        logging.error(
                            "loadProvisionedPoleTelemetry: failed to upsert %s: %s",
                            row[0],  # PoleId is the first positional column
                            row_error,
                        )

        # Poles.Lat/Long update -- per explicit instruction, these
        # coordinates are trusted enough to overwrite whatever
        # Airtable-sourced value a matching pole currently has. Plain
        # per-event UPDATEs, not a staged bulk operation -- Event Hub
        # batches here are expected to be small (a handful of readings
        # per invocation), nowhere near the scale the staging-table
        # pattern exists to make bulk-efficient elsewhere in this
        # project. Skipped entirely for an event with no coordinates at
        # all; silently affects zero rows (not an error) for a PoleID
        # that matches no current Poles.ProvisionedPoleId.
        for mapped in valid_mapped_rows:
            if mapped["Latitude"] is None or mapped["Longitude"] is None:
                continue
            cursor.execute(
                _UPDATE_POLES_LAT_LONG_SQL,
                mapped["Latitude"],
                mapped["Longitude"],
                mapped["_PoleID"],
            )

        # Daylight flags -- computed per-row inline at ingestion time so
        # newly-arrived telemetry doesn't wait until the next scheduled
        # loadProvisionedPoleDaylightFlags cycle (which could be up to an
        # hour away) before getting IsDaylight/IsDaylightForLedFault/
        # IsDaylightForPanelFault set. Uses the same _UPDATE_IS_DAYLIGHT_SQL
        # and grace-period constants as load_provisioned_pole_daylight_flags()
        # so values are consistent regardless of which path set them.
        # Poles with no resolved PoleTimeZones row yet are skipped here --
        # the scheduled loader will backfill them once their timezone is known.
        daylight_updates = []
        for mapped in valid_mapped_rows:
            pole_id = mapped.get("_PoleID")
            tz_coords = provisioned_pole_timezones.get(pole_id) if pole_id else None
            if tz_coords is None:
                continue
            lat, lon = tz_coords
            last_upload_str = mapped.get("LastUpload")
            if last_upload_str is None:
                continue
            try:
                last_upload = datetime.fromisoformat(
                    last_upload_str.replace(" +00:00", "+00:00")
                )
            except Exception:
                continue
            is_day, is_day_led, is_day_panel = _compute_daylight_flags(last_upload, lat, lon)
            if is_day is None:
                continue  # compute error already logged inside _compute_daylight_flags
            daylight_updates.append((is_day, is_day_led, is_day_panel, mapped["PoleId"], last_upload_str))

        if daylight_updates:
            try:
                cursor.executemany(_UPDATE_IS_DAYLIGHT_SQL, daylight_updates)
                logging.info(
                    "loadProvisionedPoleTelemetry: computed daylight flags for %d row(s) inline.",
                    len(daylight_updates),
                )
            except Exception as daylight_error:
                logging.warning(
                    "loadProvisionedPoleTelemetry: bulk daylight UPDATE failed (%s); "
                    "falling back to row-by-row.",
                    daylight_error,
                )
                for update_args in daylight_updates:
                    try:
                        cursor.execute(_UPDATE_IS_DAYLIGHT_SQL, update_args)
                    except Exception as row_err:
                        logging.warning(
                            "loadProvisionedPoleTelemetry: failed to set daylight flags "
                            "for PoleId=%s LastUpload=%s: %s -- will be backfilled "
                            "by loadProvisionedPoleDaylightFlags.",
                            update_args[3], update_args[4], row_err,
                        )

        conn.commit()
        logging.info(
            "loadProvisionedPoleTelemetry: processed %d event(s) in %.1fs.",
            len(events),
            time.perf_counter() - upsert_start,
        )

        cursor.execute(
            """
            UPDATE SP_Execution
            SET EndDateTime = ?,
                TotalSuccessfulRecords = ?,
                TotalErrorRecords = ?,
                BatchCount = ?,
                IsFinalBatch = 1
            WHERE Id = ?
            """,
            _to_dto_string(_now_eastern()),
            total_success,
            total_errors,
            1,
            sp_exec_id,
        )
        conn.commit()

    except Exception as ex:
        logging.error("loadProvisionedPoleTelemetry: run failed: %s", ex)
        if sp_exec_id:
            try:
                cursor.execute(
                    """
                    UPDATE SP_Execution
                    SET EndDateTime = ?, ErrorMessage = ?, TotalSuccessfulRecords = ?, TotalErrorRecords = ?
                    WHERE Id = ?
                    """,
                    _to_dto_string(_now_eastern()),
                    str(ex),
                    total_success,
                    total_errors,
                    sp_exec_id,
                )
                conn.commit()
            except Exception as log_error:
                logging.error(
                    "loadProvisionedPoleTelemetry: also failed to record ErrorMessage on "
                    "SP_Execution: %s",
                    log_error,
                )
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()
