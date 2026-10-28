"""
Tests for shared/leadsun_project_populator.py.

The whole outside world (database, Airtable, Leadsun) is faked by `world`,
which records every call in ONE ordered list, so the tests can pin the
sequencing guarantees that matter -- nothing is created until every
pre-flight check has passed, no database connection is held open across the
slow Leadsun calls, and Airtable is written before the database -- not just
that each call happens.
"""

from unittest.mock import MagicMock, call

import pytest

from shared import leadsun_project_populator as m
from shared import projects_loader
from shared.airtable_client import AirtableApiError
from shared.data_scope import UNRESTRICTED, DataScope
from shared.leadsun_edge_client import LeadsunEdgeApiError

PROJECT_ID = "rec9jSlJ4p2kJhV6w"
NAME = "Pioneer Ranch"
EXISTING = [{"id": 100, "name": "Some Other Project"}, {"id": 101, "name": "Another"}]
NEW = {"id": 389, "name": NAME}
CREW = DataScope(scheduled_projects_only=True)
CUSTOMER = DataScope(customer_id="cust-own")


class World:
    """Fakes for every external dependency, all appending to `events`."""

    def __init__(self, mocker, monkeypatch):
        self.events = []
        self.leadsun_projects = list(EXISTING)  # what Leadsun lists right now
        self.appears_on_look = 1  # the new project shows up on this look (1 = first look after upsert)
        self._looks_after_upsert = 0
        self.upserted = False

        monkeypatch.setenv("LEADSUN_EDGE_ADMIN_USERNAME", "ADMIN")
        monkeypatch.setenv("LEADSUN_EDGE_ADMIN_PASSWORD", "secret")

        # --- database ---
        self.cursor = MagicMock()
        self.cursor.fetchone.return_value = (PROJECT_ID, None)  # blank LeadsunProject
        self.cursor.rowcount = 1
        self.cursor.execute.side_effect = lambda sql, *a: self.events.append(f"db:{sql.split()[0]}")
        self.conn = MagicMock()
        self.conn.cursor.return_value = self.cursor
        self.conn.commit.side_effect = lambda: self.events.append("db:commit")
        self.conn.close.side_effect = lambda: self.events.append("db:close")

        def connect():
            self.events.append("db:connect")
            return self.conn

        mocker.patch("shared.leadsun_project_populator.get_connection", side_effect=connect)

        # --- airtable ---
        self.airtable_record = {"id": PROJECT_ID, "fields": {}}
        self.fetch_record = mocker.patch(
            "shared.airtable_client.fetch_record",
            side_effect=lambda *a, **k: self.events.append("airtable:get") or self.airtable_record,
        )
        self.update_record = mocker.patch(
            "shared.airtable_client.update_record",
            side_effect=lambda *a, **k: self.events.append("airtable:patch") or {},
        )

        # --- leadsun ---
        self.get_token = mocker.patch(
            "shared.leadsun_project_populator.get_token",
            side_effect=lambda u, p: self.events.append("leadsun:login") or {"access_token": "TOK", "refresh_token": "R"},
        )
        self.list_projects = mocker.patch(
            "shared.leadsun_project_populator.list_projects", side_effect=self._list
        )
        self.upsert_project = mocker.patch(
            "shared.leadsun_project_populator.upsert_project", side_effect=self._upsert
        )
        self.sleep = mocker.patch("shared.leadsun_project_populator.time.sleep")

    def _upsert(self, token, name, remark):
        self.events.append("leadsun:upsert")
        self.upserted = True
        return {"success": True}

    def _list(self, token):
        self.events.append("leadsun:list")
        projects = list(self.leadsun_projects)
        if self.upserted:
            self._looks_after_upsert += 1
            if self._looks_after_upsert >= self.appears_on_look:
                projects.append(dict(NEW))
        return projects

    def run(self, **overrides):
        kwargs = {"project_id": PROJECT_ID, "project_name": NAME, "remark": None}
        kwargs.update(overrides)
        return m.populate_leadsun_project(**kwargs)

    def index(self, event):
        return self.events.index(event)

    def nothing_external_happened(self):
        return not any(e.startswith(("leadsun:", "airtable:patch")) for e in self.events)


@pytest.fixture
def world(mocker, monkeypatch):
    return World(mocker, monkeypatch)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------
