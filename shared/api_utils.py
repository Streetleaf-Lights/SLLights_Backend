"""
Shared utilities for the read-only query API endpoints (getCustomers,
getProjects, and any future get<Table>() following the same pattern) --
factored out here once a second endpoint needed the exact same logic,
rather than duplicated per-module with the risk of the copies drifting
apart later.
"""

MAX_LIMIT = 1000

# No-limit-specified means "everything, up to MAX_LIMIT" -- these are
# business data tables (customers, projects), not high-volume telemetry,
# so an arbitrarily lower default just means silently truncated results
# for anyone who doesn't know to pass ?limit= explicitly. Learned this the
# hard way with getCustomers's original DEFAULT_LIMIT=100 -- fixed once,
# here, so every endpoint built on this module starts from the corrected
# default rather than repeating that mistake.
DEFAULT_LIMIT = MAX_LIMIT


def json_safe(value):
    """
    pyodbc can return types (datetime, Decimal, etc.) that aren't
    natively JSON-serializable via json.dumps(). Converts anything that
    isn't already a safe type to a plain string; passes everything else
    through unchanged.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def clamp_limit(limit) -> int:
    """Keeps limit within [1, MAX_LIMIT], defaulting to DEFAULT_LIMIT for
    None/invalid input -- a caller can't request an unbounded result set
    no matter what they pass."""
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))
