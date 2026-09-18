"""
set_pole_lights() -- the single entry point for remotely turning
light(s) on/off/dim, at one of three scopes (exactly one given per
call):
  pole_number  -- one specific pole
  gateway_code -- every currently-reporting pole under that gateway
  project_id   -- every currently-reporting pole across every gateway in
                  that project (THIS project's own Projects.Id, not
                  Leadsun's internal numeric project id -- resolved
                  internally via Projects.LeadsunProject's own
                  "ProjectId" value, same JSON_VALUE lookup
                  pole_telemetry_loader.py's own
                  update_leadsun_project_details() already uses)

Wires together:
  1. shared/sql_client.py     -- look up matching telemetry
     (UserName/GroupId/GatewayCode/ControllerCode per pole) and the
     Leadsun EDGE account password for whichever UserName that
     telemetry belongs to
  2. shared/encryption_utils.py -- decrypt that password
  3. shared/leadsun_edge_client.py -- get/refresh a bearer token, then
     send ONE remote-command call covering every matched pole -- Leadsun
     EDGE's own request shape already supports multiple groups and
     multiple products per group, so gateway/project scope is still a
     single API call, not one per pole.

No SP_Execution logging -- see shared/leadsun_edge_client.py's own
module docstring for why this is treated as a real-time control action,
not a batch load.

brightness/time_minutes are taken exactly as given by the caller and
passed straight through to Leadsun -- this module doesn't define what
"on" or "off" means (e.g. brightness=0 vs 100, or what time_minutes
represents), by explicit request: the caller decides that, every call.

Gateway/project scope only ever targets CURRENTLY REPORTING poles (a
bounded lookback, same reasoning and same window as
pole_telemetry_loader.py's own update_leadsun_project_details() --
see _REMOTE_CONTROL_LOOKBACK below) -- a pole that hasn't reported
recently is very likely offline/unreachable anyway, so a command aimed
at it wouldn't do anything even if it were included. Single pole_number
scope is NOT bounded this way -- it targets the pole's own most recent
reading regardless of age, since a caller naming one specific pole is
presumably choosing it deliberately (e.g. an operator investigating a
pole they already suspect is having trouble), not asking "whichever
poles happen to be currently online".
"""

import logging
import time as time_module
from datetime import timedelta

import jwt

from shared.sql_client import get_connection
from shared.encryption_utils import decrypt_secret
from shared.datetime_utils import now_eastern as _now_eastern, to_dto_string as _to_dto_string
from shared.leadsun_edge_client import get_token, refresh_token, send_remote_command, LeadsunEdgeApiError


class PoleNotFoundError(Exception):
    """No Poles row matches the given PoleNumber at all."""


class PoleTelemetryNotFoundError(Exception):
    """The pole exists, but has no PoleTelemetry row yet (never reported,
    or its PoleId doesn't match any telemetry) -- there's nothing to
    read GroupId/GatewayCode/ControllerCode/UserName from."""


class GatewayNotFoundError(Exception):
    """No currently-reporting PoleTelemetry row matches the given
    GatewayCode at all -- either the code is wrong, or every pole under
    it has gone quiet longer ago than _REMOTE_CONTROL_LOOKBACK."""


class ProjectNotFoundError(Exception):
    """No Projects row matches the given project_id (this project's OWN
    Id, not Leadsun's internal numeric project id)."""


class ProjectHasNoLeadsunIdError(Exception):
    """The project exists, but its own LeadsunProject JSON has no
    "ProjectId" set yet -- there's no Leadsun-side project to resolve
    telemetry against."""


class ProjectTelemetryNotFoundError(Exception):
    """The project resolves to a real Leadsun ProjectId, but no
    currently-reporting PoleTelemetry row matches it -- every pole under
    it has gone quiet longer ago than _REMOTE_CONTROL_LOOKBACK (or the
    project genuinely has zero poles reporting yet)."""


class PoleNumbersNotResolvedError(Exception):
    """One or more entries in a pole_numbers list couldn't be resolved --
    either the PoleNumber doesn't exist in Poles at all, or it exists but
    has no PoleTelemetry row yet. Both problems are reported together
    (see set_pole_lights()'s own batch-lookup code) since a caller acting
    on a whole list needs to know everything wrong with it at once, not
    just the first issue encountered -- fixing one typo only to
    immediately hit a second, unreported one on the next attempt would
    be a frustrating way to debug a list of a dozen pole numbers."""


