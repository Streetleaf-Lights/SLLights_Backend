import logging

from timezonefinder import TimezoneFinder

# SQL Server's AT TIME ZONE expects Windows timezone names (e.g.
# "Eastern Standard Time"), NOT IANA/Olson names (e.g. "America/New_York")
# -- these are two different naming systems. timezonefinder (and Python's
# zoneinfo generally) only speaks IANA, so every IANA name this project
# could plausibly encounter needs an explicit mapping to its Windows
# equivalent before it can be used in a T-SQL AT TIME ZONE clause.
#
# Deliberately scoped to the US + territories, not a global mapping --
# this is a US pole-telemetry business (matches the Eastern-time
# scheduling and Brevard County/FL sample data seen elsewhere in this
# project), so building out a full global IANA<->Windows table (hundreds
# of zones) isn't warranted for what's actually needed. A coordinate that
# resolves to an IANA zone outside this list is treated as unmapped (see
# resolve_windows_timezone()) rather than guessed at.
#
# Source: the standard IANA<->Windows correspondences published in
# Unicode CLDR's windowsZones.xml -- the same reference data most
# cross-platform timezone tooling is built from.
IANA_TO_WINDOWS = {
    "America/New_York": "Eastern Standard Time",
    "America/Detroit": "Eastern Standard Time",
    "America/Indiana/Indianapolis": "US Eastern Standard Time",
    "America/Indiana/Vincennes": "Eastern Standard Time",
    "America/Indiana/Winamac": "Eastern Standard Time",
    "America/Indiana/Petersburg": "Eastern Standard Time",
    "America/Indiana/Marengo": "US Eastern Standard Time",  # groups with Indianapolis's own historical no-DST-until-2006 quirk, not the plain Eastern group
    "America/Indiana/Vevay": "US Eastern Standard Time",  # same historical group as Marengo/Indianapolis, not plain Eastern
    "America/Kentucky/Louisville": "Eastern Standard Time",  # CLDR lists this under its legacy alias "America/Louisville", not this canonical IANA name -- same zone
    "America/Kentucky/Monticello": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Indiana/Knox": "Central Standard Time",
    "America/Indiana/Tell_City": "Central Standard Time",
    "America/Menominee": "Central Standard Time",
    "America/North_Dakota/Beulah": "Central Standard Time",
    "America/North_Dakota/Center": "Central Standard Time",
    "America/North_Dakota/New_Salem": "Central Standard Time",
    "America/Denver": "Mountain Standard Time",
    "America/Boise": "Mountain Standard Time",
    "America/Phoenix": "US Mountain Standard Time",  # Arizona -- no DST, distinct Windows zone
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Anchorage": "Alaskan Standard Time",
    "America/Juneau": "Alaskan Standard Time",
    "America/Sitka": "Alaskan Standard Time",
    "America/Metlakatla": "Alaskan Standard Time",
    "America/Yakutat": "Alaskan Standard Time",
    "America/Nome": "Alaskan Standard Time",
    "America/Adak": "Aleutian Standard Time",
    "Pacific/Honolulu": "Hawaiian Standard Time",
    "America/Puerto_Rico": "SA Western Standard Time",  # Atlantic Standard Time, no DST
}

_tf = TimezoneFinder()

# (0.0, 0.0) -- "Null Island", where the equator meets the prime meridian
# in the Gulf of Guinea -- is a well-known placeholder value GPS hardware
# reports when it hasn't acquired a real fix yet, not a location any
# actual pole would ever be at. timezonefinder correctly resolves it to
# "Etc/GMT", but treating that as a genuine location needing a Windows
# mapping would be the wrong fix for what's actually a data-quality
# issue (missing GPS fix), not a missing mapping.
_NULL_ISLAND = (0.0, 0.0)

_VALID_LATITUDE_RANGE = (-90.0, 90.0)
_VALID_LONGITUDE_RANGE = (-180.0, 180.0)


