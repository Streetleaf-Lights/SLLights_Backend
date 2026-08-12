"""
Tests for shared/pole_timezones_loader.py -- resolving timezones from
each pole's own county (Poles.CountyFips -> CountyTimeZones.FIPS)
instead of Poles.Lat/Poles.Long + a per-pole timezonefinder computation.
"""

from unittest.mock import MagicMock, patch

import pytest

from shared import pole_timezones_loader as m


class TestSourceConstants:
    def test_execution_source_is_still_leadsun(self):
        """SP_Execution.Source tracks which pipeline this loader's run
        belongs to -- unchanged, since this loader still runs as part of
        the Leadsun pipeline's load order regardless of where the
        coordinates it resolves come from."""
        assert m.EXECUTION_SOURCE == "Leadsun"

    def test_coordinate_source_is_now_county_time_zones(self):
        """PoleTimeZones.Source tracks where each row's coordinates
        actually came from -- now CountyTimeZones (via Poles.CountyFips),
        not Airtable's Lat/Long directly, and not Leadsun's raw device
        GPS either."""
        assert m.COORDINATE_SOURCE == "CountyTimeZones"

    def test_the_two_constants_are_distinct(self):
        """The whole point of splitting these apart -- they must not
        silently collapse back into a single shared value."""
        assert m.EXECUTION_SOURCE != m.COORDINATE_SOURCE


class TestResolveFromCountySql:
    def test_joins_poles_to_county_time_zones_via_county_fips(self):
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert "JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS" in sql

    def test_join_is_inner_not_left(self):
        """A pole whose CountyFips doesn't match any row in
        CountyTimeZones (missing, typo, unusual code) can't be resolved
        via this path at all -- there's no Lat/Long fallback left to try,
        unlike the design this replaced. LEFT JOIN would silently
        produce NULL coordinates/timezone for such a pole instead of
        correctly excluding it."""
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert "LEFT JOIN CountyTimeZones" not in sql

    def test_still_left_joins_pole_time_zones_to_find_unresolved_ones(self):
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert "LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId" in sql
        assert "ptz.LocationId IS NULL" in sql

    def test_filters_out_poles_with_no_location_id_yet(self):
        """A pole can exist in Poles before it's linked to a real Leadsun
        device -- without this filter, a NULL LocationId would satisfy
        the LEFT JOIN's "not yet resolved" condition and attempt to
        resolve/insert a timezone row for a pole with no real location."""
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert "p.LocationId IS NOT NULL" in sql

    def test_no_group_by_or_aggregation_needed(self):
        """Both Poles and CountyTimeZones are reference tables (one row
        per pole, one row per county respectively) -- no time-series
        data to aggregate across, unlike the PoleTelemetry-based design
        from even further back in this loader's history."""
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert "GROUP BY" not in sql
        assert "MIN(" not in sql

    def test_is_a_single_merge_not_a_select_for_python_processing(self):
        """The entire resolution is one set-based SQL statement now --
        unlike the timezonefinder-based design this replaced, there's no
        per-row Python computation left to do, since CountyTimeZones
        already has every county's timezone pre-computed."""
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert "MERGE PoleTimeZones AS target" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN" in sql

    def test_four_placeholders_for_source_and_sp_exec_id_twice(self):
        """Source/SP_ExecId are bound as plain ? placeholders (not
        selected from the USING subquery) since they're the SAME two
        values for every row in this run, needed once for the UPDATE
        branch and once for the INSERT branch."""
        sql = m._RESOLVE_FROM_COUNTY_SQL
        assert sql.count("?") == 4


class TestCountUnresolvableSql:
    def test_left_joins_both_pole_time_zones_and_county_time_zones(self):
        """Unlike the main resolution MERGE (INNER JOIN CountyTimeZones,
        since a non-matching pole is simply excluded from what it can
        resolve), this diagnostic query needs LEFT JOINs on both to
        specifically COUNT the poles the main query silently excludes,
        not exclude them itself."""
        sql = m._COUNT_UNRESOLVABLE_SQL
        assert "LEFT JOIN PoleTimeZones ptz ON p.LocationId = ptz.LocationId" in sql
        assert "LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS" in sql

    def test_counts_only_poles_not_yet_resolved_and_unresolvable(self):
        sql = m._COUNT_UNRESOLVABLE_SQL
        assert "WHERE ptz.LocationId IS NULL" in sql
        assert "AND p.LocationId IS NOT NULL" in sql
        assert "AND ctz.FIPS IS NULL" in sql

    def test_is_a_count_not_a_write(self):
        sql = m._COUNT_UNRESOLVABLE_SQL
        assert "SELECT COUNT(*)" in sql
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        assert "MERGE" not in sql