class LeadsunEdgeAccountNotFoundError(Exception):
    """The matched telemetry has a UserName, but LeadsunEdgeAccounts has
    no matching row -- there's no password to log in with."""


# How far back "currently reporting" reaches for gateway/project scope --
# same value and same reasoning as pole_telemetry_loader.py's own
# _PROJECT_DETAILS_LOOKBACK: this project's established convention for
# "currently active" telemetry, not a scan of PoleTelemetry's full
# 6-month retention window.
_REMOTE_CONTROL_LOOKBACK = timedelta(hours=3)


_FETCH_LATEST_TELEMETRY_FOR_POLE_NUMBER_SQL = """
SELECT TOP 1 pt.UserName, pt.GroupId, pt.GatewayCode, pt.ControllerCode
FROM Poles p
JOIN PoleTelemetry pt ON pt.PoleId = p.VendorPoleId
WHERE p.PoleNumber = ?
ORDER BY pt.LastUpload DESC
"""

# Shared by gateway and project scope -- the {where_clause} placeholder
# is filled in with "GatewayCode = ?" or "LeadsunProjectId = ?". Same
# ROW_NUMBER-per-PoleId dedup pattern as
# pole_telemetry_loader.py's own
# _FETCH_TELEMETRY_FOR_PROJECT_AGGREGATION_SQL, for the same reason: a
# pole can have multiple readings within the lookback window, and only
# its single most recent one should count.
_FETCH_TELEMETRY_FOR_SCOPE_SQL_TEMPLATE = """
WITH RecentTelemetry AS (
    SELECT
        UserName, GroupId, GatewayCode, ControllerCode, PoleId,
        ROW_NUMBER() OVER (PARTITION BY PoleId ORDER BY LastUpload DESC) AS rn
    FROM PoleTelemetry
    WHERE {where_clause}
      AND LastUpload >= ?
)
SELECT UserName, GroupId, GatewayCode, ControllerCode
FROM RecentTelemetry
WHERE rn = 1
"""

_FETCH_TELEMETRY_BY_GATEWAY_CODE_SQL = _FETCH_TELEMETRY_FOR_SCOPE_SQL_TEMPLATE.format(
    where_clause="GatewayCode = ?"
)
_FETCH_TELEMETRY_BY_LEADSUN_PROJECT_ID_SQL = _FETCH_TELEMETRY_FOR_SCOPE_SQL_TEMPLATE.format(
    where_clause="LeadsunProjectId = ?"
)

_FETCH_PROJECT_LEADSUN_PROJECT_ID_SQL = """
SELECT JSON_VALUE(LeadsunProject, '$.ProjectId') AS LeadsunProjectIdValue
FROM Projects
WHERE Id = ?
"""

_FETCH_LEADSUN_EDGE_ENCRYPTED_PASSWORD_SQL = """
SELECT EncryptedPassword FROM LeadsunEdgeAccounts WHERE Username = ?
"""


def _rows_to_groups_and_username(rows) -> tuple:
    """
    Turns a list of (user_name, group_id, gateway_code, controller_code)
    telemetry rows into (user_name, groups) --
      user_name: taken from the FIRST row. Every row is expected to share
        the same UserName (confirmed constant within a project -- see
        pole_telemetry_loader.py's own
        _aggregate_telemetry_by_leadsun_project() docstring for the
        11,837-record validation this was confirmed against; a gateway
        is a subset of one project, so the same guarantee holds), so this
        doesn't re-validate that on every call -- it just trusts it, the
        same way that other function already does.
      groups: one entry per distinct (group_id, gateway_code) pair,
        each collecting every controller_code seen for that pair --
        matches shared/leadsun_edge_client.py's own send_remote_command()
        `groups` parameter shape directly.
    """
    user_name = rows[0][0]
    grouped: dict = {}
    for _user_name, group_id, gateway_code, controller_code in rows:
        key = (group_id, gateway_code)
        grouped.setdefault(
            key, {"gateway_code": gateway_code, "group_id": group_id, "controller_codes": []}
        )["controller_codes"].append(controller_code)
    return user_name, list(grouped.values())