class TestHappyPath:
    def test_returns_what_was_linked(self, world):
        result = world.run(remark="phase 2")
        assert result == {
            "projectId": PROJECT_ID,
            "projectName": NAME,
            "leadsunProject": {"ProjectId": "389"},
        }

    def test_the_new_leadsun_id_is_reported_once_as_leadsunproject_projectid(self, world):
        """No separate leadsunProjectId on success -- it would only repeat
        leadsunProject.ProjectId, which IS the value that was stored."""
        result = world.run()
        assert "leadsunProjectId" not in result
        assert set(result) == {"projectId", "projectName", "leadsunProject"}
        assert result["leadsunProject"] == {"ProjectId": "389"}
        stored_param = world.cursor.execute.call_args_list[-1].args[1]  # first bound value of the UPDATE
        assert stored_param == result["leadsunProject"]["ProjectId"]

    def test_logs_in_with_the_configured_admin_account(self, world):
        world.run()
        world.get_token.assert_called_once_with("ADMIN", "secret")

    def test_creates_the_project_with_the_trimmed_name_and_remark(self, world):
        world.run(project_name="  Pioneer Ranch  ", remark="  phase 2  ")
        world.upsert_project.assert_called_once_with("TOK", "Pioneer Ranch", "phase 2")

    def test_remark_defaults_to_empty(self, world):
        world.run()
        world.upsert_project.assert_called_once_with("TOK", NAME, "")

    def test_the_full_sequence(self, world):
        world.run()
        assert world.events == [
            "db:connect", "db:SELECT", "db:close",      # 2. project exists + blank
            "airtable:get",                              # 2. airtable field empty too
            "leadsun:login",                             # 3.
            "leadsun:list",                              # 3. name-free check / ids before
            "leadsun:upsert",                            # 4.
            "leadsun:list",                              # 5. find the new id
            "airtable:patch",                            # 6. Airtable BEFORE the database
            "db:connect", "db:UPDATE", "db:commit", "db:close",   # 7.
        ]

    def test_airtable_is_patched_with_the_id_as_text_and_typecast(self, world):
        world.run()
        world.update_record.assert_called_once_with(
            projects_loader.AIRTABLE_PROJECTS_TABLE,
            PROJECT_ID,
            {"Leadsun ProjectID": "389"},
            typecast=True,
        )

    def test_the_project_record_is_looked_up_in_the_projects_table(self, world):
        world.run()
        world.fetch_record.assert_called_once_with(projects_loader.AIRTABLE_PROJECTS_TABLE, PROJECT_ID)

    def test_the_database_is_filled_conditional_on_the_value_it_read(self, world):
        world.run()
        sql, *params = world.cursor.execute.call_args_list[-1].args
        assert sql.startswith("UPDATE Projects SET LeadsunProject = ")
        assert "WHERE Id = ? AND LeadsunProject IS NULL" in sql
        assert params == ["389", PROJECT_ID]

    def test_a_blank_but_not_null_value_is_matched_exactly(self, world):
        world.cursor.fetchone.return_value = (PROJECT_ID, "{}")
        world.run()
        sql, *params = world.cursor.execute.call_args_list[-1].args
        assert "WHERE Id = ? AND LeadsunProject = ?" in sql
        assert params == ["389", PROJECT_ID, "{}"]


# --------------------------------------------------------------------------
# Same format as the LeadsunProject loader
# --------------------------------------------------------------------------
class TestSameFormatAsTheLoader:
    def test_uses_the_loaders_own_json_expression(self, world):
        """projects_loader's INSERT builds the column with
        JSON_MODIFY('{}', '$.ProjectId', source.LeadsunProjectIdValue), where
        that value is CAST(? AS NVARCHAR(MAX)) -- so the key is "ProjectId" and
        its value a JSON string. This writes the same thing."""
        loader_sql = projects_loader._PROJECT_UPSERT_SQL
        assert "JSON_MODIFY('{}', '$.ProjectId', source.LeadsunProjectIdValue)" in loader_sql
        assert "CAST(? AS NVARCHAR(MAX)) AS LeadsunProjectIdValue" in loader_sql

        world_sql = None

        def capture(sql, *a):
            nonlocal world_sql
            if sql.startswith("UPDATE"):
                world_sql = sql

        world.cursor.execute.side_effect = capture
        world.run()
        assert "JSON_MODIFY('{}', '$.ProjectId', CAST(? AS NVARCHAR(MAX)))" in world_sql

    def test_writes_only_the_project_id_key_leaving_the_rest_to_the_telemetry_job(self, world):
        world.run()
        sql, *_ = world.cursor.execute.call_args_list[-1].args
        for key in ("ProjectName", "totalGateways", "totalPoles", "groups"):
            assert key not in sql

    def test_the_airtable_field_is_the_one_the_loader_reads(self):
        record = {"id": "rec1", "fields": {m.AIRTABLE_LEADSUN_PROJECT_ID_FIELD: 389}}
        assert projects_loader._map_record_to_project(record)["LeadsunProjectIdValue"] == 389

    def test_the_key_is_the_one_the_details_job_and_id_lookup_use(self):
        from shared import pole_telemetry_loader

        assert "$.ProjectId" in pole_telemetry_loader._FETCH_PROJECT_LEADSUN_IDS_SQL


