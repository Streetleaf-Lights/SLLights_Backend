import json
import logging
import os
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import azure.functions as func

from shared.customers_loader import load_customers
from shared.projects_loader import load_projects
from shared.poles_loader import load_poles
from shared.pole_open_issues_loader import load_pole_open_issues
from shared.pole_models_loader import load_leadsun_pole_models
from shared.pole_telemetry_loader import load_leadsun_pole_telemetry, update_leadsun_project_details
from shared.pole_timezones_loader import load_leadsun_pole_timezones, load_provisioned_pole_timezones
from shared.pole_daylight_flags_loader import load_leadsun_pole_daylight_flags, load_provisioned_pole_daylight_flags
from shared.pole_vitals_loader import load_leadsun_pole_vitals, load_provisioned_pole_vitals
from shared.provisioned_data_loader import load_provisioned_pole_models, load_provisioned_pole_serials
from shared.provisioned_telemetry_loader import process_provisioned_telemetry_events
from shared.customers_api import get_customers
from shared.projects_api import get_projects
from shared.pole_vitals_api import get_pole_vitals, get_pole_vitals_by_period
from shared.poles_api import get_poles
from shared.api_utils import parse_bool_param
from shared.pole_remote_control import (
    set_pole_lights,
    PoleNotFoundError,
    PoleTelemetryNotFoundError,
    PoleNumbersNotResolvedError,
    GatewayNotFoundError,
    ProjectNotFoundError,
    ProjectHasNoLeadsunIdError,
    ProjectTelemetryNotFoundError,
    LeadsunEdgeAccountNotFoundError,
)
from shared.leadsun_edge_client import LeadsunEdgeApiError
from shared.users_api import get_users
from shared.auth_utils import AuthError, require_auth
from shared.users_management_api import (
    change_role,
    delete_user,
    forgot_password,
    invite_user,
    register_user,
    resend_invite,
    reset_password,
    sign_in,
    sign_out,
)

app = func.FunctionApp()

EASTERN = ZoneInfo("America/New_York")
TARGET_HOURS = {6, 18}  # 6 AM and 6 PM Eastern, DST-proof
ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")


# Flex Consumption runs on Linux, where NCRONTAB schedules always evaluate in
# UTC (WEBSITE_TIME_ZONE is ignored on Linux). To land reliably at 6 AM and
# 6 PM Eastern year-round without touching the cron expression across DST
# changes, this fires every hour on the hour and only does real work when the
# current Eastern-time hour matches one of TARGET_HOURS.
#
# Skips entirely when ENVIRONMENT == "Dev": local/dev runs shouldn't hit the
# real Airtable/SQL on a timer just because `func start` happens to be
# running -- use loadAirTableDataManual (unaffected by this check) to run it
# on demand instead.
@app.timer_trigger(
    schedule="0 0 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=True,
)
def loadAirTableData(myTimer: func.TimerRequest) -> None:
    if ENVIRONMENT == "Dev":
        logging.info(
            "loadAirTableData: skipping timer-triggered run in Dev -- use "
            "loadAirTableDataManual instead."
        )
        return

    if myTimer.past_due:
        logging.warning("loadAirTableData: timer is past due!")

    now_eastern = datetime.now(EASTERN)
    if now_eastern.hour not in TARGET_HOURS:
        logging.info(
            "loadAirTableData: skipping run (%s Eastern is not a scheduled hour).",
            now_eastern.strftime("%Y-%m-%d %H:%M %Z"),
        )
        return

    logging.info(
        "loadAirTableData: starting run at %s Eastern.",
        now_eastern.strftime("%Y-%m-%d %H:%M %Z"),
    )

    # Load order is Poles -> Projects -> Customers. None of the three has a
    # FK pointing "forward" at a table that hasn't loaded yet in this same
    # invocation (Poles.ProjectId/CustomerId and Projects.CustomerId are all
    # plain unconstrained columns), so this order can't hit a
    # referential-integrity error even though a Pole's Project/Customer, or
    # a Project's Customer, might not exist in the target table yet.
    load_poles()
    load_projects()
    load_customers()

    # PoleOpenIssues comes from a genuinely separate Airtable base (see
    # shared/pole_open_issues_loader.py's own AIRTABLE_POLE_ISSUES_BASE_ID
    # notes) with no load-order dependency on the three above (no FK,
    # enforced or otherwise, from Poles/Projects/Customers pointing at it)
    # -- placed last here since PoleOpenIssues.PoleId is logically meant to
    # line up with Poles.Id, even though that's not FK-enforced either.
    load_pole_open_issues()

    logging.info("loadAirTableData: run complete.")


# Manual trigger -- run it anytime with:
#   func start  (locally), then:
#   curl -X POST http://localhost:7071/api/loadAirTableDataManual
# or, once deployed to a non-Prod slot, POST to the deployed URL with the
# function key. Blocked outright in Prod so it can't accidentally be hit
# there. Unaffected by loadAirTableData's Dev-skip above -- in Dev, this is
# now the only way to trigger a run at all.
@app.route(
    route="loadAirTableDataManual", methods=["POST"], auth_level=func.AuthLevel.FUNCTION
)
def loadAirTableDataManual(req: func.HttpRequest) -> func.HttpResponse:
    if ENVIRONMENT == "Prod":
        return func.HttpResponse("Manual trigger is disabled in Prod.", status_code=403)

    logging.info("loadAirTableDataManual: manual run triggered.")
    load_poles()
    load_projects()
    load_customers()
    load_pole_open_issues()
    logging.info("loadAirTableDataManual: run complete.")

    return func.HttpResponse(
        "loadPoles + loadProjects + loadCustomers + loadPoleOpenIssues run complete.",
        status_code=200,
    )