def resolve_iana_timezone(latitude: float, longitude: float) -> str:
    """
    Returns the IANA timezone name (e.g. "America/New_York") for a given
    coordinate, or None if it can't be resolved (e.g. coordinates over
    open ocean, out-of-range/corrupted values, or genuinely missing
    input).

    Deliberately does NOT attempt to auto-correct an out-of-range value
    (e.g. by guessing it's off by a factor of 1,000,000 and dividing) --
    a wrong guess would silently assign the wrong timezone, which is
    worse than just failing safely and falling back to a default. This
    is a data-quality problem to flag, not one to silently paper over.
    """
    if latitude is None or longitude is None:
        return None

    if not (_VALID_LATITUDE_RANGE[0] <= latitude <= _VALID_LATITUDE_RANGE[1]) or not (
        _VALID_LONGITUDE_RANGE[0] <= longitude <= _VALID_LONGITUDE_RANGE[1]
    ):
        logging.warning(
            "pole_timezones: lat=%s, lng=%s is outside the valid coordinate "
            "range (latitude -90..90, longitude -180..180) -- likely "
            "corrupted or incorrectly-scaled GPS data (e.g. a device "
            "reporting micro-degrees -- degrees x 1,000,000 -- without "
            "converting back to plain decimal degrees) rather than a "
            "resolvable location. Skipping timezone resolution for it.",
            latitude,
            longitude,
        )
        return None

    try:
        result = _tf.timezone_at(lat=latitude, lng=longitude)
    except ValueError as ex:
        # Defense in depth: the explicit range check above should catch
        # the known "out of range" case before ever reaching here, but
        # this guards against anything else timezonefinder itself
        # rejects that isn't anticipated above, so a single bad
        # coordinate can never propagate as an unhandled exception and
        # get retried (and re-fail, and re-log) every single cycle.
        logging.warning(
            "pole_timezones: timezonefinder rejected lat=%s, lng=%s: %s -- "
            "skipping timezone resolution for it.",
            latitude,
            longitude,
            ex,
        )
        return None

    if result is None:
        # Valid-range coordinates that still don't map to any timezone --
        # e.g. genuinely far out in open ocean, away from any boundary.
        logging.warning(
            "pole_timezones: no timezone could be resolved for lat=%s, lng=%s "
            "(valid coordinates, but not within any known timezone boundary -- "
            "e.g. open ocean).",
            latitude,
            longitude,
        )
    return result


def resolve_windows_timezone(latitude: float, longitude: float) -> tuple:
    """
    Returns (iana_name, windows_name) for a given coordinate. windows_name
    is None if the coordinate couldn't be resolved to any timezone at
    all, is the Null Island placeholder, or resolves to an IANA zone
    outside IANA_TO_WINDOWS -- callers should treat any of these as
    "couldn't resolve" (log it, fall back to a default) rather than
    guessing, since a wrong timezone silently mis-buckets that pole's
    Hour/Day/Month vitals.
    """
    if latitude is None or longitude is None:
        return None, None

    if (latitude, longitude) == _NULL_ISLAND:
        logging.warning(
            "pole_timezones: lat=0.0, lng=0.0 (\"Null Island\") is a common "
            "placeholder for a missing/not-yet-acquired GPS fix, not a real "
            "location -- skipping timezone resolution for it rather than "
            "reporting a misleading 'add a new IANA_TO_WINDOWS mapping' "
            "warning. If this coordinate is ever genuinely correct for a "
            "real pole, this check will need revisiting."
        )
        return None, None

    iana_name = resolve_iana_timezone(latitude, longitude)
    if iana_name is None:
        # resolve_iana_timezone() already logged a specific reason (out of
        # range, or whatever timezonefinder itself rejected) -- nothing
        # more to add here.
        return None, None

    windows_name = IANA_TO_WINDOWS.get(iana_name)
    if windows_name is None:
        logging.error(
            "pole_timezones: resolved IANA zone '%s' (from lat=%s, lng=%s) has no "
            "known Windows timezone mapping -- add it to IANA_TO_WINDOWS if this "
            "location is expected.",
            iana_name,
            latitude,
            longitude,
        )
    return iana_name, windows_name
