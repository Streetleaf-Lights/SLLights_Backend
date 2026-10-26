"""
Tests for poles_api.get_poles_page() -- the paginated getPoles query.

Flow under test:
  1. a cheap query returns (Id, PoleNumber) for every pole matching the filters;
  2. Python sorts them NATURALLY ("-999" before "-1000") and slices the page;
  3. only then does the expensive per-pole query run, for just that page's Ids.

No SQL Server here, so these pin the SQL it SENDS (shape, parameter order,
that nothing expensive runs for the whole list) and the Python ordering/paging.
"""

import random

import pytest

from shared import poles_api
from shared.data_scope import DataScope
from tests.test_poles_api import TestPoleRowToDictWithParents, _summary_row

ROLLUP = poles_api._ROLLUP_PERIOD_TYPE
CUSTOMER = DataScope(customer_id="cust-own")
CREW = DataScope(scheduled_projects_only=True)
BS = chr(92)  # a single backslash, spelled out so there is no escaping ambiguity

BASE_FROM = (
    "FROM Poles sp JOIN Projects spr ON sp.ProjectId = spr.Id "
    "JOIN Customers sc ON spr.CustomerId = sc.Id"
)
FAULT_JOIN = (
    " JOIN PoleVitals sv ON COALESCE(sp.VendorPoleId, sp.ProvisionedPoleId) = sv.PoleId"
    " AND sv.PeriodType = ?"
)
CANDIDATES = f"SELECT sp.Id, sp.PoleNumber {BASE_FROM}"


def _detail_row(pole_id="pole1", pole_number="PN-1"):
    row = list(TestPoleRowToDictWithParents()._detail_row(None))
    row[1], row[2] = pole_id, pole_number  # PoleId, PoleNumber
    return tuple(row)


@pytest.fixture
def db(patch_get_connection_poles_api, mock_cursor):
    mock_cursor.fetchall.side_effect = None
    mock_cursor.fetchall.return_value = []
    return mock_cursor


def _feed(db, candidates, rows=None):
    """1st fetchall() = the cheap (Id, PoleNumber) candidates; 2nd = the per-pole rows."""
    db.fetchall.side_effect = [list(candidates), list(rows if rows is not None else [])]


def _queries(cursor):
    """[(sql, params)] for each execute() call, in order."""
    return [(c.args[0], list(c.args[1:])) for c in cursor.execute.call_args_list]


def _poles(*numbers):
    """Candidate rows (id, number) with ids derived from the number."""
    return [(f"id-{n}", n) for n in numbers]


# --------------------------------------------------------------------------
# Natural sort key
# --------------------------------------------------------------------------
def _natural(numbers):
    return sorted(numbers, key=lambda n: poles_api._natural_key(n, n))


class TestNaturalKey:
    def test_999_sorts_before_1000_which_a_plain_string_sort_gets_wrong(self):
        numbers = ["12009-1000", "12009-999", "12009-100"]
        assert sorted(numbers) == ["12009-100", "12009-1000", "12009-999"]  # what text order gives
        assert _natural(numbers) == ["12009-100", "12009-999", "12009-1000"]

    def test_every_digit_run_compares_as_a_number(self):
        assert _natural(["A-2-10", "A-10-1", "A-2-9", "A-1-1"]) == ["A-1-1", "A-2-9", "A-2-10", "A-10-1"]

    def test_text_between_numbers_is_compared_case_insensitively(self):
        assert _natural(["osc-5", "OSC-10", "Osc-1"]) == ["Osc-1", "osc-5", "OSC-10"]

    def test_letter_suffixes_after_the_number(self):
        assert _natural(["12009-1000-B", "12009-1000-A", "12009-999-Z"]) == [
            "12009-999-Z", "12009-1000-A", "12009-1000-B",
        ]

    def test_very_long_digit_runs_are_exact(self):
        assert _natural(["12057-2689033877", "12057-999", "12057-2689033878"]) == [
            "12057-999", "12057-2689033877", "12057-2689033878",
        ]

    def test_a_missing_pole_number_sorts_first(self):
        keys = sorted(
            [("b", "A-1"), ("a", None), ("c", ""), ("d", "A-0")],
            key=lambda kv: poles_api._natural_key(kv[1], kv[0]),
        )
        assert [k[0] for k in keys] == ["a", "c", "d", "b"]

    def test_numerically_equal_values_still_have_one_deterministic_order(self):
        # "A-07" and "A-7" are the same number; the exact text then decides
        assert _natural(["A-7", "A-07", "A-007"]) == _natural(["A-007", "A-7", "A-07"]) == ["A-007", "A-07", "A-7"]

    def test_identical_numbers_fall_back_to_the_pole_id(self):
        rows = [("id-b", "A-1"), ("id-a", "A-1")]
        ordered = sorted(rows, key=lambda kv: poles_api._natural_key(kv[1], kv[0]))
        assert [r[0] for r in ordered] == ["id-a", "id-b"]

    def test_never_compares_text_with_a_number(self):
        weird = ["123", "abc", "1a2", "a1b", "-", "9", "a", "1", "-1", "1-", "00", "x9y9"]
        assert len(_natural(weird)) == len(weird)  # would raise TypeError on a str/int comparison

    def test_the_result_does_not_depend_on_the_input_order(self):
        base = ["P-%d%s" % (n, s) for n in (1, 2, 9, 10, 11, 99, 100, 1000) for s in ("", "-A", "-b")]
        expected = _natural(base)
        rng = random.Random(7)
        for _ in range(25):
            shuffled = base[:]
            rng.shuffle(shuffled)
            assert _natural(shuffled) == expected


