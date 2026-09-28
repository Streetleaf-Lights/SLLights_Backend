"""
Shared Eastern-time helpers used by the Airtable loaders (customers_loader,
projects_loader, ...).

pyodbc silently converts timezone-aware datetime objects to UTC (offset
+00:00) when binding them as parameters -- the wall-clock hour ends up
right, but the offset gets discarded. to_dto_string() formats datetimes as
explicit offset strings instead, which SQL Server parses directly,
preserving the correct Eastern offset.
"""

import re
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


def parse_dto_string(value):
    """
    Parses a value that MAY be a to_dto_string()-shaped string (e.g.
    '2026-07-02 14:14:39.901 -04:00') back into an aware datetime.
    Accepts a native datetime too, passed straight through -- pyodbc's
    behavior for DATETIMEOFFSET-typed expressions varies (a genuine
    DATETIMEOFFSET column often comes back as an already-aware datetime
    object; the "AT TIME ZONE" computed-expression form this project's
    own pole-vitals queries use to convert LastUpload into each pole's
    own local offset has been observed coming back as TEXT instead, in
    exactly this string shape), so callers reading either kind of value
    can use this unconditionally rather than needing to know which one
    they'll get. None passes through as None.

    datetime.fromisoformat() alone can't parse this string as-is: it
    accepts a space between date and time, and it accepts an offset with
    no space before it, but the COMBINATION this exact format uses --
    3-digit milliseconds followed by a SPACE before the offset -- trips
    it up (confirmed directly: '...123 -04:00' raises ValueError,
    '...123-04:00' and '...123456 -04:00' both parse fine). Rather than
    depend on that combination continuing to fail or start working
    across future Python versions, the space right before the offset is
    stripped unconditionally before handing off to fromisoformat().
    """
    if value is None or isinstance(value, datetime):
        return value
    normalized = re.sub(r" ([+-]\d{2}:\d{2})$", r"\1", value.strip())
    return datetime.fromisoformat(normalized)


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


def airtable_date_to_eastern(value: str | None) -> str | None:
    """
    Converts an Airtable date or datetime field to an Eastern-time
    DATETIMEOFFSET string.

    Airtable date fields return a plain date string ('2026-09-01');
    datetime fields return an ISO 8601 UTC string ('2026-09-01T14:30:00.000Z').
    Plain dates are treated as midnight UTC before conversion.
    Returns None for null/empty values.
    """
    if not value:
        return None

    # Datetime string (has 'T') -- parse as UTC
    if "T" in value:
        utc_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        # Plain date -- treat as midnight UTC
        from datetime import timezone
        utc_dt = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)

    eastern_dt = utc_dt.astimezone(EASTERN)
    return to_dto_string(eastern_dt)
