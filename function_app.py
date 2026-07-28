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
from shared.pole_timezones_loader import load_pole_timezones
from shared.pole_daylight_flags_loader import load_pole_daylight_flags
from shared.pole_vitals_loader import load_pole_vitals
from shared.customers_api import get_customers
from shared.projects_api import get_projects
from shared.pole_vitals_api import get_pole_vitals
from shared.poles_api import get_poles
from shared.users_api import get_users

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
# Models -> Telemetry -> TimeZones -> DaylightFlags -> Vitals: PoleModels
# is a device-model reference table needed by PoleVitals' Panel/Light
# percentage formulas (SunboardPower/LightPower), PoleTelemetry is the raw
# readings PoleVitals aggregates, PoleTimeZones resolves each pole's own
# timezone (from that same fresh telemetry's Longitude/Latitude) so
# PoleVitals can bucket in each pole's local time instead of assuming
# Eastern for every pole regardless of where it actually is,
# DaylightFlags computes/caches whether each not-yet-flagged reading
# happened during daylight (needed for PoleVitals' LightStatus column,
# using PoleTimeZones' coordinates -- see that loader's module docstring
# for why this can't just be inline SQL), and PoleVitals depends on all
# four already being current for this cycle.
#
# schedule: reverted from every 30 minutes back to every 10, now that
# Week/Month have been removed from loadPoleVitals entirely (see that
# loader's module docstring and the README for the full history) -- Week
# alone was the dominant cost of every run, ~15+ minutes out of a
# ~20-25 minute total, which is what originally motivated widening this
# to 30 minutes in the first place (a 10-minute schedule can't keep up
# with a 20-25 minute run). With Week/Month gone, a normal cycle should
# comfortably fit back within 10 minutes.
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
    schedule="0 */10 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=False,
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
    load_pole_timezones()
    load_pole_daylight_flags()
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
    load_pole_timezones()
    load_pole_daylight_flags()
    load_pole_vitals()
    logging.info("loadLeadsunDataManual: run complete.")

    return func.HttpResponse(
        "loadPoleModels + loadPoleTelemetry + loadPoleTimeZones + loadPoleDaylightFlags + "
        "loadPoleVitals run complete.",
        status_code=200,
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


# --------------------------------------------------------------------------
# getPoleVitals -- same read-only/no-ETL-tracking pattern as getCustomers
# and getProjects, but a genuinely different SHAPE: not a straight table
# read, a Customer->Project rollup of pole health stats computed from
# Poles + PoleVitals. See shared/pole_vitals_api.py's module docstring
# for the full business-rule reasoning (which LightStatus values count
# as "working", which PoleVitals period type drives this, how an
# unclassified pole is handled).
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
    """
    pole_id = req.params.get("poleId")
    project_id = req.params.get("projectId")
    customer_id = req.params.get("customerId")
    limit_param = req.params.get("limit")
    summary = (req.params.get("summary") or "").strip().lower() in ("true", "1")

    if limit_param is not None and not limit_param.isdigit():
        return func.HttpResponse(
            json.dumps({"error": "limit must be a positive integer"}),
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
