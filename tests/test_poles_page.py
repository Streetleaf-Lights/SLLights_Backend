"""
Tests for poles_api.get_poles_page() -- the paginated getPoles query.

No SQL Server here, so these pin the SQL it SENDS: which query runs first,
that the count and the page share byte-identical filters, that every `?`
has a parameter in the right order, and that the expensive per-pole query
only runs for rows on the requested page.
"""

import re

import pytest

from shared import poles_api
from shared.data_scope import UNRESTRICTED, DataScope
from tests.test_poles_api import TestPoleRowToDictWithParents, _summary_row

ROLLUP = poles_api._ROLLUP_PERIOD_TYPE
CUSTOMER = DataScope(customer_id="cust-own")
CREW = DataScope(scheduled_projects_only=True)

BASE_FROM = (
    "FROM Poles sp JOIN Projects spr ON sp.ProjectId = spr.Id "
    "JOIN Customers sc ON spr.CustomerId = sc.Id"
)
FAULT_JOIN = (
    " JOIN PoleVitals sv ON COALESCE(sp.VendorPoleId, sp.ProvisionedPoleId) = sv.PoleId"
    " AND sv.PeriodType = ?"
)


def _detail_row():
    return TestPoleRowToDictWithParents()._detail_row(None)


@pytest.fixture
def db(patch_get_connection_poles_api, mock_cursor):
    mock_cursor.fetchone.return_value = (0,)
    mock_cursor.fetchall.return_value = []
    return mock_cursor


def _queries(cursor):
    """[(sql, params)] for each execute() call, in order."""
    return [(c.args[0], list(c.args[1:])) for c in cursor.execute.call_args_list]


class TestShape:
    def test_returns_total_page_pagesize_and_items(self, db):
        db.fetchone.return_value = (13950,)
        db.fetchall.return_value = [_detail_row()]

        result = poles_api.get_poles_page(page=2, page_size=10)

        assert set(result) == {"total", "page", "pageSize", "items"}
        assert (result["total"], result["page"], result["pageSize"]) == (13950, 2, 10)
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["id"] == "pole1" and item["customerId"] == "cust1" and item["controllerId"] == "CTRL-1"

    def test_items_are_the_same_objects_the_plain_list_returns(self, db):
        db.fetchone.return_value = (1,)
        db.fetchall.return_value = [_detail_row()]
        paged = poles_api.get_poles_page(page=1, page_size=10)["items"][0]
        plain = poles_api._pole_row_to_dict_with_parents(_detail_row())
        assert paged == plain

    def test_summary_mode_uses_the_summary_query_and_mapper(self, db):
        db.fetchone.return_value = (1,)
        db.fetchall.return_value = [_summary_row()]

        result = poles_api.get_poles_page(page=1, page_size=10, summary=True)

        page_sql, _ = _queries(db)[1]
        assert page_sql.startswith(poles_api._POLE_SUMMARY_SQL_TEMPLATE.split("{where_clause}")[0])
        assert "batteryVoltage1" not in result["items"][0]  # summary rows omit it

    def test_details_mode_uses_the_details_query(self, db):
        db.fetchone.return_value = (1,)
        db.fetchall.return_value = [_detail_row()]
        poles_api.get_poles_page(page=1, page_size=10, summary=False)
        page_sql, _ = _queries(db)[1]
        assert page_sql.startswith(poles_api._POLE_DETAILS_SQL_TEMPLATE.split("{where_clause}")[0])


class TestCountThenPage:
    def test_count_query_runs_first_and_touches_no_per_pole_data(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10)
        count_sql, count_params = _queries(db)[0]
        assert count_sql == f"SELECT COUNT(*) {BASE_FROM}"
        assert count_params == []
        # the whole point: counting must not pay for the expensive joins
        for expensive in ("PoleTelemetry", "OUTER APPLY", "PoleTimeZones", "PoleIssues"):
            assert expensive not in count_sql

    def test_page_query_selects_the_pages_pole_ids_in_a_subquery(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=3, page_size=10)
        page_sql, params = _queries(db)[1]
        assert (
            f"WHERE p.Id IN (SELECT sp.Id {BASE_FROM} "
            "ORDER BY sp.PoleNumber, sp.Id OFFSET ? ROWS FETCH NEXT ? ROWS ONLY)"
        ) in page_sql
        assert params == [ROLLUP, 20, 10]  # offset = (3 - 1) * 10

    def test_first_page_offset_is_zero(self, db):
        db.fetchone.return_value = (5,)
        poles_api.get_poles_page(page=1, page_size=10)
        assert _queries(db)[1][1][-2:] == [0, 10]