# --------------------------------------------------------------------------
# "The current Projects.LeadsunProject has to be blank"
# --------------------------------------------------------------------------
class TestMustBeBlank:
    @pytest.mark.parametrize("blank", [None, "", "   ", "{}", " { } ", "null"])
    def test_blank_values_are_accepted(self, world, blank):
        world.cursor.fetchone.return_value = (PROJECT_ID, blank)
        assert world.run()["leadsunProject"]["ProjectId"] == "389"

    @pytest.mark.parametrize(
        "populated",
        ['{"ProjectId": "5"}', '{"ProjectName": "x"}', '{"ProjectId":"5","groups":[]}', "not json", "[]", "0"],
    )
    def test_anything_else_is_a_409_and_nothing_external_happens(self, world, populated):
        world.cursor.fetchone.return_value = (PROJECT_ID, populated)
        with pytest.raises(m.PopulateConflictError, match="must be blank") as exc:
            world.run()
        assert exc.value.http_status == 409
        assert world.nothing_external_happened()
        assert "airtable:get" not in world.events

    def test_an_unknown_project_is_a_404_and_nothing_external_happens(self, world):
        world.cursor.fetchone.return_value = None
        with pytest.raises(m.ProjectNotFoundError, match=PROJECT_ID) as exc:
            world.run()
        assert exc.value.http_status == 404
        assert world.nothing_external_happened()
        assert "airtable:get" not in world.events


# --------------------------------------------------------------------------
# Data scope: which Projects a caller may populate
# --------------------------------------------------------------------------
class TestDataScope:
    def _read_sql(self, world):
        sql, *params = world.cursor.execute.call_args_list[0].args
        return sql, params

    def test_unrestricted_reads_the_project_by_id_alone(self, world):
        world.run(scope=UNRESTRICTED)
        sql, params = self._read_sql(world)
        assert sql == "SELECT Id, LeadsunProject FROM Projects WHERE Id = ?"
        assert params == [PROJECT_ID]

    def test_the_default_is_unrestricted(self, world):
        world.run()
        assert self._read_sql(world)[0] == "SELECT Id, LeadsunProject FROM Projects WHERE Id = ?"

    def test_crew_may_only_populate_a_project_with_an_installation_schedule(self, world):
        world.run(scope=CREW)
        sql, params = self._read_sql(world)
        assert sql == "SELECT Id, LeadsunProject FROM Projects WHERE Id = ? AND StickerNumbers IS NOT NULL"
        assert params == [PROJECT_ID]

    def test_a_customer_scope_pins_the_customer(self, world):
        world.run(scope=CUSTOMER)
        sql, params = self._read_sql(world)
        assert sql == "SELECT Id, LeadsunProject FROM Projects WHERE Id = ? AND CustomerId = ?"
        assert params == [PROJECT_ID, "cust-own"]

    def test_a_project_outside_the_scope_is_a_404_and_nothing_external_happens(self, world):
        world.cursor.fetchone.return_value = None  # the scoped SELECT finds no row
        with pytest.raises(m.ProjectNotFoundError) as exc:
            world.run(scope=CREW)
        assert exc.value.http_status == 404
        assert world.nothing_external_happened()
        assert "airtable:get" not in world.events  # not even Airtable is asked about it

    def test_out_of_scope_looks_exactly_like_nonexistent(self, world):
        """Same status AND message, so a Crew caller can't tell a project they
        may not see from one that doesn't exist."""
        world.cursor.fetchone.return_value = None
        with pytest.raises(m.ProjectNotFoundError) as unrestricted:
            world.run(scope=UNRESTRICTED)
        with pytest.raises(m.ProjectNotFoundError) as scoped:
            world.run(scope=CREW)
        assert str(unrestricted.value) == str(scoped.value)
        # and the wording itself says "not found", never anything about scope or permission
        assert str(scoped.value) == f"no Project found with ProjectId '{PROJECT_ID}'"
        for hint in ("scope", "permission", "forbidden", "outside", "allowed"):
            assert hint not in str(scoped.value).lower()

    def test_scope_changes_nothing_else_about_a_successful_run(self, world):
        unrestricted_events = None
        world.run(scope=UNRESTRICTED)
        unrestricted_events = list(world.events)
        world.events.clear()
        world.upserted = False
        world._looks_after_upsert = 0
        result = world.run(scope=CREW)
        assert world.events == unrestricted_events
        assert result["leadsunProject"]["ProjectId"] == "389"

    def test_the_final_update_still_targets_exactly_this_project(self, world):
        world.run(scope=CREW)
        sql, *params = world.cursor.execute.call_args_list[-1].args
        assert "WHERE Id = ? AND LeadsunProject IS NULL" in sql
        assert params == ["389", PROJECT_ID]


