"""
Shared Eastern-time helpers used by the Airtable loaders (customers_loader,
projects_loader, ...).

pyodbc silently converts timezone-aware datetime objects to UTC (offset
+00:00) when binding them as parameters -- the wall-clock hour ends up
right, but the offset gets discarded. to_dto_string() formats datetimes as
explicit offset strings instead, which SQL Server parses directly,
preserving the correct Eastern offset.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def now_eastern() -> datetime:
    """Current time as an aware datetime in America/New_York (handles EST/EDT automatically)."""
    return datetime.now(EASTERN)


def to_dto_string(dt: datetime) -> str:
    """
    Formats an aware datetime as an explicit DATETIMEOFFSET literal string,
    e.g. '2026-07-02 14:14:39.901 -04:00'.
    """
    offset = dt.strftime("%z")  # e.g. '-0400'
    offset_fmt = f"{offset[:3]}:{offset[3:]}"  # '-04:00'
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " " + offset_fmt


def airtable_created_time_to_eastern(created_time: str | None) -> str | None:
    """
    Converts Airtable's createdTime (always UTC, e.g. '2025-11-17T19:56:44.000Z')
    into an Eastern-time DATETIMEOFFSET string, e.g. '2025-11-17 14:56:44.000 -05:00'.
    """
    if not created_time:
        return None

    utc_dt = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
    eastern_dt = utc_dt.astimezone(EASTERN)
    return to_dto_string(eastern_dt)
