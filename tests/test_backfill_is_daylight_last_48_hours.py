"""Tests for scripts/backfill_is_daylight_last_48_hours.py"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from scripts.backfill_is_daylight_last_48_hours import (
    _COUNT_REMAINING_SQL,
    _MAX_CONSECUTIVE_FAILURES,
    _MAX_ITERATIONS,
    _RETRY_BACKOFF_SECONDS,
    count_remaining_unflagged_in_window,
    load_local_settings_into_env,
    refuse_if_prod,
    run_backfill_loop,
)


class TestRefuseIfProd:
    def test_raises_for_prod(self):
        with pytest.raises(SystemExit):
            refuse_if_prod("Prod")

    def test_does_not_raise_for_dev(self):
        refuse_if_prod("Dev")  # must not raise

    def test_does_not_raise_for_other_environments(self):
        refuse_if_prod("Staging")  # must not raise


class TestLoadLocalSettingsIntoEnv:
    def test_returns_false_when_file_missing(self, tmp_path):
        result = load_local_settings_into_env(project_root=tmp_path)
        assert result is False

    def test_loads_values_into_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SOME_TEST_KEY", raising=False)
        settings_file = tmp_path / "local.settings.json"
        settings_file.write_text(json.dumps({"Values": {"SOME_TEST_KEY": "some-value"}}))

        result = load_local_settings_into_env(project_root=tmp_path)

        assert result is True
        assert os.environ["SOME_TEST_KEY"] == "some-value"

    def test_does_not_override_an_already_set_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOME_TEST_KEY", "already-set-value")
        settings_file = tmp_path / "local.settings.json"
        settings_file.write_text(json.dumps({"Values": {"SOME_TEST_KEY": "from-file-value"}}))

        load_local_settings_into_env(project_root=tmp_path)

        assert os.environ["SOME_TEST_KEY"] == "already-set-value"

    def test_handles_missing_values_key_gracefully(self, tmp_path):
        settings_file = tmp_path / "local.settings.json"
        settings_file.write_text(json.dumps({"IsEncrypted": False}))

        result = load_local_settings_into_env(project_root=tmp_path)

        assert result is True  # file existed and was valid JSON, just no Values


class TestCountRemainingSql:
    def test_mirrors_find_unflagged_sqls_own_conditions(self):
        """Must match _FIND_UNFLAGGED_SQL's own WindowsTimeZone IS NOT
        NULL / INNER JOIN conditions exactly -- a row that
        load_pole_daylight_flags() itself would never flag (no resolved
        timezone) must not count as "still pending" here either, or this
        script's loop would never terminate waiting on it."""
        sql = _COUNT_REMAINING_SQL
        assert "JOIN PoleTimeZones ptz" in sql
        assert "LEFT JOIN" not in sql  # INNER JOIN, not LEFT
        assert "AND ptz.WindowsTimeZone IS NOT NULL" in sql

    def test_counts_a_row_missing_either_daylight_column(self):
        """Must match _FIND_UNFLAGGED_SQL's own OR condition exactly --
        IsDaylightForLedFault was added after IsDaylight already existed
        and was, in practice, already backfilled for the
        operationally-relevant window, so a row missing only the newer
        column must still count as "still pending" here too, or this
        script would report "done" while that column remained
        unflagged."""
        sql = _COUNT_REMAINING_SQL
        assert "WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL)" in sql

    def test_filters_by_cutoff_and_excludes_sentinel(self):
        sql = _COUNT_REMAINING_SQL
        assert "AND t.LastUpload >= ?" in sql
        assert "AND t.LastUpload <> ?" in sql