def _fetch_latest_telemetry_for_pole(pole_number: str):
    """
    Returns (user_name, group_id, gateway_code, controller_code) for the
    given PoleNumber's most recent PoleTelemetry reading.

    Raises PoleNotFoundError if PoleNumber doesn't match any Poles row,
    or PoleTelemetryNotFoundError if it matches a pole but that pole has
    no telemetry yet -- deliberately two different exceptions, not one
    generic "not found", since they point at two different fixes ("this
    PoleNumber is wrong" vs "this pole just hasn't reported yet").
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM Poles WHERE PoleNumber = ?", pole_number)
        if cursor.fetchone() is None:
            raise PoleNotFoundError(f"No pole found with PoleNumber '{pole_number}'.")

        cursor.execute(_FETCH_LATEST_TELEMETRY_FOR_POLE_NUMBER_SQL, pole_number)
        row = cursor.fetchone()
        if row is None:
            raise PoleTelemetryNotFoundError(
                f"Pole '{pole_number}' exists but has no telemetry recorded yet."
            )
        return row
    finally:
        cursor.close()
        conn.close()


def _fetch_latest_telemetry_for_poles(pole_numbers: list):
    """
    Batch version of _fetch_latest_telemetry_for_pole() for a LIST of
    PoleNumbers -- returns {pole_number: (user_name, group_id,
    gateway_code, controller_code)}, one entry per pole_numbers input,
    each pole's own most recent PoleTelemetry reading (same "regardless
    of age" behavior as the single-pole lookup -- see this module's own
    docstring for why: a caller naming specific poles is choosing them
    deliberately, not asking "whichever happen to be currently online").

    All-or-nothing: if ANY given PoleNumber can't be resolved (doesn't
    exist in Poles at all, or exists but has no telemetry yet), raises
    ONE PoleNumbersNotResolvedError listing every problem pole together,
    rather than silently proceeding with just the resolvable subset --
    a caller controlling real hardware from a list should know exactly
    what didn't work, not have some unknown subset silently skipped.
    """
    placeholders = ",".join("?" for _ in pole_numbers)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"SELECT PoleNumber FROM Poles WHERE PoleNumber IN ({placeholders})",
            *pole_numbers,
        )
        existing_pole_numbers = {row[0] for row in cursor.fetchall()}

        cursor.execute(
            f"""
WITH RecentTelemetry AS (
                SELECT
                    p.PoleNumber, pt.UserName, pt.GroupId, pt.GatewayCode, pt.ControllerCode,
                    ROW_NUMBER() OVER (PARTITION BY p.PoleNumber ORDER BY pt.LastUpload DESC) AS rn
                FROM Poles p
                JOIN PoleTelemetry pt ON pt.PoleId = p.VendorPoleId
                WHERE p.PoleNumber IN ({placeholders})
            )
            SELECT PoleNumber, UserName, GroupId, GatewayCode, ControllerCode
            FROM RecentTelemetry
            WHERE rn = 1
            """,
            *pole_numbers,
        )
        telemetry_by_pole_number = {row[0]: row[1:] for row in cursor.fetchall()}
    finally:
        cursor.close()
        conn.close()

    not_found = [pn for pn in pole_numbers if pn not in existing_pole_numbers]
    no_telemetry = [
        pn
        for pn in pole_numbers
        if pn in existing_pole_numbers and pn not in telemetry_by_pole_number
    ]
    if not_found or no_telemetry:
        problems = []
        if not_found:
            problems.append(f"not found: {not_found}")
        if no_telemetry:
            problems.append(f"no telemetry yet: {no_telemetry}")
        raise PoleNumbersNotResolvedError(
            f"Could not resolve all pole numbers ({'; '.join(problems)})."
        )

    return {pn: telemetry_by_pole_number[pn] for pn in pole_numbers}


def _fetch_telemetry_for_gateway(gateway_code: str):
    """
    Returns every currently-reporting (user_name, group_id, gateway_code,
    controller_code) row for the given GatewayCode (see
    _REMOTE_CONTROL_LOOKBACK for what "currently-reporting" bounds to).
    Raises GatewayNotFoundError if none match.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cutoff = _to_dto_string(_now_eastern() - _REMOTE_CONTROL_LOOKBACK)
        cursor.execute(_FETCH_TELEMETRY_BY_GATEWAY_CODE_SQL, gateway_code, cutoff)
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if not rows:
        raise GatewayNotFoundError(
            f"No currently-reporting pole found under gateway '{gateway_code}'."
        )
    return rows


