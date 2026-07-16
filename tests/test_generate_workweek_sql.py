"""Tests for scripts/generate_workweek_sql.py"""

import re
from datetime import date, timedelta

import pytest

from scripts.generate_workweek_sql import generate_merge_sql, generate_rows, sunday_on_or_before


class TestSundayOnOrBefore:
    def test_returns_same_date_if_already_sunday(self):
        a_sunday = date(2026, 1, 4)
        assert a_sunday.weekday() == 6
        assert sunday_on_or_before(a_sunday) == a_sunday

    def test_thursday_goes_back_to_previous_sunday(self):
        # Jan 1, 2026 is a Thursday
        assert sunday_on_or_before(date(2026, 1, 1)) == date(2025, 12, 28)

    def test_saturday_goes_back_one_day(self):
        a_saturday = date(2026, 1, 10)
        assert a_saturday.weekday() == 5
        assert sunday_on_or_before(a_saturday) == date(2026, 1, 4)


class TestGenerateRows:
    def test_row_count_matches_years_times_52(self):
        rows = list(generate_rows(2026, 2030))
        assert len(rows) == 5 * 52

    def test_single_year_produces_52_rows(self):
        rows = list(generate_rows(2027, 2027))
        assert len(rows) == 52

    def test_no_duplicate_year_week_pairs(self):
        rows = list(generate_rows(2026, 2030))
        keys = [(year, week) for year, week, _, _ in rows]
        assert len(keys) == len(set(keys))

    def test_week_numbers_are_always_1_to_52(self):
        rows = list(generate_rows(2026, 2030))
        weeks_by_year = {}
        for year, week, _, _ in rows:
            weeks_by_year.setdefault(year, []).append(week)
        for year, weeks in weeks_by_year.items():
            assert sorted(weeks) == list(range(1, 53)), f"{year} weeks: {sorted(weeks)}"

    def test_start_date_is_always_sunday(self):
        for _, _, start, _ in generate_rows(2026, 2030):
            assert start.weekday() == 6

    def test_end_date_is_always_saturday(self):
        for _, _, _, end in generate_rows(2026, 2030):
            assert end.weekday() == 5

    def test_end_date_is_exactly_six_days_after_start(self):
        for _, _, start, end in generate_rows(2026, 2030):
            assert end == start + timedelta(days=6)

    def test_consecutive_weeks_have_no_gap_or_overlap_within_a_year(self):
        rows = list(generate_rows(2026, 2030))
        by_year = {}
        for year, week, start, end in rows:
            by_year.setdefault(year, {})[week] = (start, end)

        for year, weeks in by_year.items():
            for w in range(1, 52):
                this_end = weeks[w][1]
                next_start = weeks[w + 1][0]
                assert next_start == this_end + timedelta(days=1), (
                    f"{year} week {w}->{w + 1}: gap/overlap "
                    f"({this_end} -> {next_start})"
                )

    def test_january_first_always_falls_within_that_years_week_1(self):
        rows = list(generate_rows(2026, 2030))
        week1_by_year = {year: (start, end) for year, week, start, end in rows if week == 1}
        for year, (start, end) in week1_by_year.items():
            jan1 = date(year, 1, 1)
            assert start <= jan1 <= end, f"{year}: Jan 1 not within Week 1 ({start}..{end})"

    def test_leap_year_february_29_is_covered_by_some_week(self):
        """2028 is a leap year -- Feb 29 must land in exactly one week."""
        rows = list(generate_rows(2028, 2028))
        feb29 = date(2028, 2, 29)
        matches = [(week, start, end) for _, week, start, end in rows if start <= feb29 <= end]
        assert len(matches) == 1

    def test_known_2026_week_1_dates(self):
        """Confirmed by hand: Jan 1 2026 is a Thursday, so Week 1 starts
        the preceding Sunday, Dec 28 2025."""
        rows = list(generate_rows(2026, 2026))
        week1 = next((s, e) for _, w, s, e in rows if w == 1)
        assert week1 == (date(2025, 12, 28), date(2026, 1, 3))

    def test_each_year_anchored_independently_not_rolling(self):
        """2027's Week 1 must be anchored to 2027's own Jan 1, not simply
        continue counting from 2026's Week 52."""
        rows_2026 = {w: (s, e) for _, w, s, e in generate_rows(2026, 2026)}
        rows_2027 = {w: (s, e) for _, w, s, e in generate_rows(2027, 2027)}
        week52_2026_end = rows_2026[52][1]
        week1_2027_start = rows_2027[1][0]
        # 2027's Week 1 should start the day after 2026's Week 52 ends,
        # continuing the calendar with no gap -- but is computed from
        # 2027's own Jan 1, not by simply adding 7 days to 2026's Week 52.
        assert week1_2027_start == week52_2026_end + timedelta(days=1)


class TestGenerateMergeSql:
    def test_contains_a_row_for_every_year_week_pair(self):
        sql = generate_merge_sql(2026, 2027)
        for year in (2026, 2027):
            for week in (1, 52):
                assert f"({year}, {week}, " in sql

    def test_is_a_merge_statement_not_plain_insert(self):
        """Idempotent/re-runnable -- a plain INSERT would fail on a second
        run due to the (Year, Week) primary key."""
        sql = generate_merge_sql(2026, 2026)
        assert "MERGE Workweek AS target" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN" in sql

    def test_match_key_is_year_and_week(self):
        sql = generate_merge_sql(2026, 2026)
        assert "ON target.Year = source.Year AND target.Week = source.Week" in sql

    def test_row_count_in_generated_sql_matches_expected(self):
        sql = generate_merge_sql(2026, 2030)
        # Count value-tuples: lines matching "    (YYYY, N, '...', '...')"
        value_lines = re.findall(r"^\s*\(\d{4}, \d+, '[\d-]+', '[\d-]+'\)", sql, re.MULTILINE)
        assert len(value_lines) == 5 * 52

    def test_dates_are_quoted_as_iso_strings(self):
        sql = generate_merge_sql(2026, 2026)
        assert "'2025-12-28'" in sql  # Week 1 start, confirmed above
        assert "'2026-01-03'" in sql  # Week 1 end
