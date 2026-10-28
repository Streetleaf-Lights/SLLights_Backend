"""
HTTP client for the Leadsun EDGE lighting-control API -- a genuinely
separate system from shared/leadsun_client.py's own /lamps and /models
endpoints (different host, different port, different auth model: this
one is username/password -> JWT bearer token, not mutual TLS with a
client certificate). Nothing in the curl examples this was built from
showed a client cert being sent, so this client doesn't attempt mTLS --
if that turns out to be required in practice, this would need the same
cert-handling machinery leadsun_client.py already has.

Three endpoints, called in this order by shared/pole_remote_control.py:
  1. get_token(username, password)      -- initial login
  2. refresh_token(refresh_token_value) -- renew an access token without
     re-sending the password, using the refresh token from step 1
  3. send_remote_command(...)           -- the actual light on/off/dim
     command, using the access token from step 1 or 2

Two more endpoints, used by shared/leadsun_project_populator.py to create a
Leadsun project: upsert_project() and list_projects() (both authenticated
with the access token from get_token()).

No SP_Execution logging here -- this is a single, immediate, real-time
control action (a person or another system asking one specific light to
change right now), not a batch load with counts to track. Ordinary
logging.info/logging.error calls cover observability for this kind of
call; see shared/pole_remote_control.py's own module docstring if that
decision needs revisiting later (e.g. an audit-trail requirement for
who-turned-what-off-when).
"""

import os
import logging

import requests

# Confirmed via the curl examples this was built from -- a different
# host, port, and path prefix from LEADSUN_API_URL/LEADSUN_MODELS_URL in
# shared/leadsun_client.py (leadsunedge-us.com:8550/lamps|models). Kept
# as its own env var, with this exact value as the default, so an
# already-configured LEADSUN_API_URL keeps meaning what it always has.
LEADSUN_EDGE_BASE_URL = os.environ.get(
    "LEADSUN_EDGE_BASE_URL", "https://www.leadsunedge-us.com:4428/street-light/lighting-control"
)

_REQUEST_TIMEOUT_SECONDS = 30


class LeadsunEdgeApiError(Exception):
    """
    Raised when the Leadsun EDGE API itself reports a failure -- either
    an HTTP-level error (non-2xx) or a 2xx response whose own JSON body
    says {"success": false, ...}. Leadsun's own "message" field (when
    present) is included in the exception text, since it's usually the
    most specific, human-readable explanation available (e.g. "Invalid
    credentials", "Token expired").
    """