class TestSkippingTheExpensiveQuery:
    def test_page_past_the_end_returns_empty_items_and_the_real_total(self, db):
        db.fetchone.return_value = (25,)
        result = poles_api.get_poles_page(page=4, page_size=10)  # offset 30 >= 25
        assert len(_queries(db)) == 1  # only the count ran
        assert result == {"total": 25, "page": 4, "pageSize": 10, "items": []}

    def test_no_matches_runs_only_the_count(self, db):
        db.fetchone.return_value = (0,)
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert len(_queries(db)) == 1
        assert result["items"] == [] and result["total"] == 0

    def test_last_partial_page_still_fetches(self, db):
        db.fetchone.return_value = (21,)
        poles_api.get_poles_page(page=3, page_size=10)  # offset 20 < 21
        assert len(_queries(db)) == 2

    def test_offset_exactly_at_total_is_past_the_end(self, db):
        db.fetchone.return_value = (20,)
        poles_api.get_poles_page(page=3, page_size=10)  # offset 20 == total
        assert len(_queries(db)) == 1


class TestPageSizeAndPageBounds:
    def test_default_page_size(self, db):
        db.fetchone.return_value = (500,)
        assert poles_api.get_poles_page(page=1)["pageSize"] == poles_api.DEFAULT_PAGE_SIZE

    @pytest.mark.parametrize("asked,expected", [(5000, poles_api.MAX_PAGE_SIZE), (0, 1), (-3, 1), (25, 25)])
    def test_page_size_is_clamped_and_the_effective_value_returned(self, db, asked, expected):
        db.fetchone.return_value = (500,)
        result = poles_api.get_poles_page(page=1, page_size=asked)
        assert result["pageSize"] == expected
        assert _queries(db)[1][1][-1] == expected

    def test_page_below_one_becomes_one(self, db):
        db.fetchone.return_value = (5,)
        result = poles_api.get_poles_page(page=0, page_size=10)
        assert result["page"] == 1
        assert _queries(db)[1][1][-2] == 0


class TestFilters:
    def test_project_customer_and_active_filters_and_param_order(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=2, page_size=10, project_id="proj1", customer_id="cust1", active=True)
        (count_sql, count_params), (page_sql, page_params) = _queries(db)
        assert "WHERE spr.Id = ? AND sc.Id = ? AND sp.Active = ?" in count_sql
        assert count_params == ["proj1", "cust1", 1]
        assert page_params == [ROLLUP, "proj1", "cust1", 1, 10, 10]

    def test_active_false_binds_zero(self, db):
        db.fetchone.return_value = (0,)
        poles_api.get_poles_page(page=1, page_size=10, active=False)
        assert _queries(db)[0][1] == [0]

    def test_no_filters_means_no_where_clause(self, db):
        db.fetchone.return_value = (0,)
        poles_api.get_poles_page(page=1, page_size=10)
        assert "WHERE" not in _queries(db)[0][0]

    def test_customer_scope(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, scope=CUSTOMER)
        (count_sql, count_params), (page_sql, page_params) = _queries(db)
        assert "WHERE spr.CustomerId = ?" in count_sql
        assert count_params == ["cust-own"]
        assert page_params == [ROLLUP, "cust-own", 0, 10]

    def test_crew_scope(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, scope=CREW)
        count_sql, count_params = _queries(db)[0]
        assert "WHERE spr.StickerNumbers IS NOT NULL" in count_sql
        assert count_params == []

    def test_scope_is_applied_to_the_count_as_well_as_the_page(self, db):
        """If only the page were scoped, `total` would reveal how many poles
        exist outside the caller's scope."""
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, scope=CUSTOMER)
        for sql, _ in _queries(db):
            assert "spr.CustomerId = ?" in sql