# --------------------------------------------------------------------------
# Shape of the result
# --------------------------------------------------------------------------
class TestShape:
    def test_returns_total_page_pagesize_and_items(self, db):
        _feed(db, [("pole1", "PN-1")], [_detail_row()])
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert set(result) == {"total", "page", "pageSize", "items"}
        assert (result["total"], result["page"], result["pageSize"]) == (1, 1, 10)
        assert result["items"][0]["id"] == "pole1"
        assert result["items"][0]["customerId"] == "cust1" and result["items"][0]["controllerId"] == "CTRL-1"

    def test_a_page_past_the_end_still_reports_the_real_total(self, db):
        _feed(db, [("pole1", "PN-1")], [])
        result = poles_api.get_poles_page(page=2, page_size=10)
        assert (result["total"], result["page"], result["items"]) == (1, 2, [])

    def test_items_are_the_same_objects_the_plain_list_returns(self, db):
        _feed(db, [("pole1", "PN-1")], [_detail_row()])
        paged = poles_api.get_poles_page(page=1, page_size=10)["items"][0]
        assert paged == poles_api._pole_row_to_dict_with_parents(_detail_row())

    def test_summary_mode_uses_the_summary_query_and_mapper(self, db):
        _feed(db, [("pole1", "PN-1")], [_summary_row()])
        result = poles_api.get_poles_page(page=1, page_size=10, summary=True)
        heavy_sql, _ = _queries(db)[1]
        assert heavy_sql.startswith(poles_api._POLE_SUMMARY_SQL_TEMPLATE.split("{where_clause}")[0])
        assert "batteryVoltage1" not in result["items"][0]

    def test_details_mode_uses_the_details_query(self, db):
        _feed(db, [("pole1", "PN-1")], [_detail_row()])
        poles_api.get_poles_page(page=1, page_size=10, summary=False)
        heavy_sql, _ = _queries(db)[1]
        assert heavy_sql.startswith(poles_api._POLE_DETAILS_SQL_TEMPLATE.split("{where_clause}")[0])


# --------------------------------------------------------------------------
# The cheap candidate query
# --------------------------------------------------------------------------
class TestCandidateQuery:
    def test_it_runs_first_and_selects_only_id_and_pole_number(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10)
        sql, params = _queries(db)[0]
        assert sql == CANDIDATES
        assert params == []

    def test_it_touches_none_of_the_per_pole_machinery_and_does_no_ordering_or_paging(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, faults=True, pole_number="1")
        sql, _ = _queries(db)[0]
        for absent in ("PoleTelemetry", "OUTER APPLY", "PoleTimeZones", "PoleIssues", "ORDER BY", "OFFSET", "COUNT("):
            assert absent not in sql

    def test_there_is_no_separate_count_query(self, db):
        _feed(db, _poles("A-1", "A-2"), [])
        poles_api.get_poles_page(page=1, page_size=10)
        assert not any("COUNT(" in sql for sql, _ in _queries(db))