def _checked_json(path: str, response) -> dict:
    """
    Shared by every call here: raises LeadsunEdgeApiError on a non-2xx
    response (including the response BODY text, which is often the only
    place the real reason is) or on a 2xx whose own JSON says
    {"success": false}; otherwise returns the parsed body.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError as ex:
        raise LeadsunEdgeApiError(
            f"Leadsun EDGE API ({path}) returned {response.status_code}: "
            f"{response.text or '(empty response body)'}"
        ) from ex
    body = response.json()
    if not body.get("success"):
        raise LeadsunEdgeApiError(
            f"Leadsun EDGE API ({path}) reported failure: {body.get('message', body)}"
        )
    return body


def _post_form(path: str, data: dict) -> dict:
    """
    Shared POST logic for get_token/refresh_token -- both send
    application/x-www-form-urlencoded bodies (requests' data= kwarg
    handles that encoding automatically) and both return the same
    {"success", "message", "statusCode", "access_token",
    "refresh_token"} shape.
    """
    url = f"{LEADSUN_EDGE_BASE_URL}/{path}"
    response = requests.post(url, data=data, timeout=_REQUEST_TIMEOUT_SECONDS)
    return _checked_json(path, response)


def _post_authed_form(path: str, access_token: str, data: dict = None) -> dict:
    """
    POST to an authenticated endpoint with a form-encoded body (curl's
    --data-urlencode, or --data '' for none). The access token goes in the
    Authorization header BARE, with no "Bearer " prefix -- the same format
    send_remote_command() settled on after "Bearer <token>" got a 401 (see
    its ASSUMPTION note), and what the curl examples for these two endpoints
    show.
    """
    url = f"{LEADSUN_EDGE_BASE_URL}/{path}"
    headers = {
        "Authorization": access_token,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    # data="" (not None) so an empty body is sent explicitly, like curl --data ''
    response = requests.post(
        url,
        data=data if data is not None else "",
        headers=headers,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    return _checked_json(path, response)


def get_token(username: str, password: str) -> dict:
    """
    Logs into the Leadsun EDGE API with a username/password (from
    LeadsunEdgeAccounts, decrypted). Returns the full response body --
    callers use body["access_token"]/body["refresh_token"].

    NOTE: sent as form fields named "userName"/"pswd" -- Leadsun's own
    field names, not this project's usual camelCase/PascalCase
    conventions; kept as-is since these are literal wire-format field
    names for an external API, not something this project controls.
    """
    return _post_form("get-token", {"userName": username, "pswd": password})


def refresh_token(refresh_token_value: str) -> dict:
    """
    Exchanges a still-valid refresh token for a new access token (and a
    new refresh token) without needing the password again. Same response
    shape as get_token(). If this fails (e.g. the refresh token itself
    has expired), the caller should fall back to get_token() with the
    password -- this function itself doesn't do that fallback, since it
    doesn't have the password to fall back with.
    """
    return _post_form("refresh-token", {"RefToken": refresh_token_value})


def send_remote_command(access_token: str, brightness: int, time_minutes: int, groups: list) -> dict:
    """
    Sends the actual light on/off/dim command, for one or many poles at
    once -- Leadsun's own remote-command payload already supports
    multiple groups AND multiple products per group in a single request,
    so controlling a whole gateway or a whole project is still exactly
    ONE call to this function, just with a bigger `groups` list -- not
    one call per pole.

    brightness (0-100) and time_minutes are passed through exactly as
    given by the caller -- this function doesn't interpret or convert
    them, since the caller (shared/pole_remote_control.py's
    set_pole_lights(), and ultimately whoever calls the setPoleLights
    HTTP endpoint) decides what they mean for a given command (e.g.
    brightness=0 for "off", brightness=100+some time_minutes for "on for
    the next N minutes").

    groups: a list of dicts, each shaped
      {"gateway_code": str, "group_id": int|str, "controller_codes": [str, ...]}
    one entry per distinct (GatewayCode, GroupId) pair being targeted --
    a single pole is just the degenerate case of one group with one
    controller code in its list.

    Each entry maps into the request body's OWN field names in a way
    that looks like a mismatch but isn't -- confirmed directly, not a
    guess:
      body["groups"][i]["controllerCode"]             <- gateway_code
      body["groups"][i]["groupId"]                    <- str(group_id)
      body["groups"][i]["products"][j]["productId"]   <- controller_codes[j]
    i.e. Leadsun's OWN "controllerCode"/"productId" field names in this
    specific endpoint's request body do NOT correspond to this project's
    OWN GatewayCode/ControllerCode PoleTelemetry columns of the same or
    similar name -- they're cross-wired. groupId is sent as a STRING
    (e.g. "1458"), matching the curl example, even though this project's
    own GroupId column is an INT.

    Raises LeadsunEdgeApiError on any HTTP-level or {"success": false}
    failure -- an HTTP-level failure (4xx/5xx) includes whatever response
    BODY text came back, if any, since raise_for_status() alone gives
    only the status code/reason with no detail on WHY the server
    rejected the request (e.g. an auth failure's real explanation is
    often only in the body, not the status line).

    ASSUMPTION, flagged explicitly: the access token is sent as an
    "Authorization: <token>" header with NO "Bearer " prefix. This
    started as "Authorization: Bearer <token>" (the more common
    convention) but that got a 401 in practice against the real API, so
    this now tries the token bare instead -- still a guess, since the
    curl example this was built from never actually showed an
    Authorization header on this specific call at all (only
    "Content-Type: application/json"). If this ALSO 401s, the real
    format is genuinely unknown from what's been provided so far and
    needs either Leadsun's own API docs, or the actual response body
    from a failed attempt (now captured -- see the raise_for_status
    handling below) to pin down further.

    On success, returns the parsed response body (typically {"success":
    true, "message": "Request successful", "statusCode": "200", "data":
    null} -- "data" carries no further information to return to a
    caller).
    """
    url = f"{LEADSUN_EDGE_BASE_URL}/dominate/remote-command"
    headers = {"Authorization": access_token, "Content-Type": "application/json"}
    payload = {
        "brightness": brightness,
        "time": time_minutes,
        "groups": [
            {
                "controllerCode": group["gateway_code"],
                "groupId": str(group["group_id"]),
                "products": [
                    {"productId": controller_code}
                    for controller_code in group["controller_codes"]
                ],
            }
            for group in groups
        ],
    }
    response = requests.post(url, json=payload, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        response.raise_for_status()
    except requests.HTTPError as ex:
        raise LeadsunEdgeApiError(
            f"Leadsun EDGE API (remote-command) returned {response.status_code}: "
            f"{response.text or '(empty response body)'}"
        ) from ex
    body = response.json()
    if not body.get("success"):
        raise LeadsunEdgeApiError(
            f"Leadsun EDGE API (remote-command) reported failure: {body.get('message', body)}"
        )
    logging.info(
        "leadsun_edge_client: remote-command sent (%d group(s), %d total product(s), "
        "brightness=%s, time=%s).",
        len(groups),
        sum(len(g["controller_codes"]) for g in groups),
        brightness,
        time_minutes,
    )
    return body


def upsert_project(access_token: str, name: str, remark: str = "") -> dict:
    """
    Creates a Leadsun project (project/upsert). Sent as form fields `name`
    and `remark` -- remark is always sent, as an empty string when there is
    none, matching how Leadsun itself reports a project with no remark
    ("remark": "").

    "upsert" suggests this may UPDATE a project that already has this name
    instead of creating a second one; callers that need a genuinely NEW
    project must check the name is free first (see
    shared/leadsun_project_populator.py). The response carries no project
    id -- {"success": true, ..., "data": null} -- so the id has to be looked
    up afterwards with list_projects().
    """
    body = _post_authed_form("project/upsert", access_token, {"name": name, "remark": remark or ""})
    logging.info("leadsun_edge_client: project upsert accepted for name=%r.", name)
    return body


def list_projects(access_token: str) -> list:
    """
    All projects visible to this account (project/projects, an empty-body
    POST). Each item is shaped like
      {"id": 389, "name": "DEMOs", "isDefault": null, "remark": "",
       "userName": "SLDEMOS", "createTime": "...", "totalGateway": 4, "totalLamp": 4}
    Returns [] if Leadsun reports success with no data.
    """
    body = _post_authed_form("project/projects", access_token)
    return body.get("data") or []