# --------------------------------------------------------------------------
# Input and configuration
# --------------------------------------------------------------------------
class TestValidation:
    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_project_id_is_required(self, world, bad):
        with pytest.raises(ValueError, match="ProjectId is required"):
            world.run(project_id=bad)
        assert world.events == []

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_project_name_is_required(self, world, bad):
        with pytest.raises(ValueError, match="ProjectName is required"):
            world.run(project_name=bad)
        assert world.events == []

    @pytest.mark.parametrize("field,key", [("ProjectId", "project_id"), ("ProjectName", "project_name")])
    def test_non_string_values_are_rejected(self, world, field, key):
        with pytest.raises(ValueError, match=f"{field} must be a string"):
            world.run(**{key: 123})
        assert world.events == []

    def test_remark_is_optional_but_must_be_text(self, world):
        with pytest.raises(ValueError, match="remark must be a string"):
            world.run(remark=5)
        assert world.events == []

    @pytest.mark.parametrize("missing", ["LEADSUN_EDGE_ADMIN_USERNAME", "LEADSUN_EDGE_ADMIN_PASSWORD"])
    def test_missing_credentials_fail_before_anything_is_touched(self, world, monkeypatch, missing):
        monkeypatch.delenv(missing)
        with pytest.raises(m.LeadsunCredentialsNotConfiguredError, match=missing) as exc:
            world.run()
        assert exc.value.http_status == 500
        assert world.events == []

    def test_empty_credentials_count_as_missing(self, world, monkeypatch):
        monkeypatch.setenv("LEADSUN_EDGE_ADMIN_PASSWORD", "")
        with pytest.raises(m.LeadsunCredentialsNotConfiguredError):
            world.run()


# --------------------------------------------------------------------------
# Pre-flight: nothing is created until every check has passed
# --------------------------------------------------------------------------
class TestPreflight:
    def test_the_database_connection_is_closed_before_leadsun_is_called(self, world):
        """A connection left idle across slow external calls is exactly how
        the loaders ended up with dropped-connection failures."""
        world.run()
        assert world.index("db:close") < world.index("leadsun:login")

    def test_an_airtable_value_that_already_exists_is_a_409_and_creates_nothing(self, world):
        world.airtable_record = {"id": PROJECT_ID, "fields": {"Leadsun ProjectID": 77}}
        with pytest.raises(m.PopulateConflictError, match="Airtable already has") as exc:
            world.run()
        assert exc.value.http_status == 409
        assert world.nothing_external_happened()

    @pytest.mark.parametrize("empty", [None, "", "  ", []])
    def test_empty_airtable_values_pass(self, world, empty):
        world.airtable_record = {"id": PROJECT_ID, "fields": {"Leadsun ProjectID": empty}}
        assert world.run()["leadsunProject"]["ProjectId"] == "389"

    def test_an_unreadable_airtable_record_is_a_502_and_creates_nothing(self, world):
        world.fetch_record.side_effect = AirtableApiError("Airtable GET T/rec failed (404): NOT_FOUND")
        with pytest.raises(m.AirtableError, match="could not read") as exc:
            world.run()
        assert exc.value.http_status == 502
        assert exc.value.leadsun_project_id is None
        assert world.nothing_external_happened()

    def test_a_leadsun_login_failure_creates_nothing_and_writes_nothing(self, world):
        world.get_token.side_effect = LeadsunEdgeApiError("Invalid credentials")
        with pytest.raises(LeadsunEdgeApiError, match="Invalid credentials"):
            world.run()
        assert "leadsun:upsert" not in world.events
        assert "airtable:patch" not in world.events
        assert world.events.count("db:UPDATE") == 0