def _resolve_leadsun_project_id(project_id: str) -> str:
    """
    Resolves THIS project's own Projects.Id into Leadsun's own internal
    numeric project id (as a string, matching how it's stored elsewhere
    -- see pole_telemetry_loader.py's own
    _aggregate_telemetry_by_leadsun_project() docstring for why string,
    not int). Raises ProjectNotFoundError if project_id doesn't exist at
    all, or ProjectHasNoLeadsunIdError if it exists but has no Leadsun
    ProjectId configured yet.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM Projects WHERE Id = ?", project_id)
        if cursor.fetchone() is None:
            raise ProjectNotFoundError(f"No project found with Id '{project_id}'.")

        cursor.execute(_FETCH_PROJECT_LEADSUN_PROJECT_ID_SQL, project_id)
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    leadsun_project_id = row[0] if row else None
    if leadsun_project_id is None:
        raise ProjectHasNoLeadsunIdError(
            f"Project '{project_id}' has no Leadsun ProjectId configured yet."
        )
    return leadsun_project_id


def _fetch_telemetry_for_project(project_id: str):
    """
    Returns every currently-reporting (user_name, group_id, gateway_code,
    controller_code) row across every gateway in the given project (THIS
    project's own Projects.Id -- see _resolve_leadsun_project_id() for
    how that's translated to Leadsun's own internal id). Raises
    ProjectNotFoundError/ProjectHasNoLeadsunIdError per
    _resolve_leadsun_project_id(), or ProjectTelemetryNotFoundError if
    the project resolves fine but nothing is currently reporting under
    it.
    """
    leadsun_project_id = _resolve_leadsun_project_id(project_id)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cutoff = _to_dto_string(_now_eastern() - _REMOTE_CONTROL_LOOKBACK)
        cursor.execute(_FETCH_TELEMETRY_BY_LEADSUN_PROJECT_ID_SQL, leadsun_project_id, cutoff)
        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    if not rows:
        raise ProjectTelemetryNotFoundError(
            f"Project '{project_id}' has no currently-reporting poles."
        )
    return rows


def _fetch_leadsun_edge_password(username: str) -> str:
    """
    Returns the DECRYPTED Leadsun EDGE password for the given username.
    Raises LeadsunEdgeAccountNotFoundError if LeadsunEdgeAccounts has no
    matching row.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(_FETCH_LEADSUN_EDGE_ENCRYPTED_PASSWORD_SQL, username)
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if row is None:
        raise LeadsunEdgeAccountNotFoundError(
            f"No LeadsunEdgeAccounts row found for username '{username}'."
        )
    return decrypt_secret(row[0])


# In-process token cache, keyed by Leadsun EDGE username -- best-effort
# only. Azure Functions instances can be recycled at any time (especially
# on Flex Consumption), silently dropping this cache; a fresh get_token()
# call is always a safe fallback, just a slightly slower one (an extra
# HTTP round trip). NOT persisted anywhere (no SQL table, no shared
# cache) -- the cost of occasionally re-authenticating is far lower than
# the complexity of a durable, cross-instance token store for what's
# fundamentally just a login-session optimization.
_token_cache: dict[str, dict] = {}

# Small buffer subtracted from a token's own "exp" claim so a token that
# LOOKS valid right now doesn't expire mid-flight between this check and
# the API actually receiving the request.
_EXPIRY_SAFETY_BUFFER_SECONDS = 30


def _decode_expiry(token: str) -> float:
    """
    Reads the "exp" claim (Unix timestamp) out of a JWT WITHOUT verifying
    its signature -- this project isn't the one that issued the token
    (Leadsun EDGE is), so it has no key to verify against, and doesn't
    need one: the token is only ever being read back to decide "is this
    still fresh enough to reuse", not being trusted as an authorization
    decision the way a token this project itself issues would be (see
    shared/auth_utils.py's own JWTs for that different, verified case).
    """
    return jwt.decode(token, options={"verify_signature": False})["exp"]


def _force_fresh_access_token(username: str, password: str) -> str:
    """
    Unconditionally calls get_token() (bypassing any cached entry
    entirely) and updates the cache with the result. Used as a
    self-healing retry step: if a cached token gets rejected by Leadsun
    EDGE, the most likely explanation isn't that the token merely
    expired early (_get_access_token already accounts for that) -- it's
    that ANOTHER Azure Functions instance (a cold start, a concurrent
    request, a scale-out event) independently called get_token() for
    this same username in the meantime, and Leadsun EDGE enforces a
    single active session per account, silently invalidating this
    instance's still-cached-and-still-"unexpired" token. Since the token
    cache is per-process (see _token_cache's own comment), no amount of
    local expiry bookkeeping can detect that from here -- only an actual
    failed request against the real API reveals it. Forcing a fresh
    login recovers immediately either way.
    """
    body = get_token(username, password)
    _token_cache[username] = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "access_exp": _decode_expiry(body["access_token"]),
        "refresh_exp": _decode_expiry(body["refresh_token"]),
    }
    return body["access_token"]