# Separate from loadAirTableData on purpose -- different source (Leadsun,
# not Airtable), different cadence (every 30 minutes, not twice a day), and
# no dependency between the two: this pipeline doesn't join against
# Poles/Projects/Customers, so there's no load-order concern with the
# Airtable pipeline either way.
#
# Renamed from loadPoleRawData now that it orchestrates two loaders, not
# one -- mirrors loadAirTableData's naming (source name + "Data" as the
# umbrella, individual load_<x>() functions underneath). Load order is
# Models -> Telemetry -> TimeZones -> DaylightFlags -> Vitals -> Provisioned
# PoleModels: PoleModels
# is a device-model reference table needed by PoleVitals' Panel/Light
# percentage formulas (SunboardPower/LightPower), PoleTelemetry is the raw
# readings PoleVitals aggregates (now also computing IsOpenIssueFault per
# reading, joining against PoleOpenIssues/Poles -- see
# pole_telemetry_loader.py), PoleTimeZones resolves each pole's own
# timezone (from that same fresh telemetry's Longitude/Latitude) so
# PoleVitals can bucket in each pole's local time instead of assuming
# Eastern for every pole regardless of where it actually is,
# DaylightFlags computes/caches whether each not-yet-flagged reading
# happened during real daylight (using PoleTimeZones' coordinates and
# real per-day/per-location sunrise/sunset math -- see
# pole_daylight_flags_loader.py/shared/daylight_utils.py -- restored
# after a fixed clock-time window was tried in its place and found to
# misclassify whichever bucket straddles the actual sunrise/sunset
# moment), needed by PoleVitals' IsLedFault column, and PoleVitals
# depends on all four already being current for this cycle.
#
# load_provisioned_pole_models() and load_provisioned_pole_serials() run
# LAST, sequentially AFTER load_leadsun_pole_vitals() -- per explicit
# request that "loadProvisionedData" run right after loadDeviceData's
# own Leadsun-sourced steps. Serials runs AFTER models specifically (not
# just after Vitals) since a serial's own PoleModelId is meant to line
# up with a PoleModels row the models loader just wrote for that same
# product_id -- no FK enforces this ordering (see sql/Poles/"Add
# ProvisionedPoleId PoleModelId ProvisionedPoleCreatedDateTime
# columns.sql" for why), but it's the logically sensible order
# regardless. A genuinely SEPARATE timer trigger scheduled to fire
# shortly after this one's own 30-minute schedule was considered and
# rejected: this whole pipeline has taken as long as ~20-25 minutes in
# the past (see the schedule history note below), so a second,
# independently-scheduled timer can't actually GUARANTEE it runs after
# this one finishes -- it could just as easily fire while this one is
# still mid-run. Calling it sequentially, in the same function
# invocation, is the only way to make "runs right after loadDeviceData"
# a real guarantee rather than a usually-true assumption. It's a
# genuinely separate, independent loader in every other sense (its own
# SP_Execution Name, its own module, its own source database) -- this is
# purely a scheduling/ordering decision, not a merging of the two into
# one logical pipeline.
#
# schedule history, for anyone wondering why this keeps moving: 10
# minutes originally -> widened to 30 when Week/Month (since removed
# entirely from loadPoleVitals) made a normal run take ~20-25 minutes,
# too long for a 10-minute schedule to keep up with -> reverted back to
# 10 once Week/Month were gone and runs got fast again -> now back to 30
# again. This latest change wasn't accompanied by a specific new reason
# recorded here -- if a future run-time regression is what prompted it,
# it's worth writing that down here when it's known, the same way the
# earlier changes above are documented.
#
# use_monitor stays False regardless of the schedule interval -- this
# isn't specific to the 10-vs-30-minute question. "Catching up" on a
# missed tick has no real value for this workload: every loader's
# lookback window already covers everything since the last successful
# run regardless of how many ticks were skipped in between, so there's
# nothing a catch-up run would compute that the next natural one
# wouldn't anyway. Leaving use_monitor=False means that if a future cycle
# ever runs long for some other reason (e.g. a genuine telemetry volume
# spike, transient database contention), Azure just waits for the next
# natural tick instead of risking the same "immediate back-to-back
# catch-up re-triggers" pattern that use_monitor=True produced before --
# Singleton Lock still separately guarantees no actual overlapping runs,
# regardless of this setting.
@app.timer_trigger(
    schedule="0 */30 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=False,
)
def loadDeviceData(myTimer: func.TimerRequest) -> None:
    if ENVIRONMENT == "Dev":
        logging.info(
            "loadDeviceData: skipping timer-triggered run in Dev -- use "
            "loadDeviceDataManual instead."
        )
        return

    if myTimer.past_due:
        logging.warning("loadDeviceData: timer is past due!")

    logging.info("loadDeviceData: starting run.")
    load_leadsun_pole_models()
    load_leadsun_pole_telemetry()
    update_leadsun_project_details()
    load_leadsun_pole_timezones()
    load_leadsun_pole_daylight_flags()
    load_leadsun_pole_vitals()
    load_provisioned_pole_models()
    load_provisioned_pole_serials()
    load_provisioned_pole_timezones()
    load_provisioned_pole_daylight_flags()
    load_provisioned_pole_vitals()
    logging.info("loadDeviceData: run complete.")


