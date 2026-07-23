import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import azure.functions as func

from shared.customers_loader import load_customers
from shared.projects_loader import load_projects
from shared.poles_loader import load_poles
from shared.pole_models_loader import load_pole_models
from shared.pole_telemetry_loader import load_pole_telemetry
from shared.pole_vitals_loader import load_pole_vitals
from shared.customers_api import get_customers
from shared.projects_api import get_projects

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
    # load_pole_statuses()

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
    logging.info("loadAirTableDataManual: run complete.")

    return func.HttpResponse("loadPoles + loadProjects + loadCustomers run complete.", status_code=200)


# Separate from loadAirTableData on purpose -- different source (Leadsun,
# not Airtable), different cadence (every 10 minutes, not twice a day), and
# no dependency between the two: this pipeline doesn't join against
# Poles/Projects/Customers, so there's no load-order concern with the
# Airtable pipeline either way.
#
# Renamed from loadPoleRawData now that it orchestrates two loaders, not
# one -- mirrors loadAirTableData's naming (source name + "Data" as the
# umbrella, individual load_<x>() functions underneath). Load order is
# Models -> Telemetry -> Vitals: PoleModels is a device-model reference
# table needed by PoleVitals' Panel/Light percentage formulas (SunboardPower/
# LightPower), PoleTelemetry is the raw readings PoleVitals aggregates, and
# PoleVitals depends on both already being current for this cycle.
@app.timer_trigger(
    schedule="0 */10 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=True,
)
def loadLeadsunData(myTimer: func.TimerRequest) -> None:
    if ENVIRONMENT == "Dev":
        logging.info(
            "loadLeadsunData: skipping timer-triggered run in Dev -- use "
            "loadLeadsunDataManual instead."
        )
        return

    if myTimer.past_due:
        logging.warning("loadLeadsunData: timer is past due!")

    logging.info("loadLeadsunData: starting run.")
    load_pole_models()
    load_pole_telemetry()
    load_pole_vitals()
    logging.info("loadLeadsunData: run complete.")


# Manual trigger for testing outside the 10-minute schedule -- same
# Prod-blocking convention as loadAirTableDataManual, and unaffected by
# loadLeadsunData's Dev-skip above -- in Dev, this is the only way to
# trigger a run at all.
@app.route(
    route="loadLeadsunDataManual", methods=["POST"], auth_level=func.AuthLevel.FUNCTION
)
def loadLeadsunDataManual(req: func.HttpRequest) -> func.HttpResponse:
    if ENVIRONMENT == "Prod":
        return func.HttpResponse("Manual trigger is disabled in Prod.", status_code=403)

    logging.info("loadLeadsunDataManual: manual run triggered.")
    load_pole_models()
    load_pole_telemetry()
    load_pole_vitals()
    logging.info("loadLeadsunDataManual: run complete.")

    return func.HttpResponse(
        "loadPoleModels + loadPoleTelemetry + loadPoleVitals run complete.", status_code=200
    )


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
    """
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        customers = get_customers(
            customer_id=customer_id, limit=int(limit_param) if limit_param else None
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
        projects = get_projects(
            project_id=project_id,
            customer_id=customer_id,
            limit=int(limit_param) if limit_param else None,
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