class TestResolveFromCountyBackfillSql:
    """
    Coverage for the fix to a real production gap: this project's poles
    were already resolved via the OLD Lat/Long-based approach for months
    before Poles.CountyFips existed, so the normal, non-backfill MERGE's
    own "not already in PoleTimeZones" restriction meant adding/
    correcting CountyFips values did nothing for any pole that already
    had a PoleTimeZones row -- in practice, nearly all of them. This
    variant drops that restriction entirely to re-resolve and overwrite
    every pole with a resolvable CountyFips, once.
    """

    def test_has_no_not_already_resolved_restriction(self):
        """The one, deliberate difference from _RESOLVE_FROM_COUNTY_SQL:
        no LEFT JOIN PoleTimeZones / "ptz.LocationId IS NULL" check at
        all -- every pole with a resolvable CountyFips is a candidate,
        regardless of whether it already has a PoleTimeZones row."""
        sql = m._RESOLVE_FROM_COUNTY_BACKFILL_SQL
        assert "PoleTimeZones ptz" not in sql
        assert "ptz.LocationId IS NULL" not in sql

    def test_still_joins_county_time_zones_via_county_fips(self):
        sql = m._RESOLVE_FROM_COUNTY_BACKFILL_SQL
        assert "JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS" in sql

    def test_still_filters_out_poles_with_no_location_id(self):
        sql = m._RESOLVE_FROM_COUNTY_BACKFILL_SQL
        assert "p.LocationId IS NOT NULL" in sql

    def test_is_otherwise_the_same_shape_as_the_normal_merge(self):
        """Same MERGE structure, same match key, same UPDATE/INSERT
        columns -- the backfill variant is a scope change, not a
        different kind of statement."""
        sql = m._RESOLVE_FROM_COUNTY_BACKFILL_SQL
        assert "MERGE PoleTimeZones AS target" in sql
        assert "ON target.LocationId = source.LocationId" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN" in sql
        assert sql.count("?") == 4


class TestCountUnresolvableBackfillSql:
    def test_has_no_not_already_resolved_restriction(self):
        sql = m._COUNT_UNRESOLVABLE_BACKFILL_SQL
        assert "PoleTimeZones" not in sql

    def test_still_counts_poles_with_no_resolvable_county_fips(self):
        sql = m._COUNT_UNRESOLVABLE_BACKFILL_SQL
        assert "LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS" in sql
        assert "AND ctz.FIPS IS NULL" in sql
        assert "WHERE p.LocationId IS NOT NULL" in sql


class TestMergeDeduplicatesByLocationId:
    """
    Regression coverage for a real production bug: Poles is keyed by its
    own Id (the Airtable record id), not LocationId -- nothing prevents
    two different Poles rows from sharing one LocationId (a genuine
    Airtable data quality issue). Without deduplicating first, the
    MERGE's USING subquery could produce two source rows for the same
    LocationId, which SQL Server rejects outright with "The MERGE
    statement attempted to UPDATE or DELETE the same row more than once"
    (error 8672) -- confirmed happening in practice, specifically via
    the backfill variant (which, unlike the normal MERGE, actually
    revisits every pole rather than only ones no prior run has already
    masked this for).
    """

    @pytest.mark.parametrize(
        "sql", [m._RESOLVE_FROM_COUNTY_SQL, m._RESOLVE_FROM_COUNTY_BACKFILL_SQL], ids=["normal", "backfill"]
    )
    def test_has_row_number_partitioned_by_location_id(self, sql):
        assert "ROW_NUMBER() OVER (PARTITION BY p.LocationId ORDER BY p.Id)" in sql

    @pytest.mark.parametrize(
        "sql", [m._RESOLVE_FROM_COUNTY_SQL, m._RESOLVE_FROM_COUNTY_BACKFILL_SQL], ids=["normal", "backfill"]
    )
    def test_filters_to_exactly_one_row_per_location_id(self, sql):
        assert "WHERE rn = 1" in sql

    @pytest.mark.parametrize(
        "sql", [m._RESOLVE_FROM_COUNTY_SQL, m._RESOLVE_FROM_COUNTY_BACKFILL_SQL], ids=["normal", "backfill"]
    )
    def test_deduplication_happens_before_the_merge_matches_target(self, sql):
        """The ROW_NUMBER()/"WHERE rn = 1" wrapper must be INSIDE the
        USING subquery (so the MERGE only ever sees one row per
        LocationId), not applied after the fact."""
        using_clause = sql.split("USING (")[1].split(") AS source")[0]
        assert "ROW_NUMBER()" in using_clause
        assert "WHERE rn = 1" in using_clause