# Manual trigger for testing outside the 30-minute schedule -- same
# Prod-blocking convention as loadAirTableDataManual, and unaffected by
# loadDeviceData's Dev-skip above -- in Dev, this is the only way to
# trigger a run at all.
@app.route(
    route="loadDeviceDataManual", methods=["POST"], auth_level=func.AuthLevel.FUNCTION
)
def loadDeviceDataManual(req: func.HttpRequest) -> func.HttpResponse:
    if ENVIRONMENT == "Prod":
        return func.HttpResponse("Manual trigger is disabled in Prod.", status_code=403)

    logging.info("loadDeviceDataManual: manual run triggered.")
    load_leadsun_pole_models()
    load_leadsun_pole_telemetry()
    update_leadsun_project_details()
    load_leadsun_pole_timezones()
    load_leadsun_pole_daylight_flags()
    load_leadsun_pole_vitals()
    load_provisioned_pole_models()
    load_provisioned_pole_serials()
    load_provisioned_pole_timezones()
    load_provisioned_pole_daylight_flags()
    load_provisioned_pole_vitals()
    logging.info("loadDeviceDataManual: run complete.")

    return func.HttpResponse(
        "loadPoleModels + loadPoleTelemetry + updateLeadsunProjectDetails + "
        "loadPoleTimeZones + loadPoleDaylightFlags + loadPoleVitals + "
        "loadProvisionedPoleModels + loadProvisionedPoleSerials + "
        "loadProvisionedPoleTimeZones + loadProvisionedPoleDaylightFlags + "
        "loadProvisionedPoleVitals run complete.",
        status_code=200,
    )


# --------------------------------------------------------------------------
# loadProvisionedPoleTelemetry -- EVENT-DRIVEN, not timer/HTTP-triggered
# like everything else in this file. Streetleaf's own provisioned poles
# push real-time telemetry to an Azure Event Hub (namespace
# ProvisionTestEventHub, event hub provisiontesteventhub); this function
# fires automatically whenever new messages arrive, rather than on a
# schedule this project controls. See shared/provisioned_telemetry_loader.py's
# own module docstring for the full message shape/field mapping.
#
# cardinality="many": receives a BATCH of messages per invocation (a
# Python list, even when Azure only had one message ready), not one
# message per invocation -- lets process_provisioned_telemetry_events()
# reuse the same chunked staging/MERGE upsert pattern every other loader
# here already uses, instead of a much less efficient single-row upsert
# per function invocation.
#
# connection="PROVISIONED_EVENT_HUB_CONNECTION_STRING": an app setting
# holding that Event Hub instance's own connection string (Azure's own
# per-Event-Hub connection string already includes
# "EntityPath=provisiontesteventhub", so event_hub_name below is
# supplied explicitly anyway, for clarity, not because the connection
# string itself is ambiguous without it).
#
# consumer_group="iothub-consumer-group": a DEDICATED consumer group for
# this function, not the shared $Default one -- so this function's own
# checkpoint position (how far it's read) never collides with any other
# reader of this same Event Hub, present or future. This consumer group
# must already exist on the Event Hub itself (Azure Portal -> the Event
# Hub -> Consumer Groups -> +Consumer Group) before this function can
# use it -- creating/naming it here in code doesn't provision it on the
# Event Hub side.
#
# No Dev-environment skip, unlike loadAirTableData/loadDeviceData --
# there's no equivalent "wait for a schedule" concept to skip; a message
# either exists to process or it doesn't, in every environment alike.
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="provisiontesteventhub",
    connection="PROVISIONED_EVENT_HUB_CONNECTION_STRING",
    consumer_group="iothub-consumer-group",
    cardinality="many",
)
def loadProvisionedPoleTelemetry(events: List[func.EventHubEvent]) -> None:
    parsed_events = [json.loads(event.get_body()) for event in events]
    logging.info(
        "loadProvisionedPoleTelemetry: received %d event(s).", len(parsed_events)
    )
    process_provisioned_telemetry_events(parsed_events)


