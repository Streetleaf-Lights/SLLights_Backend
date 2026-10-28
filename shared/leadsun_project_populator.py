"""
populate_leadsun_project: creates a project in Leadsun EDGE and links it to
one of OUR Projects, by filling Projects.LeadsunProject (and Airtable's
"Leadsun ProjectID", which is where that value really comes from).

Not related to telemetry: the richer LeadsunProject JSON (ProjectName,
totalGateways, totalPoles, groups) is still built later by
pole_telemetry_loader.update_leadsun_project_details(), once the project's
poles report in. This only sets the one key projects_loader.py itself owns,
"ProjectId", in exactly the shape that loader writes: {"ProjectId": "<id>"}
-- a JSON STRING value, produced by the same
JSON_MODIFY('{}', '$.ProjectId', CAST(? AS NVARCHAR(MAX))) its INSERT uses.

Steps, in order (a failure stops everything after it):
  1. Validate the input and the configured Leadsun admin credentials.
  2. Pre-flight, changing nothing anywhere:
       - the Project exists, is within the caller's data scope (see below),
         and its LeadsunProject is BLANK (NULL, '', '{}');
       - its Airtable record exists and "Leadsun ProjectID" is empty there too
         (SQL can lag Airtable by hours, and step 6 would overwrite it).
  3. Log in to Leadsun with the configured admin account, then list projects
     and refuse if one with this name already exists (see below).
  4. project/upsert -- create the project.
  5. project/projects, retried with short delays (Leadsun can take a moment
     to show it) -- find the NEW project by name to learn its id.
  6. PATCH Airtable's "Leadsun ProjectID".
  7. Fill Projects.LeadsunProject.

Data scope: the caller's scope (shared/data_scope.py) bounds WHICH Projects
may be populated, the same way it bounds which ones they can read. Streetleaf
Admin is unrestricted; Streetleaf Crew may only populate projects that have an
installation schedule (Projects.StickerNumbers IS NOT NULL). A Project outside
the caller's scope is reported as "not found" (404), never as forbidden, so it
isn't confirmed to exist -- and nothing is created in Leadsun for it.

Two decisions worth knowing about:

* The name-exists check (step 3). "upsert" may UPDATE an existing project of
  the same name rather than create one, and picking an existing project's id
  would silently link our Project -- and so its telemetry -- to somebody
  else's Leadsun project. So an existing name is a 409 BEFORE anything is
  created, and "the new project" in step 5 means: this name, an id that was
  not in the step-3 listing.

* Airtable (step 6) is written BEFORE the database (step 7). Airtable is the
  source of truth: loadProjects compares Projects.LeadsunProject's ProjectId
  with Airtable's value on every run and rewrites it to match -- and with an
  empty Airtable value JSON_MODIFY REMOVES the key. So if the database were
  filled first and the PATCH then failed, the next loadProjects run would
  quietly erase it. This way every partial failure either changes nothing
  local, or leaves Airtable correct so loadProjects fills the database in by
  itself. Errors after the Leadsun project exists carry its id
  (leadsun_project_id) so the rest can be finished by hand.

Leadsun has no delete call in what this uses, so a project it already created
can't be undone from here -- hence all the checks BEFORE step 4.
"""

import json
import logging
import os
import time

from shared import airtable_client
from shared.airtable_client import AirtableApiError
from shared.data_scope import UNRESTRICTED, DataScope, project_filter
from shared.leadsun_edge_client import LeadsunEdgeApiError, get_token, list_projects, upsert_project
from shared.projects_loader import AIRTABLE_PROJECTS_TABLE
from shared.sql_client import get_connection

# Same field projects_loader reads ("Leadsun ProjectID") -- a test pins the two together.
AIRTABLE_LEADSUN_PROJECT_ID_FIELD = "Leadsun ProjectID"

# Waits before each look at project/projects after the upsert ("may need a
# delay"): ~11s in all before giving up. Module-level so tests can shrink it.
_LIST_DELAYS_SECONDS = (1, 2, 3, 5)

_USERNAME_SETTING = "LEADSUN_EDGE_ADMIN_USERNAME"
_PASSWORD_SETTING = "LEADSUN_EDGE_ADMIN_PASSWORD"