class TestCountDuplicateLocationIdsSql:
    def test_groups_by_location_id_and_filters_to_more_than_one(self):
        sql = m._COUNT_DUPLICATE_LOCATION_IDS_SQL
        assert "GROUP BY LocationId" in sql
        assert "HAVING COUNT(*) > 1" in sql

    def test_excludes_null_location_ids(self):
        sql = m._COUNT_DUPLICATE_LOCATION_IDS_SQL
        assert "WHERE LocationId IS NOT NULL" in sql

    def test_is_a_count_not_a_write(self):
        sql = m._COUNT_DUPLICATE_LOCATION_IDS_SQL
        assert "SELECT COUNT(*)" in sql
        assert "INSERT" not in sql
        assert "UPDATE" not in sql
        assert "MERGE" not in sql


class TestLoadPoleTimezonesDuplicateLocationIdWarning:
    def test_logs_a_warning_when_duplicates_exist(self, caplog):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (3,)]  # 3 duplicate LocationIds
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with caplog.at_level("WARNING"):
                m.load_pole_timezones()

        warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert any("3 LocationId(s)" in w and "claimed by more than one Poles row" in w for w in warnings)

    def test_does_not_log_when_no_duplicates(self, caplog):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with caplog.at_level("WARNING"):
                m.load_pole_timezones()

        warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert not any("claimed by more than one Poles row" in w for w in warnings)

    def test_duplicate_count_is_diagnostic_only_not_counted_as_an_error(self):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (10,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        errors = final_update_args[3]
        assert errors == 0



class TestLoadPoleTimezonesBackfillParameter:
    def test_default_call_uses_the_normal_non_backfill_sql(self, mocker):
        """Regression guard: the default (backfill=False) behavior must
        stay exactly what it was before this parameter existed."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        merge_sql = mock_cursor.execute.call_args_list[1].args[0]
        count_sql = mock_cursor.execute.call_args_list[2].args[0]
        assert merge_sql == m._RESOLVE_FROM_COUNTY_SQL
        assert count_sql == m._COUNT_UNRESOLVABLE_SQL

    def test_backfill_true_uses_the_backfill_sql_variants(self, mocker):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones(backfill=True)

        merge_sql = mock_cursor.execute.call_args_list[1].args[0]
        count_sql = mock_cursor.execute.call_args_list[2].args[0]
        assert merge_sql == m._RESOLVE_FROM_COUNTY_BACKFILL_SQL
        assert count_sql == m._COUNT_UNRESOLVABLE_BACKFILL_SQL

    def test_backfill_true_still_binds_the_same_source_and_sp_exec_id(self, mocker):
        """The scope changes (which poles are candidates), but the
        Source/SP_ExecId values written for each resolved row don't --
        still CountyTimeZones, still this run's own SP_Execution id."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(77,), (0,), (0,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones(backfill=True)

        merge_call_args = mock_cursor.execute.call_args_list[1].args
        _, source1, sp_exec_id1, source2, sp_exec_id2 = merge_call_args
        assert (source1, source2) == ("CountyTimeZones", "CountyTimeZones")
        assert (sp_exec_id1, sp_exec_id2) == (77, 77)

    def test_backfill_log_message_notes_it_was_a_backfill(self, mocker, caplog):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with caplog.at_level("INFO"):
                m.load_pole_timezones(backfill=True)

        info_messages = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
        assert any("backfill" in msg for msg in info_messages)

    def test_non_backfill_log_message_does_not_mention_backfill(self, mocker, caplog):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = 5

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with caplog.at_level("INFO"):
                m.load_pole_timezones()

        info_messages = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
        assert not any("backfill" in msg for msg in info_messages)



    def test_full_success_flow_call_sequence(self, mocker):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(42,), (0,), (0,)]  # SP_Exec insert, unresolvable count, duplicate count
        mock_cursor.rowcount = 15

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 5

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleTimeZones", "Dev", "Leadsun")

        merge_sql, source1, sp_exec_id1, source2, sp_exec_id2 = calls[1].args
        assert merge_sql == m._RESOLVE_FROM_COUNTY_SQL
        assert (source1, source2) == ("CountyTimeZones", "CountyTimeZones")
        assert (sp_exec_id1, sp_exec_id2) == (42, 42)

        count_sql = calls[2].args[0]
        assert count_sql == m._COUNT_UNRESOLVABLE_SQL

        duplicate_count_sql = calls[3].args[0]
        assert duplicate_count_sql == m._COUNT_DUPLICATE_LOCATION_IDS_SQL

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[4].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (15, 0, 1, 42)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_zero_rowcount_reports_zero_not_negative(self, mocker):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success = final_update_args[2]
        assert success == 0

    def test_unresolvable_count_logs_a_warning_when_nonzero(self, mocker, caplog):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (7,), (0,)]  # 7 unresolvable poles, 0 duplicates
        mock_cursor.rowcount = 3

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with caplog.at_level("WARNING"):
                m.load_pole_timezones()

        warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert any("7 pole(s)" in w and "no resolvable CountyFips" in w for w in warnings)

    def test_unresolvable_count_of_zero_does_not_log_a_warning(self, mocker, caplog):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (0,), (0,)]
        mock_cursor.rowcount = 3

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with caplog.at_level("WARNING"):
                m.load_pole_timezones()

        warnings = [rec.message for rec in caplog.records if rec.levelname == "WARNING"]
        assert not any("no resolvable CountyFips" in w for w in warnings)

    def test_unresolvable_count_is_diagnostic_only_not_counted_as_an_error(self, mocker):
        """Poles with no resolvable CountyFips are a Poles data-quality
        gap, not something THIS run did wrong -- must not inflate
        TotalErrorRecords."""
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchone.side_effect = [(1,), (50,), (0,)]
        mock_cursor.rowcount = 3

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            m.load_pole_timezones()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        errors = final_update_args[3]
        assert errors == 0


class TestLoadPoleTimezonesTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(self):
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.execute.side_effect = RuntimeError("db down")

        with patch("shared.pole_timezones_loader.get_connection", return_value=mock_conn):
            with pytest.raises(RuntimeError, match="db down"):
                m.load_pole_timezones()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestFailureRecordingUsesAFreshConnection:
    """
    Same fix, and same reasoning, as pole_daylight_flags_loader.py's own
    equivalent: recording a run's failure in SP_Execution must not reuse
    a connection that may itself be the thing that just failed (e.g. a
    genuine SQLSTATE 08S01 "Communication link failure"), or that SECOND
    failure propagates instead of the original, more useful one.
    """

    def _make_conn(self):
        conn = MagicMock(name="conn")
        cursor = MagicMock(name="cursor")
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_original_exception_still_raised_when_recording_succeeds(self, mocker):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.execute.side_effect = [None, RuntimeError("communication link failure")]

        recovery_conn, recovery_cursor = self._make_conn()

        mocker.patch(
            "shared.pole_timezones_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="communication link failure"):
            m.load_pole_timezones()

        assert recovery_cursor.execute.called
        update_sql, end_time, error_message, success, errors, sp_exec_id = (
            recovery_cursor.execute.call_args.args
        )
        assert "UPDATE SP_Execution" in update_sql
        assert "communication link failure" in error_message
        assert sp_exec_id == 55
        recovery_conn.commit.assert_called_once()
        recovery_cursor.close.assert_called_once()
        recovery_conn.close.assert_called_once()
        main_cursor.close.assert_called_once()
        main_conn.close.assert_called_once()

    def test_original_exception_still_raised_when_recording_also_fails(self, mocker, caplog):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.execute.side_effect = [None, RuntimeError("original communication failure")]

        recovery_conn, recovery_cursor = self._make_conn()
        recovery_cursor.execute.side_effect = RuntimeError("recovery also failed")

        mocker.patch(
            "shared.pole_timezones_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="original communication failure"):
                m.load_pole_timezones()

        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("original communication failure" in msg for msg in error_messages)
        assert any(
            "additionally failed to record this run's failure" in msg and "recovery also failed" in msg
            for msg in error_messages
        )