# --------------------------------------------------------------------------
# Paging through the naturally-sorted list
# --------------------------------------------------------------------------
class TestPaging:
    NUMBERS = [f"P-{n}" for n in range(1, 26)]  # P-1 .. P-25; plain text order would put P-10 before P-2

    def _shuffled_candidates(self):
        rows = _poles(*self.NUMBERS)
        random.Random(3).shuffle(rows)
        return rows

    def _page_ids(self, db):
        return _queries(db)[1][1][1:]  # params after the template's own PoleVitals period type

    def test_page_one_is_the_first_ten_in_natural_order(self, db):
        _feed(db, self._shuffled_candidates(), [])
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert result["total"] == 25
        assert self._page_ids(db) == [f"id-P-{n}" for n in range(1, 11)]

    def test_later_pages_continue_where_the_previous_one_stopped(self, db):
        seen = []
        for page in (1, 2, 3):
            db.execute.reset_mock()
            _feed(db, self._shuffled_candidates(), [])
            poles_api.get_poles_page(page=page, page_size=10)
            seen += self._page_ids(db)
        assert seen == [f"id-P-{n}" for n in range(1, 26)]  # all 25, once each, in order

    def test_the_page_query_selects_by_id_and_binds_every_id(self, db):
        _feed(db, self._shuffled_candidates(), [])
        poles_api.get_poles_page(page=2, page_size=10)
        heavy_sql, params = _queries(db)[1]
        assert "WHERE p.Id IN (" + ", ".join("?" for _ in range(10)) + ")" in heavy_sql
        assert params[0] == ROLLUP and len(params) == 11
        assert heavy_sql.count("?") == len(params)

    def test_items_come_back_in_page_order_even_if_sql_returns_them_in_another(self, db):
        candidates = _poles("A-3", "A-1", "A-2")
        scrambled_sql_rows = [_detail_row("id-A-2", "A-2"), _detail_row("id-A-3", "A-3"), _detail_row("id-A-1", "A-1")]
        _feed(db, candidates, scrambled_sql_rows)
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert [i["id"] for i in result["items"]] == ["id-A-1", "id-A-2", "id-A-3"]

    def test_the_requested_scenario_999_before_1000(self, db):
        candidates = _poles("12009-1000", "12009-999", "12009-100")
        rows = [_detail_row(f"id-{n}", n) for n in ("12009-1000", "12009-999", "12009-100")]
        _feed(db, candidates, rows)
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert [i["poleNumber"] for i in result["items"]] == ["12009-100", "12009-999", "12009-1000"]

    def test_only_the_poles_on_the_page_reach_the_expensive_query(self, db):
        _feed(db, self._shuffled_candidates(), [])
        poles_api.get_poles_page(page=1, page_size=3)
        assert self._page_ids(db) == ["id-P-1", "id-P-2", "id-P-3"]

    def test_a_join_that_repeats_a_pole_does_not_inflate_the_total_or_the_page(self, db):
        _feed(db, [("id-1", "A-1"), ("id-1", "A-1"), ("id-2", "A-2")], [])
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert result["total"] == 2
        assert self._page_ids(db) == ["id-1", "id-2"]


class TestSkippingTheExpensiveQuery:
    def test_page_past_the_end_returns_empty_items_and_the_real_total(self, db):
        _feed(db, _poles(*[f"P-{n}" for n in range(1, 26)]))
        result = poles_api.get_poles_page(page=4, page_size=10)  # offset 30 >= 25
        assert len(_queries(db)) == 1  # only the cheap query ran
        assert result == {"total": 25, "page": 4, "pageSize": 10, "items": []}

    def test_no_matches_runs_only_the_cheap_query(self, db):
        _feed(db, [])
        result = poles_api.get_poles_page(page=1, page_size=10)
        assert len(_queries(db)) == 1
        assert result["items"] == [] and result["total"] == 0

    def test_last_partial_page_fetches_only_the_remaining_poles(self, db):
        _feed(db, _poles(*[f"P-{n}" for n in range(1, 22)]), [])  # 21 poles
        poles_api.get_poles_page(page=3, page_size=10)
        assert len(_queries(db)) == 2
        assert _queries(db)[1][1][1:] == ["id-P-21"]

    def test_offset_exactly_at_the_total_is_past_the_end(self, db):
        _feed(db, _poles(*[f"P-{n}" for n in range(1, 21)]))  # 20 poles
        poles_api.get_poles_page(page=3, page_size=10)
        assert len(_queries(db)) == 1


