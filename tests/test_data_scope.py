"""
Tests for shared/data_scope.py and for how every read API applies it.

The point of these tests is the SQL each query actually sends: scope must
be enforced IN the statement (and inside the TOP subquery for list calls),
never by filtering rows afterwards, and an unrestricted caller must still
get the original, unscoped query.
"""

import pytest

from shared import (
    customers_api,
    data_scope,
    pole_vitals_api,
    poles_api,
    projects_api,
    users_api,
)
from shared.auth_utils import AuthError
from shared.data_scope import UNRESTRICTED, DataScope

CUSTOMER = DataScope(customer_id="cust-own")
CREW = DataScope(scheduled_projects_only=True)
ROLLUP = pole_vitals_api._ROLLUP_PERIOD_TYPE


class _Ctx:
    def __init__(self, role, customer_id=None, user_id="user-1"):
        self.role = role
        self.customer_id = customer_id
        self.user_id = user_id
        self.session_id = "sess"


# --------------------------------------------------------------------------
# scope_for(): role -> scope
# --------------------------------------------------------------------------
class TestScopeFor:
    def test_streetleaf_admin_is_unrestricted(self):
        scope = data_scope.scope_for(_Ctx("Streetleaf Admin"))
        assert scope == UNRESTRICTED
        assert not scope.is_restricted

    @pytest.mark.parametrize("role", ["Customer Owner", "Customer Admin", "User"])
    def test_customer_roles_are_pinned_to_their_customer(self, role):
        scope = data_scope.scope_for(_Ctx(role, customer_id="cust-9"))
        assert scope.customer_id == "cust-9"
        assert scope.is_restricted
        assert not scope.scheduled_projects_only

    def test_impersonating_streetleaf_admin_gets_the_impersonated_customer(self):
        # signIn with a customerId creates a session whose role is
        # 'Customer Owner' and whose customer is the one impersonated.
        scope = data_scope.scope_for(_Ctx("Customer Owner", customer_id="impersonated"))
        assert scope.customer_id == "impersonated"

    def test_crew_is_limited_to_scheduled_projects_but_not_to_a_customer(self):
        scope = data_scope.scope_for(_Ctx("Streetleaf Crew", user_id="crew-7"))
        assert scope.scheduled_projects_only is True
        assert scope.customer_id is None
        assert scope.is_restricted

    @pytest.mark.parametrize("role", ["Customer Owner", "Customer Admin", "User"])
    def test_customer_role_without_a_customer_fails_closed(self, role):
        with pytest.raises(AuthError) as exc:
            data_scope.scope_for(_Ctx(role, customer_id=None))
        assert exc.value.status_code == 403

    def test_unknown_role_fails_closed(self):
        with pytest.raises(AuthError) as exc:
            data_scope.scope_for(_Ctx("Intern"))
        assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# SQL condition helpers
# --------------------------------------------------------------------------
class TestFilters:
    def test_unrestricted_adds_nothing(self):
        for fn in (data_scope.customer_filter, data_scope.project_filter, data_scope.user_filter):
            assert fn(UNRESTRICTED) == ([], [])

    def test_customer_filter_for_customer_scope(self):
        assert data_scope.customer_filter(CUSTOMER, "c") == (["c.Id = ?"], ["cust-own"])

    def test_customer_filter_bare_table_uses_the_table_name(self):
        assert data_scope.customer_filter(CUSTOMER) == (["Customers.Id = ?"], ["cust-own"])

    def test_customer_filter_for_crew_requires_a_scheduled_project(self):
        conditions, params = data_scope.customer_filter(CREW, "c")
        assert params == []
        assert len(conditions) == 1
        assert "EXISTS (SELECT 1 FROM Projects sp" in conditions[0]
        assert "sp.CustomerId = c.Id" in conditions[0]
        assert "sp.StickerNumbers IS NOT NULL" in conditions[0]

    def test_project_filter(self):
        assert data_scope.project_filter(CUSTOMER, "proj") == (["proj.CustomerId = ?"], ["cust-own"])
        assert data_scope.project_filter(CREW, "proj") == (["proj.StickerNumbers IS NOT NULL"], [])
        assert data_scope.project_filter(CUSTOMER) == (["CustomerId = ?"], ["cust-own"])

    def test_user_filter(self):
        assert data_scope.user_filter(CUSTOMER) == (["u.CustomerId = ?"], ["cust-own"])
        # Crew is limited on customers/projects/poles, NOT on users
        assert data_scope.user_filter(CREW) == ([], [])