def _get_access_token(username: str, password: str) -> str:
    """
    Returns a valid access token for username, reusing a cached one if
    it's not near expiry, refreshing via the cached refresh token if the
    access token has expired but the refresh token hasn't, or logging in
    fresh with the password otherwise (no cache entry at all, or the
    refresh token itself has also expired, or a refresh attempt itself
    fails for any reason).
    """
    now = time_module.time()
    cached = _token_cache.get(username)

    if cached and cached["access_exp"] - _EXPIRY_SAFETY_BUFFER_SECONDS > now:
        return cached["access_token"]

    if cached and cached["refresh_exp"] - _EXPIRY_SAFETY_BUFFER_SECONDS > now:
        try:
            body = refresh_token(cached["refresh_token"])
            _token_cache[username] = {
                "access_token": body["access_token"],
                "refresh_token": body["refresh_token"],
                "access_exp": _decode_expiry(body["access_token"]),
                "refresh_exp": _decode_expiry(body["refresh_token"]),
            }
            return body["access_token"]
        except Exception as ex:
            logging.warning(
                "pole_remote_control: refresh_token failed for '%s', falling back to "
                "get_token: %s",
                username,
                ex,
            )

    body = get_token(username, password)
    _token_cache[username] = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "access_exp": _decode_expiry(body["access_token"]),
        "refresh_exp": _decode_expiry(body["refresh_token"]),
    }
    return body["access_token"]


def _execute_remote_command(
    user_name: str, groups: list, brightness: int, time_minutes: int, scope_description: str
) -> dict:
    """
    Shared by every scope in set_pole_lights(): fetches the password for
    user_name, gets/reuses an access token, sends the remote-command, and
    self-heals with one retry (a truly fresh login) if the first attempt
    is rejected -- see _force_fresh_access_token()'s own docstring for
    why that specific failure mode happens and why retrying once (not
    looping) is the right response to it.
    """
    password = _fetch_leadsun_edge_password(user_name)
    access_token = _get_access_token(user_name, password)

    logging.info(
        "pole_remote_control: sending remote command for %s (userName=%s, "
        "%d group(s), brightness=%s, time=%s).",
        scope_description, user_name, len(groups), brightness, time_minutes,
    )
    try:
        return send_remote_command(
            access_token=access_token,
            brightness=brightness,
            time_minutes=time_minutes,
            groups=groups,
        )
    except LeadsunEdgeApiError as ex:
        logging.warning(
            "pole_remote_control: remote-command failed with cached token for %s "
            "(userName=%s), retrying once with a fresh login: %s",
            scope_description, user_name, ex,
        )
        access_token = _force_fresh_access_token(user_name, password)
        return send_remote_command(
            access_token=access_token,
            brightness=brightness,
            time_minutes=time_minutes,
            groups=groups,
        )