class TestPageSizeAndPageBounds:
    def test_default_page_size(self, db):
        _feed(db, _poles(*[f"P-{n}" for n in range(1, 101)]), [])
        result = poles_api.get_poles_page(page=1)
        assert result["pageSize"] == poles_api.DEFAULT_PAGE_SIZE
        assert len(_queries(db)[1][1][1:]) == poles_api.DEFAULT_PAGE_SIZE

    @pytest.mark.parametrize("asked,expected", [(5000, poles_api.MAX_PAGE_SIZE), (0, 1), (-3, 1), (25, 25)])
    def test_page_size_is_clamped_and_the_effective_value_returned(self, db, asked, expected):
        _feed(db, _poles(*[f"P-{n}" for n in range(1, 31)]), [])
        result = poles_api.get_poles_page(page=1, page_size=asked)
        assert result["pageSize"] == expected
        assert len(_queries(db)[1][1][1:]) == min(expected, 30)

    def test_page_below_one_becomes_one(self, db):
        _feed(db, _poles("A-1", "A-2"), [])
        result = poles_api.get_poles_page(page=0, page_size=10)
        assert result["page"] == 1
        assert _queries(db)[1][1][1:] == ["id-A-1", "id-A-2"]

    def test_the_largest_page_stays_under_sql_servers_parameter_limit(self):
        # one parameter per id in the IN list, plus the template's period type
        assert poles_api.MAX_PAGE_SIZE + 1 < 2100


# --------------------------------------------------------------------------
# Filters, scope, faults, search -- all applied in the ONE cheap query
# --------------------------------------------------------------------------
class TestFilters:
    def test_project_customer_and_active_filters_and_param_order(self, db):
        _feed(db, _poles("A-1"), [])
        poles_api.get_poles_page(page=1, page_size=10, project_id="proj1", customer_id="cust1", active=True)
        (sql, params), (_heavy_sql, heavy_params) = _queries(db)
        assert "WHERE spr.Id = ? AND sc.Id = ? AND sp.Active = ?" in sql
        assert params == ["proj1", "cust1", 1]
        assert heavy_params == [ROLLUP, "id-A-1"]  # filters are NOT repeated: the page is chosen by Id

    def test_active_false_binds_zero(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, active=False)
        assert _queries(db)[0][1] == [0]

    def test_no_filters_means_no_where_clause(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10)
        assert "WHERE" not in _queries(db)[0][0]

    def test_customer_scope(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, scope=CUSTOMER)
        sql, params = _queries(db)[0]
        assert "WHERE spr.CustomerId = ?" in sql and params == ["cust-own"]

    def test_crew_scope(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, scope=CREW)
        sql, params = _queries(db)[0]
        assert "WHERE spr.StickerNumbers IS NOT NULL" in sql and params == []

    def test_scope_bounds_both_the_items_and_the_total(self, db):
        """Total and page both come from the one scoped candidate query, so
        `total` can never reveal poles outside the caller's scope."""
        _feed(db, _poles("A-1"), [])
        result = poles_api.get_poles_page(page=1, page_size=10, scope=CUSTOMER)
        assert result["total"] == 1
        scoped = [sql for sql, _ in _queries(db) if "spr.CustomerId = ?" in sql]
        assert len(scoped) == 1 and scoped[0].startswith("SELECT sp.Id, sp.PoleNumber")


class TestFaultsFilter:
    def test_joins_current_vitals_and_uses_the_rollups_fault_definition(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, faults=True)
        sql, params = _queries(db)[0]
        assert FAULT_JOIN in sql
        assert "(sv.IsOnline = 1 OR sv.IsOpenIssueFault = 1) AND sv.IsPoleFault = 1" in sql
        assert params == [ROLLUP]  # the PeriodType bound in the JOIN

    def test_matches_the_rollups_own_total_faults_expression(self):
        """faults=1's `total` must reconcile with getPoleVitals' TotalFaults."""
        from shared import pole_vitals_api

        rollup_sql = pole_vitals_api._FETCH_SQL_TEMPLATE
        assert "CASE WHEN (IsOnline = 1 OR IsOpenIssueFault = 1) AND IsPoleFault = 1 THEN 1 ELSE 0 END" in rollup_sql
        assert poles_api._FAULT_PREDICATE == "(sv.IsOnline = 1 OR sv.IsOpenIssueFault = 1) AND sv.IsPoleFault = 1"

    def test_param_order_with_every_filter_at_once(self, db):
        _feed(db, _poles("A-1"), [])
        poles_api.get_poles_page(
            page=1, page_size=10, project_id="proj1", customer_id="cust1", pole_number="10",
            active=True, faults=True, scope=CUSTOMER,
        )
        (_sql, params), (_heavy_sql, heavy_params) = _queries(db)
        # JOIN's period type first (it precedes WHERE in the text), then project, customer, search, active, scope
        assert params == [ROLLUP, "proj1", "cust1", "%10%", 1, "cust-own"]
        # the page query is by Id only -- none of those filters are repeated
        assert heavy_params == [ROLLUP, "id-A-1"]

    def test_not_joined_when_faults_is_off(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, faults=False)
        assert "PoleVitals sv" not in _queries(db)[0][0]