class PopulateError(Exception):
    """Base for failures the HTTP layer turns into a specific status."""

    http_status = 500

    def __init__(self, message, leadsun_project_id=None):
        super().__init__(message)
        # Set once the Leadsun project exists, so the caller can finish by hand.
        self.leadsun_project_id = leadsun_project_id


class ProjectNotFoundError(PopulateError):
    http_status = 404


class PopulateConflictError(PopulateError):
    http_status = 409


class LeadsunProjectLookupError(PopulateError):
    http_status = 502


class AirtableError(PopulateError):
    http_status = 502


class DatabaseUpdateError(PopulateError):
    http_status = 500


class LeadsunCredentialsNotConfiguredError(PopulateError):
    http_status = 500


def _required_text(value, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} is required" if value is None else f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _optional_text(value, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip()


def _credentials() -> tuple:
    username = os.environ.get(_USERNAME_SETTING)
    password = os.environ.get(_PASSWORD_SETTING)
    if not username or not password:
        raise LeadsunCredentialsNotConfiguredError(
            f"{_USERNAME_SETTING} and {_PASSWORD_SETTING} must both be configured"
        )
    return username, password


def _is_blank_leadsun_project(value) -> bool:
    """NULL, '' (or whitespace), '{}' or JSON null -- i.e. nothing populated yet."""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except ValueError:
        return False  # not JSON at all: something is there, don't overwrite it
    return parsed is None or parsed == {}


def _same_name(a, b) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _read_blank_leadsun_project(project_id: str, scope: DataScope = UNRESTRICTED):
    """
    Returns the Project's current LeadsunProject value (blank by definition,
    exactly as stored -- the final UPDATE is conditional on it still being
    that value). Raises 404 if there's no such Project OR it is outside
    `scope` (deliberately indistinguishable), 409 if it's populated. A
    short-lived connection: nothing is held open while Leadsun is called.
    """
    conditions, params = ["Id = ?"], [project_id]
    scope_conditions, scope_params = project_filter(scope)
    conditions += scope_conditions
    params += scope_params

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT Id, LeadsunProject FROM Projects WHERE " + " AND ".join(conditions), *params
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if row is None:
        raise ProjectNotFoundError(f"no Project found with ProjectId '{project_id}'")
    current = row[1]
    if not _is_blank_leadsun_project(current):
        raise PopulateConflictError(
            f"Project '{project_id}' already has a LeadsunProject ({str(current)[:200]}); "
            "it must be blank before one can be populated"
        )
    return current


def _ensure_airtable_field_empty(project_id: str) -> None:
    try:
        record = airtable_client.fetch_record(AIRTABLE_PROJECTS_TABLE, project_id)
    except AirtableApiError as ex:
        raise AirtableError(f"could not read the Project's Airtable record: {ex}") from ex
    value = (record.get("fields") or {}).get(AIRTABLE_LEADSUN_PROJECT_ID_FIELD)
    if value is not None and str(value).strip() not in ("", "[]"):
        raise PopulateConflictError(
            f"Airtable already has a '{AIRTABLE_LEADSUN_PROJECT_ID_FIELD}' ({value}) for "
            f"Project '{project_id}' even though Projects.LeadsunProject is blank "
            "(loadProjects hasn't run since it was set); not overwriting it"
        )


def _find_new_project_id(token: str, name: str, ids_before: set) -> str:
    """The id of the project named `name` that wasn't in the pre-upsert listing."""
    last_seen = 0
    for delay in _LIST_DELAYS_SECONDS:
        time.sleep(delay)
        try:
            projects = list_projects(token)
        except LeadsunEdgeApiError as ex:
            raise LeadsunProjectLookupError(
                f"Leadsun accepted project '{name}' but listing projects afterwards failed: {ex}"
            ) from ex
        last_seen = len(projects)
        new = [
            p for p in projects
            if p.get("id") is not None and _same_name(p.get("name"), name) and str(p["id"]) not in ids_before
        ]
        if len(new) == 1:
            return str(new[0]["id"])
        if len(new) > 1:
            ids = ", ".join(str(p["id"]) for p in new)
            raise LeadsunProjectLookupError(
                f"more than one NEW Leadsun project named '{name}' appeared (ids {ids}); "
                "can't tell which is this one, so nothing was linked"
            )
    raise LeadsunProjectLookupError(
        f"Leadsun accepted project '{name}' but it never appeared in the project list "
        f"({len(_LIST_DELAYS_SECONDS)} looks, {last_seen} project(s) listed); nothing was linked"
    )