def set_pole_lights(
    brightness: int,
    time_minutes: int,
    pole_number: str = None,
    pole_numbers: list = None,
    gateway_code: str = None,
    project_id: str = None,
) -> dict:
    """
    Turns light(s) on/off/dim at exactly ONE of four scopes -- pass
    exactly one of pole_number/pole_numbers/gateway_code/project_id, not
    zero, not more than one. brightness and time_minutes are passed
    straight through to Leadsun for every matched pole -- see this
    module's own docstring for why no on/off-specific interpretation
    happens here.

    pole_numbers (a LIST of PoleNumbers, possibly spanning different
    groups AND different Leadsun accounts) is the odd one out among the
    four scopes: pole_number/gateway_code/project_id each resolve to
    telemetry that's guaranteed (or, for gateway/project, has been
    validated elsewhere -- see pole_telemetry_loader.py's own
    _aggregate_telemetry_by_leadsun_project() docstring) to share ONE
    UserName, so they only ever need ONE Leadsun EDGE login and ONE
    remote-command call. An arbitrary pole_numbers list has no such
    guarantee -- the poles named could belong to entirely different
    projects with entirely different Leadsun accounts. So this scope
    partitions the resolved telemetry by UserName FIRST, then issues one
    independent login + remote-command call PER distinct UserName found
    (still just one call per account, not one per pole -- poles sharing
    an account but spanning multiple groups still land in one call's
    `groups` list, same as gateway/project scope). In the overwhelmingly
    common case (a caller's pole_numbers all belong to one project), this
    still ends up being exactly one call, same as any other scope.

    Raises:
      ValueError                      -- zero or more than one scope
                                          argument given
      PoleNotFoundError               -- pole_number doesn't exist
      PoleTelemetryNotFoundError      -- pole exists, no telemetry yet
      PoleNumbersNotResolvedError     -- one or more pole_numbers entries
                                          couldn't be resolved (see
                                          _fetch_latest_telemetry_for_poles()
                                          for the all-or-nothing reasoning)
      GatewayNotFoundError            -- gateway_code matches nothing
                                          currently reporting
      ProjectNotFoundError            -- project_id doesn't exist
      ProjectHasNoLeadsunIdError      -- project exists, no Leadsun
                                          ProjectId configured yet
      ProjectTelemetryNotFoundError   -- project resolves fine, nothing
                                          currently reporting under it
      LeadsunEdgeAccountNotFoundError -- matched UserName has no
                                          matching LeadsunEdgeAccounts row
      LeadsunEdgeApiError             -- Leadsun EDGE itself rejected the
                                          login or the command

    Returns the parsed remote-command response body on success (see
    shared/leadsun_edge_client.py's send_remote_command() for its shape)
    for pole_number/gateway_code/project_id scope, OR for pole_numbers
    scope specifically:
      - if every matched pole shares one UserName (the common case):
        that SAME single response body, unchanged -- so a caller who
        only ever uses single-account pole lists sees an identical
        response shape regardless of which scope they used.
      - if the list spans multiple UserNames: {"results": [{"userName":
        ..., "poleCount": <int>, "response": {...}}, ...]}, one entry
        per account actually contacted.
    """
    scope_args_given = [
        name
        for name, value in (
            ("pole_number", pole_number),
            ("pole_numbers", pole_numbers),
            ("gateway_code", gateway_code),
            ("project_id", project_id),
        )
        if value
    ]
    if len(scope_args_given) != 1:
        raise ValueError(
            "Exactly one of pole_number, pole_numbers, gateway_code, or project_id is "
            f"required (got: {scope_args_given or 'none'})."
        )

    if pole_number:
        user_name, group_id, gateway_code_value, controller_code = (
            _fetch_latest_telemetry_for_pole(pole_number)
        )
        groups = [
            {
                "gateway_code": gateway_code_value,
                "group_id": group_id,
                "controller_codes": [controller_code],
            }
        ]
        scope_description = f"pole '{pole_number}'"
        return _execute_remote_command(user_name, groups, brightness, time_minutes, scope_description)

    if pole_numbers:
        telemetry_by_pole_number = _fetch_latest_telemetry_for_poles(pole_numbers)
        by_username: dict = {}
        for pole_number_key, (user_name, group_id, gateway_code_value, controller_code) in (
            telemetry_by_pole_number.items()
        ):
            by_username.setdefault(user_name, []).append(
                (user_name, group_id, gateway_code_value, controller_code)
            )

        results = []
        for user_name, rows in by_username.items():
            _, groups = _rows_to_groups_and_username(rows)
            scope_description = f"{len(rows)} pole(s) from pole_numbers list (userName={user_name})"
            response = _execute_remote_command(
                user_name, groups, brightness, time_minutes, scope_description
            )
            results.append({"userName": user_name, "poleCount": len(rows), "response": response})

        if len(results) == 1:
            return results[0]["response"]
        return {"results": results}

    if gateway_code:
        rows = _fetch_telemetry_for_gateway(gateway_code)
        user_name, groups = _rows_to_groups_and_username(rows)
        scope_description = f"gateway '{gateway_code}' ({len(rows)} pole(s))"
        return _execute_remote_command(user_name, groups, brightness, time_minutes, scope_description)

    rows = _fetch_telemetry_for_project(project_id)
    user_name, groups = _rows_to_groups_and_username(rows)
    scope_description = f"project '{project_id}' ({len(rows)} pole(s))"
    return _execute_remote_command(user_name, groups, brightness, time_minutes, scope_description)
