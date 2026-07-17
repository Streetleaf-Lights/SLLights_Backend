"""Tests for shared/pole_vitals_loader.py"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shared import pole_vitals_loader

EASTERN = ZoneInfo("America/New_York")


def _eastern(*args):
    """Builds an aware Eastern datetime, matching what now_eastern() returns
    in production -- _compute_cutoff()/to_dto_string() require tzinfo."""
    return datetime(*args, tzinfo=EASTERN)


# --------------------------------------------------------------------------
# _compute_cutoff -- pure function, no database needed
# --------------------------------------------------------------------------


class TestComputeCutoff:
    def test_hour_default_lookback(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=False)
        expected = now - timedelta(hours=3)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_day_default_lookback(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Day", backfill=False)
        expected = now - timedelta(days=2)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_week_default_lookback(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Week", backfill=False)
        expected = now - timedelta(days=8)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_month_default_lookback(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Month", backfill=False)
        expected = now - timedelta(days=35)
        assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M"))

    def test_backfill_uses_wide_window_regardless_of_period_type(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in pole_vitals_loader.PERIOD_TYPES:
            cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=True)
            expected = now - timedelta(days=400)
            assert cutoff.startswith(expected.strftime("%Y-%m-%d %H:%M")), period_type

    def test_backfill_window_wider_than_default_for_every_period_type(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        for period_type in pole_vitals_loader.PERIOD_TYPES:
            default_cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=False)
            backfill_cutoff = pole_vitals_loader._compute_cutoff(now, period_type, backfill=True)
            # Earlier cutoff = wider lookback window
            assert backfill_cutoff < default_cutoff, period_type

    def test_returns_dto_formatted_string(self):
        now = _eastern(2026, 7, 15, 14, 30, 0)
        cutoff = pole_vitals_loader._compute_cutoff(now, "Hour", backfill=False)
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$", cutoff)


# --------------------------------------------------------------------------
# Structural checks on each period type's MERGE SQL. These can't verify
# actual aggregation correctness (that needs a real SQL Server -- not
# available in this sandbox), but they do catch structural drift/typos and
# document exactly what's expected of each statement.
# --------------------------------------------------------------------------


class TestMergeSqlStructureCommon:
    """Checks that apply identically to all four period types."""

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_has_exactly_four_placeholders(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert sql.count("?") == 4  # cutoff, sentinel-exclusion, Source, SP_ExecId

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_excludes_missing_last_upload_sentinel(self, period_type):
        """
        PoleTelemetry rows with a genuinely-missing LastUpload get the
        far-future sentinel timestamp (see pole_telemetry_loader.py) so
        their composite PK stays valid -- but that sentinel is always
        ">= cutoff" for any reasonable lookback window, and DATEADD-ing a
        day/month onto a bucket derived from it overflows DATE's max
        value (SQLSTATE 22007). Must be explicitly excluded.
        """
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "AND t.LastUpload <> ?" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_joins_pole_models_on_model_id(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_battery_percentage_formula(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_panel_percentage_formula_uses_sunboard_power_with_nullif_guard(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_light_percentage_formula_uses_light_power_with_nullif_guard(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "(t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_filters_by_last_upload_cutoff(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "WHERE t.LastUpload >= ?" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_uses_eastern_time_zone_for_bucketing(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "AT TIME ZONE 'Eastern Standard Time'" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_match_key_is_location_period_type_period_start(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "ON target.LocationId = source.LocationId" in sql
        assert "AND target.PeriodType = source.PeriodType" in sql
        assert "AND target.PeriodStart = source.PeriodStart" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_aggregates_with_avg_and_count(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "AVG(BatteryPercentage)" in sql
        assert "AVG(PanelPercentage)" in sql
        assert "AVG(LightPercentage)" in sql
        assert "COUNT(*)" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_is_merge_not_plain_insert(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "MERGE PoleVitals AS target" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN" in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_no_fk_references(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert "REFERENCES" not in sql

    @pytest.mark.parametrize("period_type", pole_vitals_loader.PERIOD_TYPES)
    def test_correct_period_type_literal(self, period_type):
        sql = pole_vitals_loader._MERGE_SQL_BY_PERIOD_TYPE[period_type]
        assert f"'{period_type}' AS PeriodType" in sql


class TestHourMergeSqlBucketing:
    def test_truncates_to_the_hour(self):
        sql = pole_vitals_loader._HOUR_MERGE_SQL
        assert "DATEADD(HOUR, DATEDIFF(HOUR, '19000101', LocalTime), '19000101')" in sql

    def test_period_end_is_one_hour_after_start(self):
        sql = pole_vitals_loader._HOUR_MERGE_SQL
        assert "DATEADD(HOUR, 1, BucketStart) AT TIME ZONE 'Eastern Standard Time' AS PeriodEnd" in sql


class TestDayMergeSqlBucketing:
    def test_truncates_to_the_date(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "CAST(LocalTime AS DATE) AS BucketStart" in sql

    def test_period_end_is_one_day_after_start(self):
        sql = pole_vitals_loader._DAY_MERGE_SQL
        assert "DATEADD(DAY, 1, BucketStart)" in sql


class TestMonthMergeSqlBucketing:
    def test_truncates_to_first_of_month(self):
        sql = pole_vitals_loader._MONTH_MERGE_SQL
        assert "DATEFROMPARTS(YEAR(LocalTime), MONTH(LocalTime), 1) AS BucketStart" in sql

    def test_period_end_is_one_month_after_start(self):
        sql = pole_vitals_loader._MONTH_MERGE_SQL
        assert "DATEADD(MONTH, 1, BucketStart)" in sql


class TestWeekMergeSqlBucketing:
    def test_joins_workweek_table(self):
        sql = pole_vitals_loader._WEEK_MERGE_SQL
        assert "JOIN Workweek w ON CAST(tv.LocalTime AS DATE) BETWEEN w.StartDate AND w.EndDate" in sql

    def test_uses_workweek_start_and_end_dates_as_bucket_boundaries(self):
        sql = pole_vitals_loader._WEEK_MERGE_SQL
        assert "w.StartDate AS BucketStart" in sql
        assert "w.EndDate AS BucketEnd" in sql

    def test_period_end_is_one_day_after_workweek_end_date(self):
        """PeriodEnd is exclusive (start of next period) -- Workweek's own
        EndDate is inclusive (the Saturday itself), so this needs the +1
        day to convert from inclusive to exclusive, consistent with the
        other three period types' exclusive-end convention."""
        sql = pole_vitals_loader._WEEK_MERGE_SQL
        assert "DATEADD(DAY, 1, BucketEnd)" in sql

    def test_does_not_use_raw_date_math_for_bucketing(self):
        """Week bucketing must come from the Workweek table, not
        DATEPART/ISO-week functions -- per the explicit request to use
        "the Workweek definition"."""
        sql = pole_vitals_loader._WEEK_MERGE_SQL
        assert "DATEPART(WEEK" not in sql
        assert "DATEPART(ISO_WEEK" not in sql