class TestFaultsFilter:
    def test_joins_current_vitals_and_uses_the_rollups_fault_definition(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, faults=True)
        count_sql, count_params = _queries(db)[0]
        assert FAULT_JOIN in count_sql
        assert "(sv.IsOnline = 1 OR sv.IsOpenIssueFault = 1) AND sv.IsPoleFault = 1" in count_sql
        assert count_params == [ROLLUP]  # the PeriodType bound in the JOIN

    def test_matches_the_rollups_own_total_faults_expression(self):
        """faults=1's `total` must reconcile with getPoleVitals' TotalFaults."""
        from shared import pole_vitals_api

        rollup_sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "CASE WHEN (IsOnline = 1 OR IsOpenIssueFault = 1) AND IsPoleFault = 1 THEN 1 ELSE 0 END" in rollup_sql
        assert poles_api._FAULT_PREDICATE == "(sv.IsOnline = 1 OR sv.IsOpenIssueFault = 1) AND sv.IsPoleFault = 1"

    def test_param_order_with_every_filter_at_once(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(
            page=2, page_size=10, project_id="proj1", customer_id="cust1",
            active=True, faults=True, scope=CUSTOMER,
        )
        (count_sql, count_params), (page_sql, page_params) = _queries(db)
        # JOIN's period type first (it precedes WHERE in the text), then each condition
        assert count_params == [ROLLUP, "proj1", "cust1", 1, "cust-own"]
        # page query: template's own period type, then the subquery's params, then OFFSET/FETCH
        assert page_params == [ROLLUP, ROLLUP, "proj1", "cust1", 1, "cust-own", 10, 10]

    def test_not_joined_when_faults_is_off(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, faults=False)
        assert "PoleVitals sv" not in _queries(db)[0][0]


FILTER_COMBOS = [
    {},
    {"project_id": "p"},
    {"customer_id": "c"},
    {"active": True},
    {"faults": True},
    {"scope": CUSTOMER},
    {"scope": CREW},
    {"project_id": "p", "customer_id": "c", "active": False, "faults": True, "scope": CUSTOMER},
    {"faults": True, "scope": CREW, "summary": True},
    {"pole_number": "1000"},
    {"pole_number": "10_5", "faults": True},
    {"pole_number": "x", "project_id": "p", "customer_id": "c", "active": True, "faults": True, "scope": CUSTOMER},
]


class TestEveryPlaceholderHasAParameter:
    @pytest.mark.parametrize("kwargs", FILTER_COMBOS)
    def test_placeholder_count_matches_params_in_both_queries(self, db, kwargs):
        db.fetchone.return_value = (100,)
        poles_api.get_poles_page(page=2, page_size=10, **kwargs)
        for sql, params in _queries(db):
            assert sql.count("?") == len(params), sql

    @pytest.mark.parametrize("kwargs", FILTER_COMBOS)
    def test_count_and_page_filter_the_exact_same_poles(self, db, kwargs):
        """The FROM/WHERE that picks the poles must be byte-identical in
        both queries, or total could disagree with the items."""
        db.fetchone.return_value = (100,)
        poles_api.get_poles_page(page=2, page_size=10, **kwargs)
        (count_sql, _), (page_sql, _) = _queries(db)
        picked = count_sql[len("SELECT COUNT(*) "):]
        assert f"SELECT sp.Id {picked} ORDER BY" in page_sql.replace("\n", " ")


BS = chr(92)  # a single backslash, spelled out so there is no escaping ambiguity


class TestLikeContains:
    def test_plain_text_is_wrapped_for_a_contains_match(self):
        assert poles_api._like_contains("12009-1000") == "%12009-1000%"

    def test_like_wildcards_in_the_search_text_are_matched_literally(self):
        assert poles_api._like_contains("10_5") == "%10" + BS + "_5%"
        assert poles_api._like_contains("50%") == "%50" + BS + "%%"
        assert poles_api._like_contains("a[b]") == "%a" + BS + "[b]%"

    def test_the_escape_character_itself_is_escaped_first(self):
        assert poles_api._like_contains("x" + BS + "y") == "%x" + BS + BS + "y%"
        # escaping order matters: "%" must not be double-escaped by a later pass
        assert poles_api._like_contains(BS + "%") == "%" + BS + BS + BS + "%%"


class TestPoleNumberSearch:
    ESCAPE_CLAUSE = "ESCAPE '" + BS + "'"

    def test_adds_a_contains_condition_and_binds_the_pattern(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, pole_number="1000")
        (count_sql, count_params), (page_sql, page_params) = _queries(db)
        assert f"WHERE sp.PoleNumber LIKE ? {self.ESCAPE_CLAUSE}" in count_sql
        assert count_params == ["%1000%"]
        assert page_params == [ROLLUP, "%1000%", 0, 10]

    def test_the_search_text_is_always_a_bound_parameter_never_inlined_into_the_sql(self, db):
        db.fetchone.return_value = (0,)
        evil = "'; DROP TABLE Poles; --"
        poles_api.get_poles_page(page=1, page_size=10, pole_number=evil)
        for sql, params in _queries(db):
            assert "DROP" not in sql
        assert _queries(db)[0][1] == [f"%{evil}%"]

    def test_wildcards_typed_by_the_user_are_escaped_in_the_bound_pattern(self, db):
        db.fetchone.return_value = (0,)
        poles_api.get_poles_page(page=1, page_size=10, pole_number="10_5")
        assert _queries(db)[0][1] == ["%10" + BS + "_5%"]

    @pytest.mark.parametrize("blank", [None, ""])
    def test_no_search_means_no_condition(self, db, blank):
        db.fetchone.return_value = (0,)
        poles_api.get_poles_page(page=1, page_size=10, pole_number=blank)
        assert "PoleNumber" not in _queries(db)[0][0]
        assert _queries(db)[0][1] == []

    def test_param_order_with_every_filter_at_once(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(
            page=2, page_size=10, project_id="proj1", customer_id="cust1", pole_number="10",
            active=True, faults=True, scope=CUSTOMER,
        )
        (count_sql, count_params), (page_sql, page_params) = _queries(db)
        # JOIN's period type, then conditions in order: project, customer, search, active, scope
        assert count_params == [ROLLUP, "proj1", "cust1", "%10%", 1, "cust-own"]
        assert page_params == [ROLLUP, ROLLUP, "proj1", "cust1", "%10%", 1, "cust-own", 10, 10]

    def test_search_is_applied_to_the_count_as_well_as_the_page(self, db):
        """Otherwise `total` would count poles the search excluded."""
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, pole_number="1000")
        for sql, _ in _queries(db):
            assert "sp.PoleNumber LIKE ?" in sql

    def test_search_runs_only_in_the_cheap_id_subquery(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, pole_number="1000")
        count_sql, _ = _queries(db)[0]
        for expensive in ("PoleTelemetry", "OUTER APPLY", "PoleTimeZones"):
            assert expensive not in count_sql


class TestOrderIsPoleNumberAcrossTheWholeList:
    """Paginated results are sorted by PoleNumber globally -- NOT grouped by
    project the way the unpaginated list is."""

    @pytest.mark.parametrize("summary", [False, True])
    def test_page_rows_come_back_ordered_by_pole_number_then_id(self, db, summary):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10, summary=summary)
        page_sql, _ = _queries(db)[1]
        assert page_sql.rstrip().endswith("ORDER BY p.PoleNumber, p.Id")
        assert "ORDER BY proj.Id" not in page_sql  # the project-first order is gone from this path

    def test_the_subquery_that_picks_the_page_uses_the_same_order(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10)
        page_sql, _ = _queries(db)[1]
        assert "ORDER BY sp.PoleNumber, sp.Id OFFSET ? ROWS FETCH NEXT ? ROWS ONLY)" in page_sql
        # same keys in the same order, just different table aliases
        norm = lambda text: re.sub(r"\b(sp|p)\.", "", text)
        assert norm(poles_api._PAGE_SUBQUERY_ORDER_BY) == norm(poles_api._PAGED_ORDER_BY) == "ORDER BY PoleNumber, Id"

    def test_the_order_is_total_so_paging_never_repeats_or_skips_a_pole(self):
        assert poles_api._PAGED_ORDER_BY.endswith(", p.Id")
        assert poles_api._PAGE_SUBQUERY_ORDER_BY.endswith(", sp.Id")

    def test_the_project_is_not_part_of_the_order(self, db):
        db.fetchone.return_value = (50,)
        poles_api.get_poles_page(page=1, page_size=10)
        page_sql, _ = _queries(db)[1]
        assert "spr.Id," not in page_sql.split("OFFSET")[0].rsplit("ORDER BY", 1)[1]

    def test_the_unpaginated_list_keeps_its_project_first_order(self, patch_get_connection_poles_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(project_id="proj1")
        sql, *_ = mock_cursor.execute.call_args.args
        assert sql.rstrip().endswith("ORDER BY proj.Id, p.PoleNumber")

    def test_the_shared_templates_are_untouched_and_match_the_constant_we_swap(self):
        from shared import pole_vitals_api

        for template in (poles_api._POLE_SUMMARY_SQL_TEMPLATE, pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE):
            assert template.rstrip().endswith(poles_api._TEMPLATE_ORDER_BY)
            assert poles_api._TEMPLATE_ORDER_BY == "ORDER BY proj.Id, p.PoleNumber"

    def test_fails_loudly_rather_than_misordering_if_a_template_changes(self, db, monkeypatch):
        db.fetchone.return_value = (50,)
        monkeypatch.setattr(
            poles_api, "_POLE_DETAILS_SQL_TEMPLATE", poles_api._POLE_DETAILS_SQL_TEMPLATE.rstrip() + ", p.Id"
        )
        with pytest.raises(RuntimeError, match="update _TEMPLATE_ORDER_BY"):
            poles_api.get_poles_page(page=1, page_size=10)
        assert len(_queries(db)) == 1  # the page query was never sent with the wrong order