class TestNameAlreadyInLeadsun:
    """'upsert' may UPDATE an existing project of the same name; linking to
    one this call didn't create would tie our telemetry to somebody else's."""

    @pytest.mark.parametrize("existing_name", [NAME, "pioneer ranch", "  PIONEER RANCH  "])
    def test_an_existing_name_is_a_409_and_nothing_is_created_or_written(self, world, existing_name):
        world.leadsun_projects = EXISTING + [{"id": 55, "name": existing_name}]
        with pytest.raises(m.PopulateConflictError, match=r"already exists \(id 55\)") as exc:
            world.run()
        assert exc.value.http_status == 409
        assert "leadsun:upsert" not in world.events
        assert "airtable:patch" not in world.events
        assert world.events.count("db:UPDATE") == 0

    def test_a_different_name_is_fine(self, world):
        world.leadsun_projects = EXISTING + [{"id": 55, "name": "Pioneer Ranch Phase 2"}]
        assert world.run()["leadsunProject"]["ProjectId"] == "389"


# --------------------------------------------------------------------------
# Finding the NEW project's id after the upsert
# --------------------------------------------------------------------------
class TestFindingTheNewProject:
    def test_found_on_the_first_look(self, world):
        world.run()
        assert world.sleep.call_args_list == [call(m._LIST_DELAYS_SECONDS[0])]

    def test_waits_and_looks_again_until_it_appears(self, world):
        world.appears_on_look = 3
        result = world.run()
        assert result["leadsunProject"]["ProjectId"] == "389"
        assert world.sleep.call_args_list == [call(d) for d in m._LIST_DELAYS_SECONDS[:3]]
        assert world.events.count("leadsun:list") == 1 + 3  # the before-snapshot + three looks

    def test_never_appearing_is_a_502_and_nothing_is_linked(self, world):
        world.appears_on_look = 999
        with pytest.raises(m.LeadsunProjectLookupError, match="never appeared") as exc:
            world.run()
        assert exc.value.http_status == 502
        assert world.events.count("leadsun:list") == 1 + len(m._LIST_DELAYS_SECONDS)
        assert "airtable:patch" not in world.events
        assert world.events.count("db:UPDATE") == 0

    def test_two_new_projects_with_that_name_are_ambiguous_and_nothing_is_linked(self, world):
        original = world._list

        def two_new(token):
            projects = original(token)
            if world.upserted:
                projects.append({"id": 390, "name": NAME})
            return projects

        world.list_projects.side_effect = two_new
        with pytest.raises(m.LeadsunProjectLookupError, match="more than one NEW"):
            world.run()
        assert "airtable:patch" not in world.events
        assert world.events.count("db:UPDATE") == 0

    def test_only_a_project_that_was_not_listed_before_counts_as_new(self, world):
        # a lookalike with a DIFFERENT name that was already there must not be picked
        world.leadsun_projects = EXISTING + [{"id": 55, "name": NAME + " 2"}]
        assert world.run()["leadsunProject"]["ProjectId"] == "389"

    def test_a_failure_while_listing_after_the_upsert_says_the_project_was_accepted(self, world):
        calls = {"n": 0}
        original = world._list

        def fail_after_upsert(token):
            calls["n"] += 1
            if world.upserted:
                raise LeadsunEdgeApiError("boom")
            return original(token)

        world.list_projects.side_effect = fail_after_upsert
        with pytest.raises(m.LeadsunProjectLookupError, match="accepted project .* listing projects afterwards failed"):
            world.run()
        assert "airtable:patch" not in world.events

    def test_a_failed_upsert_stops_everything(self, world):
        world.upsert_project.side_effect = LeadsunEdgeApiError("name too long")
        with pytest.raises(LeadsunEdgeApiError, match="name too long"):
            world.run()
        assert world.events.count("leadsun:list") == 1  # only the before-snapshot
        assert "airtable:patch" not in world.events

    def test_the_helper_ignores_a_same_named_project_that_was_already_listed(self, world):
        """_find_new_project_id's own contract: 'new' means not in the listing taken
        BEFORE the upsert -- independent of the caller's name-clash check."""
        world.list_projects.side_effect = lambda token: [
            {"id": 55, "name": NAME},   # already existed before the upsert
            {"id": 389, "name": NAME},  # the one the upsert just created
        ]
        assert m._find_new_project_id("TOK", NAME, ids_before={"55"}) == "389"

    def test_the_helper_reports_ambiguity_when_nothing_distinguishes_the_new_one(self, world):
        world.list_projects.side_effect = lambda token: [{"id": 55, "name": NAME}, {"id": 389, "name": NAME}]
        with pytest.raises(m.LeadsunProjectLookupError, match="more than one NEW"):
            m._find_new_project_id("TOK", NAME, ids_before=set())

    def test_the_id_may_arrive_as_a_number_or_text(self, world):
        original = world._list

        def text_id(token):
            projects = original(token)
            return [dict(p, id=str(p["id"])) for p in projects]

        world.list_projects.side_effect = text_id
        assert world.run()["leadsunProject"]["ProjectId"] == "389"


