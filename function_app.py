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
