import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import azure.functions as func

from shared.customers_loader import load_customers

app = func.FunctionApp()

EASTERN = ZoneInfo("America/New_York")
TARGET_HOURS = {6, 18}  # 6 AM and 6 PM Eastern, DST-proof
ENVIRONMENT = os.environ.get("ENVIRONMENT", "Dev")


# Flex Consumption runs on Linux, where NCRONTAB schedules always evaluate in
# UTC (WEBSITE_TIME_ZONE is ignored on Linux). To land reliably at 6 AM and
# 6 PM Eastern year-round without touching the cron expression across DST
# changes, this fires every hour on the hour and only does real work when the
# current Eastern-time hour matches one of TARGET_HOURS.
@app.timer_trigger(
    schedule="0 0 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=True,
)
def loadAirTableData(myTimer: func.TimerRequest) -> None:
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

    # Only Customers for now. Add these back in as they're built out:
    load_customers()
    # load_projects()
    # load_poles()
    # load_pole_statuses()

    logging.info("loadAirTableData: run complete.")


# Manual trigger for testing outside the scheduled hours -- run it anytime with:
#   func start  (locally), then:
#   curl -X POST http://localhost:7071/api/loadAirTableDataManual
# or, once deployed to a non-Prod slot, POST to the deployed URL with the
# function key. Blocked outright in Prod so it can't accidentally be hit there.
@app.route(
    route="loadAirTableDataManual", methods=["POST"], auth_level=func.AuthLevel.FUNCTION
)
def loadAirTableDataManual(req: func.HttpRequest) -> func.HttpResponse:
    if ENVIRONMENT == "Prod":
        return func.HttpResponse("Manual trigger is disabled in Prod.", status_code=403)

    logging.info("loadAirTableDataManual: manual run triggered.")
    load_customers()
    logging.info("loadAirTableDataManual: run complete.")

    return func.HttpResponse("loadCustomers run complete.", status_code=200)