# --------------------------------------------------------------------------
# Partial failures AFTER the Leadsun project exists
# --------------------------------------------------------------------------
class TestFailuresAfterTheLeadsunProjectExists:
    def test_an_airtable_failure_changes_nothing_locally_and_reports_the_leadsun_id(self, world):
        world.update_record.side_effect = AirtableApiError("Airtable PATCH T/rec failed (403): INVALID_PERMISSIONS")
        with pytest.raises(m.AirtableError) as exc:
            world.run()
        assert exc.value.http_status == 502
        assert exc.value.leadsun_project_id == "389"
        assert "Set it to 389" in str(exc.value) and "database was not changed" in str(exc.value)
        assert world.events.count("db:UPDATE") == 0  # nothing local for loadProjects to later undo

    def test_airtable_is_written_before_the_database(self, world):
        world.run()
        assert world.index("airtable:patch") < world.index("db:UPDATE")

    def test_a_database_failure_leaves_airtable_correct_and_reports_the_id(self, world):
        def fail_on_update(sql, *a):
            world.events.append(f"db:{sql.split()[0]}")
            if sql.startswith("UPDATE"):
                raise RuntimeError("connection reset")

        world.cursor.execute.side_effect = fail_on_update
        with pytest.raises(m.DatabaseUpdateError, match="Airtable updated") as exc:
            world.run()
        assert exc.value.http_status == 500
        assert exc.value.leadsun_project_id == "389"
        assert "loadProjects will fill" in str(exc.value)
        assert "airtable:patch" in world.events

    def test_a_value_that_changed_underneath_is_a_409_not_an_overwrite(self, world):
        world.cursor.rowcount = 0  # the conditional UPDATE matched nothing
        with pytest.raises(m.PopulateConflictError, match="changed while this ran") as exc:
            world.run()
        assert exc.value.http_status == 409
        assert exc.value.leadsun_project_id == "389"

    def test_the_connection_is_closed_even_when_the_update_fails(self, world):
        world.cursor.execute.side_effect = lambda sql, *a: (
            (_ for _ in ()).throw(RuntimeError("x")) if sql.startswith("UPDATE") else None
        )
        with pytest.raises(m.DatabaseUpdateError):
            world.run()
        assert world.conn.close.call_count == 2  # the read, and the failed write


class TestErrorStatuses:
    @pytest.mark.parametrize(
        "error,status",
        [
            (m.ProjectNotFoundError, 404),
            (m.PopulateConflictError, 409),
            (m.LeadsunProjectLookupError, 502),
            (m.AirtableError, 502),
            (m.DatabaseUpdateError, 500),
            (m.LeadsunCredentialsNotConfiguredError, 500),
        ],
    )
    def test_each_error_carries_its_http_status(self, error, status):
        assert error("x").http_status == status
        assert issubclass(error, m.PopulateError)

    def test_the_leadsun_id_is_optional_on_every_error(self):
        assert m.PopulateError("x").leadsun_project_id is None
        assert m.PopulateError("x", leadsun_project_id="9").leadsun_project_id == "9"