# --------------------------------------------------------------------------
# getCustomers -- read-only API endpoint, NOT part of the Airtable/Leadsun
# ETL pipeline. Meant to be imported into Azure API Management and called
# by a website, not run on a schedule -- so unlike everything else in this
# file, it has no timer trigger, no SP_Execution tracking (it doesn't load
# or sync anything, just serves what's already been loaded), and no
# Dev-environment skip.
#
# SECURITY NOTE: this endpoint does NOT enforce any row-level access
# control -- e.g. it will NOT automatically restrict a "Customer Admin"
# caller to only their own customer just because the Users table has that
# relationship. It returns whatever customerId is asked for. If per-user
# scoping is needed, it has to happen either in an API Management policy
# (e.g. validating a JWT and rewriting/restricting the customerId param
# before it reaches this function) or in the calling website -- this
# function has no visibility into who's actually calling it beyond
# whether they have a valid function key.
#
# auth_level=FUNCTION (not ANONYMOUS): API Management would call this with
# the function key attached (as a named value / backend credential in its
# policy), so the Function App itself still isn't reachable by anyone who
# doesn't go through APIM (or doesn't have the key). ANONYMOUS would only
# be safe here if the Function App were also network-isolated so APIM is
# the sole path to it (e.g. via Private Endpoint) -- absent that, FUNCTION
# is the safer default.
@app.route(route="getCustomers", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def getCustomers(req: func.HttpRequest) -> func.HttpResponse:
    """
    Query params:
      customerId -- optional. If given, returns a single customer object
        (404 if not found) instead of an array.
      limit -- optional, default/max 1000 (see shared/api_utils.py).
        Ignored if customerId is given.
      active -- optional, "true"/"1" or "false"/"0" (case-insensitive).
        Filters to only customers whose Active column matches. Ignored
        if customerId is given (a single-Id lookup is a specific
        resource, not a filtered list). Omitted entirely -> no filter,
        same as before this param existed.
    """
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")
    active_param = req.params.get("active")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    active = None
    if active_param is not None:
        try:
            active = parse_bool_param(active_param, "active")
        except ValueError as ve:
            return func.HttpResponse(
                json.dumps({"error": str(ve)}),
                status_code=400,
                mimetype="application/json",
            )

    try:
        customers = get_customers(
            customer_id=customer_id,
            limit=int(limit_param) if limit_param else None,
            active=active,
        )
    except Exception as ex:
        logging.error("getCustomers: query failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    if customer_id:
        if not customers:
            return func.HttpResponse(
                json.dumps({"error": "customer not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            json.dumps(customers[0]), status_code=200, mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps(customers), status_code=200, mimetype="application/json"
    )


# --------------------------------------------------------------------------
# getProjects -- same pattern as getCustomers exactly (read-only, not part
# of the ETL pipeline, no SP_Execution tracking, no Dev-skip). See
# getCustomers's comment block above for the full reasoning -- repeated
# briefly here rather than cross-referenced, so this function is
# self-contained to read on its own.
#
# SECURITY NOTE: same as getCustomers -- no row-level access control
# enforced here either. Returns whatever projectId/customerId is asked for.
#
# auth_level=FUNCTION, same reasoning as getCustomers: API Management
# calls this with the function key attached; ANONYMOUS would only be safe
# with network isolation ensuring APIM is the sole path to the Function
# App.
@app.route(route="getProjects", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def getProjects(req: func.HttpRequest) -> func.HttpResponse:
    """
    Query params:
      projectId -- optional. If given, returns a single project object
        (404 if not found) instead of an array. Can be combined with
        customerId to also verify the project belongs to that customer.
      customerId -- optional. If given WITHOUT projectId, returns an
        array of every project for that customer -- a collection filter,
        not a single-resource lookup, so an empty array (200) means "this
        customer has no projects", not "not found" (no 404 here).
      limit -- optional, default/max 1000 (see shared/api_utils.py).
        Ignored if projectId is given.
      active -- optional, "true"/"1" or "false"/"0" (case-insensitive).
        Filters to only projects whose Active column matches. Applies to
        both the customerId-filtered list and the fully unfiltered list;
        ignored if projectId is given. Omitted entirely -> no filter,
        same as before this param existed.
    """
    project_id = req.params.get("projectId")
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")
    active_param = req.params.get("active")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    active = None
    if active_param is not None:
        try:
            active = parse_bool_param(active_param, "active")
        except ValueError as ve:
            return func.HttpResponse(
                json.dumps({"error": str(ve)}),
                status_code=400,
                mimetype="application/json",
            )

    try:
        projects = get_projects(
            project_id=project_id,
            customer_id=customer_id,
            limit=int(limit_param) if limit_param else None,
            active=active,
        )
    except Exception as ex:
        logging.error("getProjects: query failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    if project_id:
        if not projects:
            return func.HttpResponse(
                json.dumps({"error": "project not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            json.dumps(projects[0]), status_code=200, mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps(projects), status_code=200, mimetype="application/json"
    )


# --------------------------------------------------------------------------
# getPoleVitals -- same read-only/no-ETL-tracking pattern as getCustomers
# and getProjects, but a genuinely different SHAPE: not a straight table
# read, a Customer->Project rollup of pole health stats computed from
# Poles + PoleVitals. See shared/pole_vitals_api.py's module docstring
# for the full business-rule reasoning (the population/connected/faults/
# percentWorking rollup design, which PoleVitals period type drives it,
# how a pole with no recent data is handled).
#
# SECURITY NOTE: same as getCustomers/getProjects -- no row-level access
# control enforced here either.
@app.route(route="getPoleVitals", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def getPoleVitals(req: func.HttpRequest) -> func.HttpResponse:
    """
    Query params:
      projectId -- optional. If given, returns a single FLAT project
        object (customerId/customerName included on it directly, not
        nested) -- 404 if not found. Can be combined with customerId to
        also verify the project belongs to that customer.
      customerId -- optional. If given WITHOUT projectId, returns a
        single customer object with a nested "projects" array (one entry
        per project, including projects with zero poles) -- 404 if that
        customer doesn't exist. NOT an array -- a customerId always
        identifies at most one customer.
      limit -- optional, default/max 1000 (see shared/api_utils.py).
        Applies to how many CUSTOMERS are returned in the unfiltered
        case -- each returned customer still includes every one of their
        projects. Ignored if either id is given.
    """
    project_id = req.params.get("projectId")
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        result = get_pole_vitals(
            project_id=project_id,
            customer_id=customer_id,
            limit=int(limit_param) if limit_param else None,
        )
    except Exception as ex:
        logging.error("getPoleVitals: query failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    if project_id:
        if result is None:
            return func.HttpResponse(
                json.dumps({"error": "project not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            json.dumps(result), status_code=200, mimetype="application/json"
        )

    if customer_id:
        if result is None:
            return func.HttpResponse(
                json.dumps({"error": "customer not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            json.dumps(result), status_code=200, mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


# --------------------------------------------------------------------------
# getPoles -- same read-only/no-ETL-tracking pattern as getCustomers/
# getProjects/getPoleVitals. Each pole carries the exact same fields as a
# pole entry inside getPoleVitals's "poles" list (by explicit request),
# reusing that module's own SQL/field-mapping directly (see
# shared/poles_api.py's module docstring) rather than a second,
# independently-maintained copy -- plus one addition beyond that literal
# field set: projectId, since a flat, unfiltered pole list otherwise has
# no way to trace a given pole back to its project.
#
# SECURITY NOTE: same as getCustomers/getProjects/getPoleVitals -- no
# row-level access control enforced here either.
@app.route(route="getPoles", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def getPoles(req: func.HttpRequest) -> func.HttpResponse:
    """
    Query params:
      poleId -- optional. If given, returns a single pole object (404 if
        not found) instead of an array. Can be combined with projectId
        and/or customerId to also verify the pole belongs to that
        project/customer.
      projectId -- optional. If given WITHOUT poleId, returns an array
        of every pole belonging to that project (empty array, not 404,
        if the project has zero poles or doesn't exist).
      customerId -- optional. If given WITHOUT poleId, returns an array
        of every pole belonging to any of that customer's projects. Can
        be combined with projectId.
      limit -- optional, default/max 1000 (see shared/api_utils.py) --
        or default/max 20000 if summary=true. Applies to how many poles
        are returned in the fully unfiltered case. Ignored if poleId,
        projectId, or customerId is given.
      summary -- optional, "true"/"1" (case-insensitive) to enable.
        Uses a lighter query that omits lastUpdate/batteryVoltage1/
        batteryVoltage2 from each pole (see shared/poles_api.py's own
        notes on why) and raises the unfiltered case's limit ceiling --
        built for a "give me every pole" consumer (e.g. a map rendering
        all ~14K poles at once) that only needs location/status, not
        per-pole telemetry detail.
      active -- optional, "true"/"1" or "false"/"0" (case-insensitive).
        Filters to only poles whose Active column matches. Applies to
        the projectId-filtered list, the customerId-filtered list, and
        the fully unfiltered list; ignored if poleId is given. Omitted
        entirely -> no filter, same as before this param existed.
    """
    pole_id = req.params.get("poleId")
    project_id = req.params.get("projectId")
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")
    summary = (req.params.get("summary") or "").strip().lower() in ("true", "1")
    active_param = req.params.get("active")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    active = None
    if active_param is not None:
        try:
            active = parse_bool_param(active_param, "active")
        except ValueError as ve:
            return func.HttpResponse(
                json.dumps({"error": str(ve)}),
                status_code=400,
                mimetype="application/json",
            )

    try:
        result = get_poles(
            pole_id=pole_id,
            project_id=project_id,
            customer_id=customer_id,
            limit=int(limit_param) if limit_param else None,
            summary=summary,
            active=active,
        )
    except Exception as ex:
        logging.error("getPoles: query failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    if pole_id:
        if result is None:
            return func.HttpResponse(
                json.dumps({"error": "pole not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            json.dumps(result), status_code=200, mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


# --------------------------------------------------------------------------
# getUsers -- same read-only/no-ETL-tracking pattern as getCustomers/
# getProjects. Named "getUsers" (plural), not "getUser" as originally
# asked for, to match every other read endpoint in this project
# (getCustomers, getProjects, getPoles) -- easy to rename back if a
# singular name was actually wanted.
#
# SECURITY NOTE: same as every other read endpoint here -- no row-level
# access control enforced. This one is a bit more sensitive than the
# others, though: it's reading account data, not just pole/project
# metadata. get_users() already hard-excludes PasswordHash/ResetToken/
# ResetTokenExpiresAt at the SQL level (see shared/users_api.py), so
# there's no path for those to leak through this endpoint even if it's
# ever asked to return more fields later -- but the same lack of
# row-level access control as every other endpoint here still applies to
# whichever fields ARE returned (name/email/role/status/customer).
@app.route(route="getUsers", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def getUsers(req: func.HttpRequest) -> func.HttpResponse:
    """
    Query params:
      userId -- optional. If given, returns a single user object (404 if
        not found) instead of an array. Can be combined with customerId
        to also verify the user belongs to that customer.
      customerId -- optional. If given WITHOUT userId, returns an array
        of every user belonging to that customer (empty array, not 404,
        if that customer has zero users or doesn't exist) -- a
        collection filter, matching getProjects/getPoles' customerId
        convention.
      limit -- optional, default/max 1000 (see shared/api_utils.py).
        Applies to how many users are returned in the fully unfiltered
        case. Ignored if userId or customerId is given.
    """
    user_id = req.params.get("userId")
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        users = get_users(
            user_id=user_id,
            customer_id=customer_id,
            limit=int(limit_param) if limit_param else None,
        )
    except Exception as ex:
        logging.error("getUsers: query failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    if user_id:
        if not users:
            return func.HttpResponse(
                json.dumps({"error": "user not found"}),
                status_code=404,
                mimetype="application/json",
            )
        return func.HttpResponse(
            json.dumps(users[0]), status_code=200, mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps(users), status_code=200, mimetype="application/json"
    )


# --------------------------------------------------------------------------
# User management -- invite/register/sign-in/sign-out/forgot-reset-password/
# delete. Genuinely different in kind from every other endpoint above: these
# manage real user accounts and credentials, not read-only reporting data,
# and (unlike every getX endpoint here) most of them enforce their own
# application-level authentication/authorization on top of the Azure
# Function key every route already sits behind -- the function key alone
# only proves "this caller is the website's own backend", not "this caller
# is a specific, signed-in human with a specific role".
#
# FOLLOW-UP, not done as part of this change: getCustomers/getProjects/
# getPoles/getPoleVitals/getUsers above don't call require_auth() at all
# yet, so a Customer Admin's requests aren't actually scoped to their own
# CustomerId yet -- they can currently see every customer's data, same as a
# Streetleaf Admin would. Retrofitting that is a separate, contained change:
# call require_auth() at the top of each of those, then (when
# ctx.role != "Streetleaf Admin") filter/verify against ctx.customer_id the
# same way invite_user()/delete_user() already enforce their own
# Streetleaf-Admin-only restriction. Worth doing before this goes live for
# real Customer Admin users.


def _auth_error_response(ex: AuthError) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": str(ex)}),
        status_code=ex.status_code,
        mimetype="application/json",
    )


@app.route(route="inviteUser", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def inviteUser(req: func.HttpRequest) -> func.HttpResponse:
    """
    Creates a Pending user and emails them an invite link. Streetleaf
    Admin or Customer Admin only (see invite_user()'s own docstring for
    the full permission model: Streetleaf Admin can invite any role;
    Customer Admin can invite "Customer Admin"/"User" but not
    "Streetleaf Admin", and only for their own customer -- customerId is
    forced to their own regardless of what's passed, and an explicitly
    passed, mismatched one is rejected). Body: {"name": ..., "email":
    ..., "role": "Streetleaf Admin" | "Customer Admin" | "User",
    "customerId": ... (required for "Customer Admin"; optional for
    "User" -- present means a customer-side user, absent means a
    "Streetleaf User" with no customer association, same as Streetleaf
    Admin itself; ignored/forced to the caller's own for a Customer
    Admin caller either way, see above)}.
    """
    try:
        ctx = require_auth(req)
        body = req.get_json()
        result = invite_user(
            ctx,
            name=body.get("name"),
            email=body.get("email"),
            role=body.get("role"),
            customer_id=body.get("customerId"),
        )
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("inviteUser: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=201, mimetype="application/json"
    )


@app.route(route="resendInvite", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def resendInvite(req: func.HttpRequest) -> func.HttpResponse:
    """
    Streetleaf-Admin-only: re-sends an invite email to an existing
    Pending user, refreshing their token/expiry in place (see
    resend_invite()'s own docstring for why this is preferred over a
    deleteUser()-then-inviteUser() round trip). 409 if the target user
    is already Active (nothing left to resend), 404 if the user doesn't
    exist at all. Body: {"userId": ...}.
    """
    try:
        ctx = require_auth(req)
        body = req.get_json()
        result = resend_invite(ctx, target_user_id=body.get("userId"))
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("resendInvite: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


@app.route(route="registerUser", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def registerUser(req: func.HttpRequest) -> func.HttpResponse:
    """
    Public (no signed-in caller yet -- the invite token itself is the
    authorization for this one-time action). Body: {"token": ...,
    "password": ...}. Completes account setup and signs the new user in
    (returns a session token), same shape as signIn's response.
    """
    try:
        body = req.get_json()
        result = register_user(token=body.get("token"), password=body.get("password"))
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("registerUser: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


@app.route(route="signIn", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def signIn(req: func.HttpRequest) -> func.HttpResponse:
    """
    Public. Body: {"email": ..., "password": ...}. Deliberately generic
    401 for every failure reason (wrong password, no such email, account
    not Active yet) -- see users_management_api.py's own docstring for
    why this isn't a bug.
    """
    try:
        body = req.get_json()
        result = sign_in(email=body.get("email"), password=body.get("password"))
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("signIn: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


@app.route(route="signOut", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def signOut(req: func.HttpRequest) -> func.HttpResponse:
    """Requires Authorization: Bearer <token>. Revokes the caller's own
    current session -- immediately, not just client-side token disposal."""
    try:
        ctx = require_auth(req)
        sign_out(ctx)
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("signOut: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps({"success": True}), status_code=200, mimetype="application/json"
    )


@app.route(route="forgotPassword", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def forgotPassword(req: func.HttpRequest) -> func.HttpResponse:
    """
    Public. Body: {"email": ...}. ALWAYS returns the same generic
    success message regardless of whether that email actually exists in
    the system -- do not change this to report failure differently for
    an unrecognized email; that would defeat the whole anti-enumeration
    point of this endpoint.
    """
    try:
        body = req.get_json()
        forgot_password(email=body.get("email"))
    except Exception as ex:
        # Deliberately not distinguishing AuthError from anything else
        # here, and deliberately not letting ANY failure mode change
        # this response -- forgot_password() itself never raises for a
        # normal "no such email" case (see its own docstring), so
        # reaching this except block at all means something genuinely
        # unexpected happened. Still logged, but the response to the
        # caller stays identical either way.
        logging.error("forgotPassword: unexpected error: %s", ex)

    return func.HttpResponse(
        json.dumps({"message": "If that email exists, a reset link has been sent."}),
        status_code=200,
        mimetype="application/json",
    )


@app.route(route="resetPassword", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def resetPassword(req: func.HttpRequest) -> func.HttpResponse:
    """
    Public (the reset token itself is the authorization). Body:
    {"token": ..., "newPassword": ...}. Also revokes every one of that
    user's currently-active sessions -- see reset_password()'s own
    docstring for why.
    """
    try:
        body = req.get_json()
        reset_password(token=body.get("token"), new_password=body.get("newPassword"))
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("resetPassword: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps({"success": True}), status_code=200, mimetype="application/json"
    )


@app.route(route="deleteUser", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def deleteUser(req: func.HttpRequest) -> func.HttpResponse:
    """
    PERMANENTLY removes a user (a hard delete, not a deactivation -- see
    delete_user()'s own docstring for the earlier soft-delete design
    this replaced, and why it's not reversible) and revokes their active
    sessions. Streetleaf Admin or Customer Admin only -- see
    delete_user()'s own docstring for the full permission model
    (Streetleaf Admin can delete any Streetleaf Admin/Customer Admin/
    User except itself; Customer Admin can delete a Customer Admin/User
    within their own customer only, never a Streetleaf Admin; no caller
    can ever delete their own account). Query param: ?userId=X.
    """
    try:
        ctx = require_auth(req)
        target_user_id = req.params.get("userId")
        delete_user(ctx, target_user_id)
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("deleteUser: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps({"success": True}), status_code=200, mimetype="application/json"
    )


@app.route(route="changeRole", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def changeRole(req: func.HttpRequest) -> func.HttpResponse:
    """
    Toggles a user's role between an Admin role and 'User', keeping them
    within the same organization (Streetleaf or a specific Customer) --
    see change_role()'s own docstring for exactly how the new role is
    determined and revokes the target's active sessions immediately.
    Streetleaf Admin or Customer Admin only -- see change_role()'s own
    docstring for the full permission model (identical in structure to
    deleteUser's own: Streetleaf Admin can change any Streetleaf
    Admin/Customer Admin/User except itself; Customer Admin can change a
    Customer Admin/User within their own customer only, never a
    Streetleaf Admin; no caller can ever change their own role). Body:
    {"userId": ...}.
    """
    try:
        ctx = require_auth(req)
        body = req.get_json()
        result = change_role(ctx, target_user_id=body.get("userId"))
    except AuthError as ex:
        return _auth_error_response(ex)
    except Exception as ex:
        logging.error("changeRole: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


# --------------------------------------------------------------------------
# getPoleVitalsByPeriod -- a different shape from getPoleVitals: a single
# pole's FULL HISTORY of PoleVitals rows for a SPECIFIC, caller-chosen
# period type, each read directly -- no rollup, no averaging across rows,
# no aggregation across a window at all. batteryVoltage1/batteryVoltage2
# are not included (dropped per explicit request, along with the
# PoleTelemetry join that would otherwise be needed for them). Use this
# to see every one of this pole's own Hour buckets (or Day buckets) as-is;
# use getPoleVitals/getPoles for a steadier current-status signal (which
# reads each pole's single Last48Hours row instead).
@app.route(
    route="getPoleVitalsByPeriod", methods=["GET"], auth_level=func.AuthLevel.FUNCTION
)
def getPoleVitalsByPeriod(req: func.HttpRequest) -> func.HttpResponse:
    """
    Query params:
      poleId -- required.
      periodType -- required. 'Hour' or 'Day' only -- Last48Hours is
        excluded (it's a single current-state row per pole, not a history
        to page through; see pole_vitals_api.py's own module docstring).
      limit -- optional, default/max per shared/api_utils.py. Max number
        of history entries returned, most-recent-first. Hour keeps at
        most 168 rows per pole and Day keeps 7 (see
        pole_vitals_loader.py's own retention pruning), so this is
        already implicitly bounded even without a limit.

    Returns the pole's static info (id, poleNumber, locationId,
    installDate, lat, long, lastUpdate) plus a "vitals" list, one entry
    per PoleVitals row (periodStart, periodEnd, isOnline, isLedFault,
    isBatteryFault, isPanelFault, isOpenIssueFault, isPoleFault,
    avgBatteryPercentage, avgPanelPercentage, avgLightPercentage).

    404 only if no pole exists with that id. If the pole exists but has
    no PoleVitals rows of the requested periodType yet, "vitals" comes
    back as an empty list, not a 404 -- the pole itself was found, it
    just has no history yet for that period type.
    """
    pole_id = req.params.get("poleId")
    period_type = req.params.get("periodType")
    limit_param = req.params.get("limit")

    if not pole_id:
        return func.HttpResponse(
            json.dumps({"error": "poleId is required"}),
            status_code=400,
            mimetype="application/json",
        )
    if not period_type:
        return func.HttpResponse(
            json.dumps({"error": "periodType is required"}),
            status_code=400,
            mimetype="application/json",
        )
    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        result = get_pole_vitals_by_period(
            pole_id=pole_id,
            period_type=period_type,
            limit=int(limit_param) if limit_param else None,
        )
    except ValueError as ex:
        return func.HttpResponse(
            json.dumps({"error": str(ex)}), status_code=400, mimetype="application/json"
        )
    except Exception as ex:
        logging.error("getPoleVitalsByPeriod: query failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    if result is None:
        return func.HttpResponse(
            json.dumps({"error": "pole not found"}),
            status_code=404,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )


@app.route(route="setPoleLights", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def setPoleLights(req: func.HttpRequest) -> func.HttpResponse:
    """
    Remotely turns light(s) on/off/dim. Body (JSON) needs EXACTLY ONE of
    the four scope fields below, plus brightness/time:
      {"poleNumber": "...", "brightness": 0-100, "time": <int>}
        -- one specific pole, looked up by its human-facing PoleNumber
           (NOT its Id), e.g. "12009-1000-A".
      {"poleNumbers": ["...", "..."], "brightness": 0-100, "time": <int>}
        -- a specific LIST of poles, possibly spanning different groups
           and/or different Leadsun accounts (see
           shared/pole_remote_control.py's set_pole_lights() docstring
           for how multi-account lists are handled and how the response
           shape differs in that case).
      {"gatewayCode": "...", "brightness": 0-100, "time": <int>}
        -- every currently-reporting pole under that Leadsun gateway.
      {"projectId": "...", "brightness": 0-100, "time": <int>}
        -- every currently-reporting pole across every gateway in that
           project (THIS project's own Projects.Id, not Leadsun's
           internal numeric project id).

    brightness and time are forwarded to Leadsun EXACTLY as given --
    this endpoint doesn't define what "on" or "off" means (e.g. whether
    brightness=0 is "off" and what unit/meaning "time" carries); the
    caller decides that on every call. See shared/pole_remote_control.py
    and shared/leadsun_edge_client.py's own module docstrings for the
    full flow this wires together (telemetry lookup -> Leadsun EDGE
    login/token refresh -> the actual remote-command call, sent as ONE
    request per Leadsun account covering every matched pole under it).
    """
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "request body must be valid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    pole_number = body.get("poleNumber")
    pole_numbers = body.get("poleNumbers")
    gateway_code = body.get("gatewayCode")
    project_id = body.get("projectId")
    brightness = body.get("brightness")
    time_minutes = body.get("time")

    scope_fields_given = [
        name
        for name, value in (
            ("poleNumber", pole_number),
            ("poleNumbers", pole_numbers),
            ("gatewayCode", gateway_code),
            ("projectId", project_id),
        )
        if value
    ]
    if len(scope_fields_given) != 1:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": "exactly one of poleNumber, poleNumbers, gatewayCode, "
                    "or projectId is required"
                }
            ),
            status_code=400,
            mimetype="application/json",
        )
    if pole_numbers is not None and (
        not isinstance(pole_numbers, list)
        or not pole_numbers
        or not all(isinstance(pn, str) and pn for pn in pole_numbers)
    ):
        return func.HttpResponse(
            json.dumps({"error": "poleNumbers must be a non-empty array of non-empty strings"}),
            status_code=400,
            mimetype="application/json",
        )
    if not isinstance(brightness, int) or isinstance(brightness, bool) or not (0 <= brightness <= 100):
        return func.HttpResponse(
            json.dumps({"error": "brightness is required and must be an integer 0-100"}),
            status_code=400,
            mimetype="application/json",
        )
    if not isinstance(time_minutes, int) or isinstance(time_minutes, bool) or time_minutes < 0:
        return func.HttpResponse(
            json.dumps({"error": "time is required and must be a non-negative integer"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        result = set_pole_lights(
            pole_number=pole_number,
            pole_numbers=pole_numbers,
            gateway_code=gateway_code,
            project_id=project_id,
            brightness=brightness,
            time_minutes=time_minutes,
        )
    except (
        PoleNotFoundError,
        PoleTelemetryNotFoundError,
        PoleNumbersNotResolvedError,
        GatewayNotFoundError,
        ProjectNotFoundError,
        ProjectTelemetryNotFoundError,
    ) as ex:
        return func.HttpResponse(
            json.dumps({"error": str(ex)}), status_code=404, mimetype="application/json"
        )
    except ProjectHasNoLeadsunIdError as ex:
        return func.HttpResponse(
            json.dumps({"error": str(ex)}), status_code=400, mimetype="application/json"
        )
    except LeadsunEdgeAccountNotFoundError as ex:
        logging.error("setPoleLights: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "no Leadsun EDGE account configured for this scope"}),
            status_code=500,
            mimetype="application/json",
        )
    except LeadsunEdgeApiError as ex:
        logging.error("setPoleLights: Leadsun EDGE API error: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "Leadsun EDGE API request failed"}),
            status_code=502,
            mimetype="application/json",
        )
    except Exception as ex:
        logging.error("setPoleLights: failed: %s", ex)
        return func.HttpResponse(
            json.dumps({"error": "internal error"}),
            status_code=500,
            mimetype="application/json",
        )

    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json"
    )