# --------------------------------------------------------------------------
# customers_api
# --------------------------------------------------------------------------
class TestCustomersScoped:
    def test_customer_scope_list(self, patch_get_connection_customers_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        customers_api.get_customers(scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE Customers.Id = ?" in sql and "ORDER BY Name" in sql
        assert params == [customers_api.clamp_limit(None), "cust-own"]

    def test_customer_scope_cannot_look_up_another_customer(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        result = customers_api.get_customers(customer_id="cust-other", scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE Id = ? AND Customers.Id = ?" in sql
        assert params == ["cust-other", "cust-own"]  # can never both match
        assert result == []

    def test_crew_only_sees_customers_with_a_scheduled_project(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        customers_api.get_customers(scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "EXISTS (SELECT 1 FROM Projects sp" in sql
        assert "sp.StickerNumbers IS NOT NULL" in sql
        assert params == [customers_api.clamp_limit(None)]

    def test_scope_is_combined_with_the_active_filter(
        self, patch_get_connection_customers_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        customers_api.get_customers(active=True, scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE Active = ? AND Customers.Id = ?" in sql
        assert params == [customers_api.clamp_limit(None), 1, "cust-own"]

    def test_unrestricted_has_no_where_clause(self, patch_get_connection_customers_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        customers_api.get_customers(scope=UNRESTRICTED)
        sql, *_ = mock_cursor.execute.call_args.args
        assert "WHERE" not in sql


# --------------------------------------------------------------------------
# projects_api
# --------------------------------------------------------------------------
class TestProjectsScoped:
    def test_customer_scope_list(self, patch_get_connection_projects_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        projects_api.get_projects(scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE CustomerId = ?" in sql and "ORDER BY Name" in sql
        assert params == [projects_api.clamp_limit(None), "cust-own"]

    def test_customer_scope_with_another_customers_filter_matches_nothing(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        projects_api.get_projects(customer_id="cust-other", scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE CustomerId = ? AND CustomerId = ?" in sql
        assert params == [projects_api.clamp_limit(None), "cust-other", "cust-own"]

    def test_crew_list_only_scheduled_projects(self, patch_get_connection_projects_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        projects_api.get_projects(scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE StickerNumbers IS NOT NULL" in sql
        assert params == [projects_api.clamp_limit(None)]

    def test_crew_single_project_lookup_is_scope_checked(
        self, patch_get_connection_projects_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        projects_api.get_projects(project_id="proj-unscheduled", scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE Id = ? AND StickerNumbers IS NOT NULL" in sql
        assert "TOP" not in sql
        assert params == ["proj-unscheduled"]

    def test_unrestricted_single_lookup_unchanged(self, patch_get_connection_projects_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        projects_api.get_projects(project_id="p1")
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE Id = ?" in sql and "StickerNumbers IS NOT NULL" not in sql
        assert params == ["p1"]


# --------------------------------------------------------------------------
# users_api
# --------------------------------------------------------------------------
class TestUsersScoped:
    def test_customer_scope_list_is_limited_before_the_top(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        users_api.get_users(scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE u.CustomerId = ? ORDER BY u.Name" in sql
        assert sql.index("TOP (?)") < sql.index("WHERE")
        assert params == [users_api.clamp_limit(None), "cust-own"]

    def test_customer_scope_cannot_read_another_customers_user(
        self, patch_get_connection_users_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        users_api.get_users(user_id="someone-else", customer_id="cust-other", scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "u.Id = ? AND u.CustomerId = ? AND u.CustomerId = ?" in sql
        assert params == ["someone-else", "cust-other", "cust-own"]

    def test_crew_still_sees_all_users(self, patch_get_connection_users_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        users_api.get_users(scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE" not in sql  # the original, unrestricted user list
        assert params == [users_api.clamp_limit(None)]

    def test_crew_single_user_lookup_is_not_narrowed(self, patch_get_connection_users_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        users_api.get_users(user_id="any-user", scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE u.Id = ?" in sql and "AND" not in sql.split("WHERE")[1]
        assert params == ["any-user"]

    def test_unrestricted_list_is_the_original_query(self, patch_get_connection_users_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        users_api.get_users(scope=UNRESTRICTED)
        sql, *_ = mock_cursor.execute.call_args.args
        assert "WHERE" not in sql and sql.rstrip().endswith("ORDER BY u.Name")


# --------------------------------------------------------------------------
# poles_api
# --------------------------------------------------------------------------
class TestPolesScoped:
    def test_customer_scope_with_a_project_filter(self, patch_get_connection_poles_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(project_id="proj1", scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE proj.Id = ? AND c.Id = ?" in sql
        assert params == [ROLLUP, "proj1", "cust-own"]

    def test_crew_with_a_customer_filter(self, patch_get_connection_poles_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(customer_id="cust1", scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "WHERE c.Id = ? AND proj.StickerNumbers IS NOT NULL" in sql
        assert params == [ROLLUP, "cust1"]

    def test_out_of_scope_pole_lookup_returns_none(self, patch_get_connection_poles_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        assert poles_api.get_poles(pole_id="pole-of-another-customer", scope=CUSTOMER) is None
        sql, *params = mock_cursor.execute.call_args.args
        assert "p.Id = ?" in sql and "c.Id = ?" in sql
        assert params == [ROLLUP, "pole-of-another-customer", "cust-own"]

    @pytest.mark.parametrize("summary", [False, True])
    def test_customer_scope_unfiltered_applies_inside_the_top_subquery(
        self, patch_get_connection_poles_api, mock_cursor, summary
    ):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(summary=summary, scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        sub = sql[sql.index("SELECT TOP (?) sp.Id"): sql.index("ORDER BY sp.PoleNumber")]
        assert "JOIN Projects spr ON sp.ProjectId = spr.Id" in sub
        assert "spr.CustomerId = ?" in sub  # inside the subquery => the limit counts only in-scope poles
        assert params[0] == ROLLUP and params[2:] == ["cust-own"]

    def test_crew_unfiltered_applies_inside_the_top_subquery(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        sub = sql[sql.index("SELECT TOP (?) sp.Id"): sql.index("ORDER BY sp.PoleNumber")]
        assert "spr.StickerNumbers IS NOT NULL" in sub
        assert len(params) == 2  # rollup period + limit; no scope params

    def test_active_filter_params_stay_in_order_with_scope(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(active=True, scope=CUSTOMER)
        sql, *params = mock_cursor.execute.call_args.args
        # placeholders appear: rollup join, TOP, scope customer, then Active
        assert params == [ROLLUP, poles_api.clamp_limit(None), "cust-own", 1]
        assert sql.count("?") == len(params)

    def test_unrestricted_unfiltered_is_the_original_query(
        self, patch_get_connection_poles_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(scope=UNRESTRICTED)
        sql, *params = mock_cursor.execute.call_args.args
        assert "spr" not in sql
        assert "WHERE p.Id IN (SELECT TOP (?) Id FROM Poles ORDER BY PoleNumber)" in sql
        assert params == [ROLLUP, poles_api.clamp_limit(None)]


# --------------------------------------------------------------------------
# pole_vitals_api
# --------------------------------------------------------------------------
class TestPoleVitalsScoped:
    def _both_queries(self, mock_cursor):
        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2  # rollup + per-pole details share one where_clause
        return calls

    def test_customer_scope_unfiltered_limits_inside_the_customers_subquery(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        pole_vitals_api.get_pole_vitals(scope=CUSTOMER)
        for call in self._both_queries(mock_cursor):
            sql, *params = call.args
            assert "SELECT TOP (?) Id FROM Customers WHERE Customers.Id = ? ORDER BY Name" in sql
            assert params == [ROLLUP, pole_vitals_api.clamp_limit(None), "cust-own"]

    def test_crew_unfiltered_scopes_both_the_customers_and_their_projects(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        pole_vitals_api.get_pole_vitals(scope=CREW)
        for call in self._both_queries(mock_cursor):
            sql, *params = call.args
            sub = sql[sql.index("SELECT TOP (?) Id FROM Customers"): sql.index("ORDER BY Name)")]
            assert "EXISTS (SELECT 1 FROM Projects sp" in sub
            assert "AND proj.StickerNumbers IS NOT NULL" in sql[sql.index("ORDER BY Name)"):]
            assert params == [ROLLUP, pole_vitals_api.clamp_limit(None)]

    def test_customer_scope_cannot_request_another_customer(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        result = pole_vitals_api.get_pole_vitals(customer_id="cust-other", scope=CUSTOMER)
        assert result is None  # -> 404, indistinguishable from "doesn't exist"
        for call in self._both_queries(mock_cursor):
            sql, *params = call.args
            assert "WHERE c.Id = ? AND c.Id = ?" in sql
            assert params == [ROLLUP, "cust-other", "cust-own"]

    def test_crew_project_lookup_requires_a_scheduled_project(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchall.return_value = []
        assert pole_vitals_api.get_pole_vitals(project_id="p-unscheduled", scope=CREW) is None
        for call in self._both_queries(mock_cursor):
            sql, *params = call.args
            assert "WHERE proj.Id = ? AND proj.StickerNumbers IS NOT NULL" in sql
            assert params == [ROLLUP, "p-unscheduled"]

    def test_unrestricted_is_the_original_query(self, patch_get_connection_pole_vitals_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        pole_vitals_api.get_pole_vitals(scope=UNRESTRICTED)
        for call in self._both_queries(mock_cursor):
            sql, *params = call.args
            assert "WHERE c.Id IN (SELECT TOP (?) Id FROM Customers ORDER BY Name)" in sql
            assert "StickerNumbers" not in sql.split("WHERE c.Id IN")[1]
            assert params == [ROLLUP, pole_vitals_api.clamp_limit(None)]


class TestPoleVitalsByPeriodScoped:
    def test_out_of_scope_pole_is_not_found_and_history_is_never_queried(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None  # the scoped info query finds nothing
        result = pole_vitals_api.get_pole_vitals_by_period("pole-of-other", "Hour", scope=CUSTOMER)
        assert result is None
        assert mock_cursor.execute.call_count == 1  # no history query for a pole you can't see
        sql, *params = mock_cursor.execute.call_args.args
        assert "AND EXISTS (SELECT 1 FROM Projects spr WHERE spr.Id = p.ProjectId AND spr.CustomerId = ?)" in sql
        assert params == ["pole-of-other", "cust-own"]

    def test_crew_scope_requires_a_scheduled_project(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None
        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour", scope=CREW)
        sql, *params = mock_cursor.execute.call_args.args
        assert "spr.StickerNumbers IS NOT NULL" in sql
        assert params == ["pole1"]

    def test_unrestricted_runs_the_original_info_query(
        self, patch_get_connection_pole_vitals_api, mock_cursor
    ):
        mock_cursor.fetchone.return_value = None
        pole_vitals_api.get_pole_vitals_by_period("pole1", "Hour")
        sql, *params = mock_cursor.execute.call_args.args
        assert sql == pole_vitals_api._POLE_INFO_FOR_HISTORY_SQL_TEMPLATE
        assert params == ["pole1"]