class TestCountRemainingUnflaggedInWindow:
    def test_returns_the_count_and_binds_params_correctly(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("shared.sql_client.get_connection", return_value=mock_conn):
            result = count_remaining_unflagged_in_window("2026-08-01 00:00:00.000 -04:00", "sentinel")

        assert result == 42
        sql, cutoff, sentinel = mock_cursor.execute.call_args.args
        assert cutoff == "2026-08-01 00:00:00.000 -04:00"
        assert sentinel == "sentinel"

    def test_closes_cursor_and_connection(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("shared.sql_client.get_connection", return_value=mock_conn):
            count_remaining_unflagged_in_window("cutoff", "sentinel")

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_closes_even_on_failure(self):
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = RuntimeError("db down")
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch("shared.sql_client.get_connection", return_value=mock_conn):
            with pytest.raises(RuntimeError):
                count_remaining_unflagged_in_window("cutoff", "sentinel")

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestMaxIterationsSafetyCap:
    def test_is_generously_above_the_realistic_iteration_estimate(self):
        """~1.12M rows / 20000 per batch =~ 56 iterations in the worst
        realistic case (see this script's own module docstring for that
        estimate) -- the cap needs real headroom above that so a normal,
        if slow, run never hits it, while still being a bounded, finite
        number rather than no cap at all."""
        assert _MAX_ITERATIONS > 100


class TestRunBackfillLoop:
    """
    Coverage for the retry-with-backoff behavior added after a real
    production incident: a single transient failure (SQLSTATE 08S01,
    "Communication link failure" from a dropped connection mid-batch)
    was killing this entire script and losing all its progress, even
    though a fresh connection on the next attempt would likely have
    succeeded. sleep_fn is always a no-op fake here -- these tests care
    that backoff/retry HAPPENS, not that it actually pauses for real
    seconds.
    """

    def _make_loop_deps(self, remaining_sequence, daylight_fn_side_effect=None):
        """
        remaining_sequence: values count_remaining_fn returns on
        successive calls (first call is the initial check before the
        loop even starts).
        daylight_fn_side_effect: passed straight through to a MagicMock's
        side_effect for load_pole_daylight_flags_fn -- a list mixing
        None (success) and exception instances (failure) drives the
        scenario.
        """
        count_remaining_fn = MagicMock(side_effect=list(remaining_sequence))
        load_fn = MagicMock(side_effect=daylight_fn_side_effect)
        sleep_fn = MagicMock()
        return count_remaining_fn, load_fn, sleep_fn

    def test_converges_normally_with_no_failures(self):
        count_remaining_fn, load_fn, sleep_fn = self._make_loop_deps(
            remaining_sequence=[500000, 300000, 100000, 0]
        )

        run_backfill_loop("cutoff", "sentinel", load_fn, count_remaining_fn, sleep_fn)

        assert load_fn.call_count == 3
        assert count_remaining_fn.call_count == 4  # initial + one per iteration
        sleep_fn.assert_not_called()  # no failures, no backoff needed

    def test_already_caught_up_never_calls_the_daylight_function(self):
        count_remaining_fn, load_fn, sleep_fn = self._make_loop_deps(remaining_sequence=[0])

        run_backfill_loop("cutoff", "sentinel", load_fn, count_remaining_fn, sleep_fn)

        load_fn.assert_not_called()

    def test_transient_failure_retries_and_recovers(self):
        """One failure, then success -- must retry the SAME iteration
        (not skip it or double-count it), and must NOT raise, since a
        single blip followed by recovery is exactly the case this
        exists to handle."""
        count_remaining_fn, load_fn, sleep_fn = self._make_loop_deps(
            remaining_sequence=[100000, 50000, 0],
            daylight_fn_side_effect=[RuntimeError("communication link failure"), None, None],
        )

        run_backfill_loop("cutoff", "sentinel", load_fn, count_remaining_fn, sleep_fn)

        assert load_fn.call_count == 3  # failed once, retried, succeeded twice more
        sleep_fn.assert_called_once_with(_RETRY_BACKOFF_SECONDS)

    def test_consecutive_failure_count_resets_after_a_success(self):
        """2 failures, 1 success, then 2 more failures should NOT trip
        the consecutive-failure cap (5) even though 4 total failures
        happened -- only genuinely CONSECUTIVE failures should count,
        since a success in between means the underlying issue clearly
        isn't persistent."""
        count_remaining_fn, load_fn, sleep_fn = self._make_loop_deps(
            remaining_sequence=[100000, 50000, 0],
            daylight_fn_side_effect=[
                RuntimeError("fail 1"),
                RuntimeError("fail 2"),
                None,  # resets the consecutive counter
                RuntimeError("fail 3"),
                RuntimeError("fail 4"),
                None,
            ],
        )

        run_backfill_loop("cutoff", "sentinel", load_fn, count_remaining_fn, sleep_fn)

        assert load_fn.call_count == 6
        assert sleep_fn.call_count == 4  # once per failure, regardless of the reset

    def test_persistent_consecutive_failures_stop_before_max_iterations(self):
        """5 failures in a row (== _MAX_CONSECUTIVE_FAILURES) must stop
        the run, with a message distinguishing this from the separate
        _MAX_ITERATIONS cap -- and must do so well before burning through
        300 iterations waiting to find out the issue isn't transient."""
        count_remaining_fn, load_fn, sleep_fn = self._make_loop_deps(
            remaining_sequence=[100000],  # only the initial check -- loop never gets to recheck
            daylight_fn_side_effect=[RuntimeError("persistent failure")] * 10,
        )

        with pytest.raises(SystemExit, match="consecutive failures"):
            run_backfill_loop("cutoff", "sentinel", load_fn, count_remaining_fn, sleep_fn)

        assert load_fn.call_count == _MAX_CONSECUTIVE_FAILURES

    def test_max_iterations_cap_still_applies_when_making_slow_progress(self):
        """Progress IS happening (remaining decreases every time, no
        failures at all) but never reaches 0 within _MAX_ITERATIONS --
        must still stop with the iterations-specific message, distinct
        from the consecutive-failures one."""
        # One more than _MAX_ITERATIONS distinct remaining-values, never
        # hitting 0, so the loop is still going when the cap triggers.
        remaining_sequence = [1000000 - i for i in range(_MAX_ITERATIONS + 2)]
        count_remaining_fn, load_fn, sleep_fn = self._make_loop_deps(
            remaining_sequence=remaining_sequence
        )

        with pytest.raises(SystemExit, match=f"Stopping after {_MAX_ITERATIONS} iterations"):
            run_backfill_loop("cutoff", "sentinel", load_fn, count_remaining_fn, sleep_fn)

        sleep_fn.assert_not_called()  # no failures occurred, only slow progress