class TestLikeContains:
    def test_plain_text_is_wrapped_for_a_contains_match(self):
        assert poles_api._like_contains("12009-1000") == "%12009-1000%"

    def test_like_wildcards_in_the_search_text_are_matched_literally(self):
        assert poles_api._like_contains("10_5") == "%10" + BS + "_5%"
        assert poles_api._like_contains("50%") == "%50" + BS + "%%"
        assert poles_api._like_contains("a[b]") == "%a" + BS + "[b]%"

    def test_the_escape_character_itself_is_escaped_first(self):
        assert poles_api._like_contains("x" + BS + "y") == "%x" + BS + BS + "y%"
        assert poles_api._like_contains(BS + "%") == "%" + BS + BS + BS + "%%"


class TestPoleNumberSearch:
    ESCAPE_CLAUSE = "ESCAPE '" + BS + "'"

    def test_adds_a_contains_condition_and_binds_the_pattern(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, pole_number="1000")
        sql, params = _queries(db)[0]
        assert f"WHERE sp.PoleNumber LIKE ? {self.ESCAPE_CLAUSE}" in sql
        assert params == ["%1000%"]

    def test_the_search_text_is_always_a_bound_parameter_never_inlined_into_the_sql(self, db):
        _feed(db, [])
        evil = "'; DROP TABLE Poles; --"
        poles_api.get_poles_page(page=1, page_size=10, pole_number=evil)
        sql, params = _queries(db)[0]
        assert "DROP" not in sql
        assert params == [f"%{evil}%"]

    def test_wildcards_typed_by_the_user_are_escaped_in_the_bound_pattern(self, db):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, pole_number="10_5")
        assert _queries(db)[0][1] == ["%10" + BS + "_5%"]

    @pytest.mark.parametrize("blank", [None, ""])
    def test_no_search_means_no_condition(self, db, blank):
        _feed(db, [])
        poles_api.get_poles_page(page=1, page_size=10, pole_number=blank)
        assert "PoleNumber LIKE" not in _queries(db)[0][0]
        assert _queries(db)[0][1] == []

    def test_search_results_are_in_natural_order_too(self, db):
        _feed(db, _poles("X-1000", "X-999", "X-100"), [_detail_row(f"id-X-{n}", f"X-{n}") for n in (1000, 999, 100)])
        result = poles_api.get_poles_page(page=1, page_size=10, pole_number="X-")
        assert [i["poleNumber"] for i in result["items"]] == ["X-100", "X-999", "X-1000"]


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
        _feed(db, _poles("A-1", "A-2", "A-3"), [])
        poles_api.get_poles_page(page=1, page_size=10, **kwargs)
        queries = _queries(db)
        assert len(queries) == 2
        for sql, params in queries:
            assert sql.count("?") == len(params), sql


class TestUnpaginatedListIsUnchanged:
    def test_the_plain_list_keeps_its_project_first_order(self, patch_get_connection_poles_api, mock_cursor):
        mock_cursor.fetchall.return_value = []
        poles_api.get_poles(project_id="proj1")
        sql, *_ = mock_cursor.execute.call_args.args
        assert sql.rstrip().endswith("ORDER BY proj.Id, p.PoleNumber")

    def test_the_shared_templates_are_untouched(self):
        from shared import pole_vitals_api

        for template in (poles_api._POLE_SUMMARY_SQL_TEMPLATE, pole_vitals_api._POLE_DETAILS_SQL_TEMPLATE):
            assert template.rstrip().endswith("ORDER BY proj.Id, p.PoleNumber")