# --------------------------------------------------------------------------
# load_pole_vitals() -- full flow
# --------------------------------------------------------------------------


class TestLoadPoleVitalsSuccessFlow:
    def test_full_success_flow_executes_all_four_period_types_in_order(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (77,)
        mock_cursor.rowcount = 5

        pole_vitals_loader.load_pole_vitals()

        calls = mock_cursor.execute.call_args_list
        # insert SP_Execution, 4x MERGE, final update
        assert len(calls) == 6

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleVitals", "Dev", "Leadsun")

        merge_calls = calls[1:5]
        for period_type, call in zip(pole_vitals_loader.PERIOD_TYPES, merge_calls):
            merge_sql, cutoff, sentinel, source_name, sp_exec_id = call.args
            assert f"'{period_type}' AS PeriodType" in merge_sql
            assert sentinel == pole_vitals_loader._MISSING_LAST_UPLOAD_SENTINEL
            assert source_name == "Leadsun"
            assert sp_exec_id == 77

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[5].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (20, 0, 4, 77)  # 5 rows x 4 period types

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_default_run_uses_small_lookback_not_backfill_window(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=False)

        merge_calls = mock_cursor.execute.call_args_list[1:5]
        cutoffs = [call.args[1] for call in merge_calls]
        # None of the default-run cutoffs should be as far back as the
        # ~400-day backfill window would produce.
        for cutoff in cutoffs:
            assert cutoff > "2025-06-01"  # comfortably within ~13 months, not 400 days

    def test_backfill_true_uses_wide_lookback_for_every_period_type(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 0

        pole_vitals_loader.load_pole_vitals(backfill=True)

        merge_calls = mock_cursor.execute.call_args_list[1:5]
        cutoffs = [call.args[1] for call in merge_calls]
        # All four period types should use the SAME wide backfill cutoff.
        assert len(set(cutoffs)) == 1

    def test_zero_rowcount_does_not_go_negative_or_none(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = -1  # pyodbc convention for "not applicable"

        pole_vitals_loader.load_pole_vitals()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert success == 0
        assert errors == 0


class TestIsBenignNullAggregateWarning:
    def test_recognizes_sqlstate_01003(self):
        exc = Exception(
            "01003",
            "[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Warning: Null value is eliminated by an aggregate or other SET operation. "
            "(8153) (SQLExecDirectW)",
        )
        assert pole_vitals_loader._is_benign_null_aggregate_warning(exc) is True

    def test_does_not_recognize_a_genuine_error(self):
        exc = Exception(
            "22007",
            "[22007] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Adding a value to a 'date' column caused an overflow. (517) (SQLExecDirectW)",
        )
        assert pole_vitals_loader._is_benign_null_aggregate_warning(exc) is False

    def test_does_not_recognize_a_plain_exception_with_no_sqlstate(self):
        assert pole_vitals_loader._is_benign_null_aggregate_warning(RuntimeError("boom")) is False

    def test_does_not_crash_on_an_exception_with_no_args(self):
        assert pole_vitals_loader._is_benign_null_aggregate_warning(Exception()) is False


class TestLoadPoleVitalsBenignWarningHandling:
    def _make_01003_exception(self):
        return Exception(
            "01003",
            "[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Warning: Null value is eliminated by an aggregate or other SET operation. "
            "(8153) (SQLExecDirectW)",
        )

    def test_01003_warning_is_not_counted_as_an_error(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 7
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            self._make_01003_exception(),  # Hour MERGE -- benign
            None,  # Day
            None,  # Week
            None,  # Month
            None,  # final update
        ]

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 0  # the 01003 "failure" must not count as an error
        assert success == 28  # 4 period types x 7 rows each, including Hour

    def test_01003_warning_logs_as_info_not_error(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 3
        mock_cursor.execute.side_effect = [
            None,
            self._make_01003_exception(),
            None,
            None,
            None,
            None,
        ]

        with caplog.at_level("INFO"):
            pole_vitals_loader.load_pole_vitals()

        info_messages = [rec.message for rec in caplog.records if rec.levelname == "INFO"]
        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("Hour period recomputed" in m and "expected, not an error" in m for m in info_messages)
        assert not any("failed to recompute Hour" in m for m in error_messages)

    def test_genuine_22007_overflow_still_counts_as_a_real_error(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        """Sanity check that the benign-warning carve-out doesn't
        accidentally swallow a real error too -- e.g. the date-overflow
        bug this exact carve-out was added alongside a fix for."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.rowcount = 5
        overflow_exc = Exception(
            "22007",
            "[22007] ... Adding a value to a 'date' column caused an overflow. (517)",
        )
        mock_cursor.execute.side_effect = [None, None, overflow_exc, None, None, None]

        pole_vitals_loader.load_pole_vitals()  # must not raise (per-period isolation)

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1


class TestLoadPoleVitalsPartialFailure:
    def test_one_period_type_failing_does_not_block_the_others(
        self, patch_get_connection_pole_vitals, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        # insert SP_Execution succeeds, then Hour fails, Day/Week/Month
        # succeed, then final update succeeds.
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            RuntimeError("Hour failed"),  # Hour MERGE
            None,  # Day MERGE
            None,  # Week MERGE
            None,  # Month MERGE
            None,  # final update
        ]
        mock_cursor.rowcount = 3

        pole_vitals_loader.load_pole_vitals()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert errors == 1
        assert success == 9  # 3 successful period types x 3 rows each

    def test_logs_error_for_failed_period_type(
        self, patch_get_connection_pole_vitals, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.execute.side_effect = [
            None,
            RuntimeError("boom"),
            None,
            None,
            None,
            None,
        ]
        mock_cursor.rowcount = 0

        with caplog.at_level("ERROR"):
            pole_vitals_loader.load_pole_vitals()

        assert any(
            "failed to recompute Hour period" in rec.message for rec in caplog.records
        )


class TestLoadPoleVitalsTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_vitals, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db connection lost")

        with pytest.raises(RuntimeError, match="db connection lost"):
            pole_vitals_loader.load_pole_vitals()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