def _write_airtable(project_id: str, leadsun_project_id: str) -> None:
    try:
        # typecast: the string "389" becomes a number or stays text, whichever
        # type that Airtable column is -- not something this code controls.
        airtable_client.update_record(
            AIRTABLE_PROJECTS_TABLE,
            project_id,
            {AIRTABLE_LEADSUN_PROJECT_ID_FIELD: leadsun_project_id},
            typecast=True,
        )
    except AirtableApiError as ex:
        raise AirtableError(
            f"Leadsun project {leadsun_project_id} was created, but Airtable's "
            f"'{AIRTABLE_LEADSUN_PROJECT_ID_FIELD}' could not be updated ({ex}). Set it to "
            f"{leadsun_project_id} on this Project's Airtable record by hand; loadProjects "
            "will then fill Projects.LeadsunProject. The database was not changed.",
            leadsun_project_id=leadsun_project_id,
        ) from ex


def _write_database(project_id: str, leadsun_project_id: str, original) -> None:
    # JSON_MODIFY('{}', ...) -- the same expression projects_loader's INSERT
    # uses -- and conditional on the value still being what we read, so a
    # concurrent change is noticed instead of overwritten.
    where_current = "LeadsunProject IS NULL" if original is None else "LeadsunProject = ?"
    sql = (
        "UPDATE Projects SET LeadsunProject = JSON_MODIFY('{}', '$.ProjectId', CAST(? AS NVARCHAR(MAX))) "
        f"WHERE Id = ? AND {where_current}"
    )
    params = [leadsun_project_id, project_id] + ([] if original is None else [original])
    try:
        conn = get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(sql, *params)
            updated = cursor.rowcount
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as ex:
        raise DatabaseUpdateError(
            f"Leadsun project {leadsun_project_id} was created and Airtable updated, but the "
            f"database update failed ({ex}). loadProjects will fill Projects.LeadsunProject "
            "in on its next run.",
            leadsun_project_id=leadsun_project_id,
        ) from ex
    if updated == 0:
        raise PopulateConflictError(
            f"Leadsun project {leadsun_project_id} was created and Airtable updated, but "
            f"Projects.LeadsunProject for '{project_id}' changed while this ran, so it was not "
            "overwritten. loadProjects will reconcile it from Airtable.",
            leadsun_project_id=leadsun_project_id,
        )


def populate_leadsun_project(project_id, project_name, remark=None, scope: DataScope = UNRESTRICTED) -> dict:
    """
    Creates the Leadsun project and links it to Project `project_id` (our
    Projects.Id, which is also its Airtable record id). See the module
    docstring for the steps and why they run in this order. `scope` is the
    caller's data scope: a Project outside it is a 404 and nothing is created.

    Raises ValueError for bad input; PopulateError subclasses (each with its
    own http_status) for everything else; LeadsunEdgeApiError if a Leadsun
    call itself fails.
    """
    project_id = _required_text(project_id, "ProjectId")
    project_name = _required_text(project_name, "ProjectName")
    remark = _optional_text(remark, "remark")
    username, password = _credentials()

    # Pre-flight: change nothing anywhere until all of this passes.
    original = _read_blank_leadsun_project(project_id, scope)
    _ensure_airtable_field_empty(project_id)

    token = get_token(username, password)["access_token"]
    existing = list_projects(token)
    clash = [p for p in existing if _same_name(p.get("name"), project_name)]
    if clash:
        raise PopulateConflictError(
            f"a Leadsun project named '{project_name}' already exists (id {clash[0].get('id')}); "
            "not creating another or linking to one this call didn't create"
        )
    ids_before = {str(p["id"]) for p in existing if p.get("id") is not None}

    upsert_project(token, project_name, remark)
    leadsun_project_id = _find_new_project_id(token, project_name, ids_before)

    _write_airtable(project_id, leadsun_project_id)
    _write_database(project_id, leadsun_project_id, original)

    logging.info(
        "populate_leadsun_project: Project %s linked to new Leadsun project %s ('%s').",
        project_id, leadsun_project_id, project_name,
    )
    return {
        "projectId": project_id,
        "projectName": project_name,
        # The new Leadsun id is leadsunProject.ProjectId -- exactly what was stored.
        "leadsunProject": {"ProjectId": leadsun_project_id},
    }
