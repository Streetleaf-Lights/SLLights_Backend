"""Tests for shared/pole_daylight_flags_loader.py"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from shared import pole_daylight_flags_loader
from shared.datetime_utils import to_dto_string


class TestFindUnflaggedSql:
    def test_only_selects_rows_missing_any_daylight_column(self):
        """OR, not just IsDaylight IS NULL: both IsDaylightForLedFault
        and IsDaylightForPanelFault were added after IsDaylight already
        existed and were, in practice, already backfilled for the
        operationally-relevant window at various points -- a row already
        has IsDaylight set must still be revisited if it's missing
        either newer column."""
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert (
            "WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL "
            "OR t.IsDaylightForPanelFault IS NULL)" in sql
        )

    def test_only_trusts_locations_with_a_resolved_windows_timezone(self):
        """A NULL WindowsTimeZone means PoleTimeZones' stored coordinates
        for that location couldn't be trusted (Null Island, out-of-range
        values) -- is_daylight() has no defensive validation of its own,
        so this must not feed it those same coordinates."""
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert "ptz.WindowsTimeZone IS NOT NULL" in sql

    def test_excludes_the_sentinel_last_upload_value(self):
        """A real production bug this was missing for: the
        '9999-12-31 23:59:59.999' sentinel marks "no real telemetry yet"
        throughout this project (already excluded elsewhere, e.g. the
        backfill script's own _COUNT_REMAINING_SQL), but was missing
        here. Without it, this loader would repeatedly (every single
        run, forever) attempt and fail to compute daylight for a
        sentinel row -- its IsDaylight columns can never get set, so it
        never stops matching the "still unflagged" condition -- wasting
        _BATCH_SIZE capacity that could go to genuinely processable rows.
        Also a hard requirement, not just wasteful: is_daylight()'s own
        ± grace-period arithmetic can overflow Python's datetime.max when
        applied to a date already this extreme, confirmed in practice as
        a genuine "date value out of range" failure for exactly this
        value."""
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert "AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'" in sql

    def test_uses_inner_join_not_left_join(self):
        """A LocationId with no PoleTimeZones entry at all can't have its
        daylight computed yet -- must be excluded, not included with
        NULL coordinates."""
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert "JOIN PoleTimeZones ptz" in sql
        assert "LEFT JOIN PoleTimeZones" not in sql

    def test_is_bounded_by_a_batch_size_parameter(self):
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert "TOP (?)" in sql


class TestLoadPoleDaylightFlagsSuccessFlow:
    def test_full_success_flow_computes_and_stores_each_flag(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (55,)
        reading_1_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # clearly daylight, past warmup
        reading_2_time = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)  # clearly dark, even with any grace period
        mock_cursor.fetchall.return_value = [
            ("12009-1000", reading_1_time, 27.99507, -80.7236),
            ("12009-1000", reading_2_time, 27.99507, -80.7236),
        ]
        # Reading 1: daylight=True at the exact moment -- short-circuits
        # IsLedFaultFlag's own `or` chain (no further calls needed
        # there), but IsPanelFaultFlag's `and` chain now needs BOTH its
        # own checks -- sunrise warmup AND sunset winddown -- 3 calls
        # total for this reading. Reading 2: daylight=False at the exact
        # moment -- short-circuits IsPanelFaultFlag's `and` chain
        # immediately (no call needed there at all, since the very first
        # operand is already False), but IsLedFaultFlag's own `or` chain
        # still needs both its checks (neither comes back True, a
        # reading well into the night) -- 3 calls total for this
        # reading. 6 calls overall, not 5.
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight",
            side_effect=[True, True, True, False, False, False],
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        calls = mock_cursor.execute.call_args_list
        assert "INSERT INTO SP_Execution" in calls[0].args[0]
        assert calls[1].args[0] == pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert calls[1].args[1] == pole_daylight_flags_loader._BATCH_SIZE

        # executemany used for the bulk write (not per-row execute())
        assert mock_cursor.executemany.called
        executemany_sql, executemany_params = mock_cursor.executemany.call_args.args
        assert executemany_sql == pole_daylight_flags_loader._UPDATE_IS_DAYLIGHT_SQL
        # LastUpload must be the DTO-formatted STRING, not the raw
        # datetime object read back from the SELECT -- see
        # TestLastUploadIsFormattedAsDtoString below for why this
        # specific detail is the actual bug this loader was fixed for.
        # Each tuple is (IsDaylight, IsDaylightForLedFault,
        # IsDaylightForPanelFault, LocationId, LastUpload) -- reading 1's
        # LED value is True purely from the short-circuit (daylight=True
        # already answers it), and its Panel value is True from BOTH its
        # own separate checks (warmup AND winddown) passing; reading 2's
        # LED value is False since both its checks came back False, and
        # its Panel value is False purely from ITS OWN short-circuit
        # (daylight=False already answers it, neither check needed at
        # all).
        assert executemany_params == [
            (True, True, True, "12009-1000", to_dto_string(reading_1_time)),
            (False, False, False, "12009-1000", to_dto_string(reading_2_time)),
        ]

        assert mock_resolve.call_count == 6
        mock_resolve.assert_any_call(reading_1_time, 27.99507, -80.7236)
        mock_resolve.assert_any_call(
            reading_1_time - pole_daylight_flags_loader._PANEL_FAULT_SUNRISE_WARMUP_PERIOD, 27.99507, -80.7236
        )
        mock_resolve.assert_any_call(
            reading_1_time + pole_daylight_flags_loader._PANEL_FAULT_SUNSET_WINDDOWN_PERIOD, 27.99507, -80.7236
        )
        mock_resolve.assert_any_call(reading_2_time, 27.99507, -80.7236)
        mock_resolve.assert_any_call(
            reading_2_time - pole_daylight_flags_loader._LED_FAULT_GRACE_PERIOD, 27.99507, -80.7236
        )
        mock_resolve.assert_any_call(
            reading_2_time + pole_daylight_flags_loader._LED_FAULT_GRACE_PERIOD, 27.99507, -80.7236
        )

        final_update_args = calls[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (2, 0)

    def test_grace_period_check_is_skipped_when_already_confirmed_daylight(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """`daylight or is_daylight(...)` short-circuits -- confirmed
        daylight at the exact moment already answers
        IsDaylightForLedFault's own question, so its second, grace-period
        is_daylight() call should never happen at all for this reading.
        IsDaylightForPanelFault's own checks are a separate `and` chain,
        though, and STILL need to run even when daylight is already True
        -- 3 total calls, not 1 (one for the exact moment, one each for
        the sunrise warmup and sunset winddown checks)."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, True, True]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()  # must not raise StopIteration

        assert mock_resolve.call_count == 3

    def test_grace_period_check_catches_a_reading_right_after_sunset(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """The exact real-world case this whole column exists for: a
        reading whose OWN moment is already dark, but was still daylight
        within the grace period before it -- IsDaylightForLedFault must
        come back True (via the grace-period check), even though
        IsDaylight itself is False."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 0, 27, tzinfo=timezone.utc)  # just after sunset
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[False, True]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_value, is_daylight_for_led_fault_value = executemany_params[0][0], executemany_params[0][1]
        assert is_daylight_value is False
        assert is_daylight_for_led_fault_value is True

    def test_grace_period_check_catches_a_reading_right_before_sunrise(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """The mirror case of the sunset test above, on the other end of
        the night: a reading whose own moment is still dark, and was
        ALSO still dark one grace period earlier (so the "before" check
        doesn't help), but WILL be daylight within one grace period
        after it -- some lamps sense approaching dawn light and turn off
        slightly before the astronomical sunrise moment, which the
        "before" check alone could never catch."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 10, 33, tzinfo=timezone.utc)  # just before sunrise
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[False, False, True]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_value, is_daylight_for_led_fault_value = executemany_params[0][0], executemany_params[0][1]
        assert is_daylight_value is False
        assert is_daylight_for_led_fault_value is True

    def test_grace_period_checks_both_fail_for_a_reading_deep_in_the_night(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """A reading nowhere near either transition -- all three checks
        must run (no short-circuit available), and IsDaylightForLedFault
        must correctly stay False, same as the strict IsDaylight."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)  # middle of the night
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[False, False, False]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        assert mock_resolve.call_count == 3
        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_value, is_daylight_for_led_fault_value = executemany_params[0][0], executemany_params[0][1]
        assert is_daylight_value is False
        assert is_daylight_for_led_fault_value is False

    def test_panel_fault_warmup_check_is_skipped_entirely_at_night(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """`daylight and is_daylight(...)` short-circuits when daylight
        is already False -- confirmed night already answers
        IsDaylightForPanelFault's own question (it can never be True at
        night regardless of the warmup check), so that second call
        should never happen at all for this reading."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)  # middle of the night
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[False, False, False]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()  # must not raise StopIteration

        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_for_panel_fault_value = executemany_params[0][2]
        assert is_daylight_for_panel_fault_value is False

    def test_panel_fault_warmup_check_exempts_a_reading_within_the_first_hour_after_sunrise(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """The real-world case this column exists for: a reading whose
        OWN moment is already daylight, but sunrise happened less than
        an hour ago -- IsDaylightForPanelFault must come back False (the
        panel hasn't had time to warm up yet), even though IsDaylight
        itself is True."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 10, 33, tzinfo=timezone.utc)  # just after sunrise
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, False]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_value = executemany_params[0][0]
        is_daylight_for_panel_fault_value = executemany_params[0][2]
        assert is_daylight_value is True
        assert is_daylight_for_panel_fault_value is False

    def test_panel_fault_warmup_check_passes_once_past_the_first_hour_after_sunrise(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """Once it's been daylight for over an hour, AND won't lose
        daylight again within the next hour either, IsDaylightForPanelFault
        must correctly flip to True -- the panel is expected to be
        producing by then."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)  # well past sunrise
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, True, True]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        assert mock_resolve.call_count == 3
        mock_resolve.assert_any_call(
            reading_time - pole_daylight_flags_loader._PANEL_FAULT_SUNRISE_WARMUP_PERIOD, 27.99507, -80.7236
        )
        mock_resolve.assert_any_call(
            reading_time + pole_daylight_flags_loader._PANEL_FAULT_SUNSET_WINDDOWN_PERIOD, 27.99507, -80.7236
        )
        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_for_panel_fault_value = executemany_params[0][2]
        assert is_daylight_for_panel_fault_value is True

    def test_panel_fault_winddown_check_exempts_a_reading_within_the_last_hour_before_sunset(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """The new, mirror-image case at the other end of the day:
        daylight=True, past the sunrise warmup (before check True), but
        the winddown check (will it STILL be daylight an hour from now)
        comes back False -- sunset is coming up soon, so the panel
        shouldn't be expected to still be producing, even though it's
        not dark yet."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 23, 0, tzinfo=timezone.utc)  # just before sunset
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, True, False]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        assert mock_resolve.call_count == 3
        mock_resolve.assert_any_call(
            reading_time + pole_daylight_flags_loader._PANEL_FAULT_SUNSET_WINDDOWN_PERIOD, 27.99507, -80.7236
        )
        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_value = executemany_params[0][0]
        is_daylight_for_panel_fault_value = executemany_params[0][2]
        assert is_daylight_value is True  # strict IsDaylight is still True
        assert is_daylight_for_panel_fault_value is False  # but the panel-fault variant is False

    def test_panel_fault_winddown_check_is_forward_looking_not_backward(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """The winddown check must ADD the period (look into the
        future, "will it still be daylight"), not subtract it -- easy
        to get backwards by copy-pasting the warmup check's own
        subtraction."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, True, True]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        call_args_list = [c.args for c in mock_resolve.call_args_list]
        winddown_call = (
            reading_time + pole_daylight_flags_loader._PANEL_FAULT_SUNSET_WINDDOWN_PERIOD,
            27.99507,
            -80.7236,
        )
        assert winddown_call in call_args_list

    def test_panel_fault_and_led_fault_are_independent_symmetric_but_opposite_operators(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """Both flags now check something before AND after a reading's
        exact moment -- same SHAPE -- but IsLedFaultFlag's own chain is
        an OR (widening the window where zero lamp power is exempted)
        while IsPanelFaultFlag's is an AND (narrowing the window where
        zero panel output is expected). Confirmed here by a case where
        they diverge: daylight=False with both LED grace-period checks
        True (so IsDaylightForLedFault=True via the OR), while
        IsDaylightForPanelFault must stay False regardless (its own
        `and` chain starts from daylight=False, which alone decides it)."""
        mock_cursor.fetchone.return_value = (1,)
        reading_time = datetime(2026, 7, 15, 0, 27, tzinfo=timezone.utc)  # just after sunset
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[False, True]
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()

        executemany_params = mock_cursor.executemany.call_args.args[1]
        is_daylight_for_led_fault_value = executemany_params[0][1]
        is_daylight_for_panel_fault_value = executemany_params[0][2]
        assert is_daylight_for_led_fault_value is True
        assert is_daylight_for_panel_fault_value is False

    def test_no_unflagged_rows_skips_the_write_step_entirely(
        self, patch_get_connection_pole_daylight_flags, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = []

        pole_daylight_flags_loader.load_pole_daylight_flags()

        mock_cursor.executemany.assert_not_called()
        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (0, 0)

    def test_fast_executemany_is_never_set(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """Regression test for a real production bug: cursor.fast_executemany
        = True infers a fixed buffer size for variable-length string
        parameters (LocationId varies in length across poles) from the
        batch -- depending on pyodbc version, a row whose LocationId
        doesn't fit that inferred size can get silently mis-bound.
        executemany() raises no exception either way (so this still
        looks like success, and total_success still gets incremented),
        but the WHERE clause then matches zero rows for that write.
        Confirmed in practice: a direct, single-row UPDATE with values
        copied straight from the table matched correctly, but the
        batched write reported success while changing nothing --
        fast_executemany was the one difference between those two
        paths. Checking mock_cursor.__dict__ directly, not
        hasattr()/getattr() -- MagicMock auto-creates any attribute on
        read access, so hasattr() would report True regardless of
        whether the production code ever actually assigned it."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("12009-1000", datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), 27.99507, -80.7236)]
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)

        pole_daylight_flags_loader.load_pole_daylight_flags()

        assert "fast_executemany" not in mock_cursor.__dict__


class TestProgressLogging:
    """
    Coverage for a real UX gap this was added to fix: this loop can now
    run long enough with zero log output in between (up to twice the
    is_daylight() calls per row it used to, all pure CPU-bound Python
    with no network I/O in between) to look indistinguishable from a
    genuine hang -- confirmed in practice, someone waiting on a large
    batch had no way to tell whether it was progressing or stuck.
    """

    def _make_unflagged_rows(self, count):
        base_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        return [(f"loc-{i}", base_time, 27.99507, -80.7236) for i in range(count)]

    def test_logs_progress_at_each_interval(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = self._make_unflagged_rows(12000)
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)

        with caplog.at_level("INFO"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        progress_messages = [
            rec.message
            for rec in caplog.records
            if rec.levelname == "INFO" and "computed" in rec.message
        ]
        assert any("5000/12000" in msg for msg in progress_messages)
        assert any("10000/12000" in msg for msg in progress_messages)
        # 12000 isn't itself a multiple of _PROGRESS_LOG_INTERVAL (5000)
        # -- confirms this is a periodic "every N rows" log, not simply
        # one line per batch regardless of size.
        assert not any("12000/12000" in msg for msg in progress_messages)

    def test_no_progress_line_for_a_batch_smaller_than_the_interval(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = self._make_unflagged_rows(100)
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)

        with caplog.at_level("INFO"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        progress_messages = [
            rec.message
            for rec in caplog.records
            if rec.levelname == "INFO" and "computed" in rec.message
        ]
        assert progress_messages == []


class TestLastUploadIsFormattedAsDtoString:
    """
    Dedicated regression coverage for a real production bug: last_upload
    comes back from the SELECT as a timezone-aware Python datetime (via
    sql_client.py's DATETIMEOFFSET output converter). Binding that same
    raw datetime object back as a WRITE parameter for WHERE LastUpload = ?
    hits the exact pyodbc + DATETIMEOFFSET gotcha this project already
    knows about (silently mishandled as an input parameter) -- the UPDATE
    doesn't raise, it just silently matches zero rows every time, which
    is worse than an error: SP_Execution kept reporting success
    (total_success += len(updates) happens unconditionally) while nothing
    was actually ever written, for as long as this went unnoticed.
    """

    def test_last_upload_is_a_string_not_a_raw_datetime_in_the_write_params(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        reading_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)

        pole_daylight_flags_loader.load_pole_daylight_flags()

        _, params = mock_cursor.executemany.call_args.args
        written_last_upload = params[0][4]
        assert isinstance(written_last_upload, str)
        assert not isinstance(written_last_upload, datetime)

    def test_written_string_matches_to_dto_string_exactly(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        reading_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)

        pole_daylight_flags_loader.load_pole_daylight_flags()

        _, params = mock_cursor.executemany.call_args.args
        assert params[0][4] == to_dto_string(reading_time)

    def test_row_by_row_fallback_also_uses_the_formatted_string(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        """The fallback path just reuses whatever's already in `updates`
        -- confirms it doesn't somehow reintroduce the raw datetime."""
        reading_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("12009-1000", reading_time, 27.99507, -80.7236)]
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)
        mock_cursor.executemany.side_effect = RuntimeError("batch write failed")

        pole_daylight_flags_loader.load_pole_daylight_flags()

        update_calls = [
            c
            for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_daylight_flags_loader._UPDATE_IS_DAYLIGHT_SQL
        ]
        assert len(update_calls) == 1
        written_last_upload = update_calls[0].args[5]
        assert written_last_upload == to_dto_string(reading_time)


class TestZeroRowsAffectedWarning:
    """
    The exact symptom of the bug above -- executemany() succeeding
    (no exception) but matching zero actual rows -- now gets a loud
    warning instead of silently reporting success, in case this same
    failure mode ever recurs for a different reason.
    """

    def test_zero_rowcount_after_batch_write_logs_a_warning(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [
            ("12009-1000", datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), 27.99507, -80.7236)
        ]
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)
        mock_cursor.rowcount = 0

        with caplog.at_level("WARNING"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        assert any(
            "batch update reported 0 rows affected" in rec.message for rec in caplog.records
        )

    def test_nonzero_rowcount_does_not_log_the_warning(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [
            ("12009-1000", datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), 27.99507, -80.7236)
        ]
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", return_value=True)
        mock_cursor.rowcount = 1

        with caplog.at_level("WARNING"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        assert not any(
            "batch update reported 0 rows affected" in rec.message for rec in caplog.records
        )


class TestLoadPoleDaylightFlagsPartialFailure:
    def test_one_computation_failure_does_not_block_the_others(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        good_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        bad_time = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
        mock_cursor.fetchall.return_value = [
            ("bad-location", bad_time, 999.0, 999.0),
            ("12009-1000", good_time, 27.99507, -80.7236),
        ]
        mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight",
            side_effect=[ValueError("boom"), True, True, True],
        )

        pole_daylight_flags_loader.load_pole_daylight_flags()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)

    def test_batch_write_failure_falls_back_to_row_by_row(
        self, patch_get_connection_pole_daylight_flags, mocker, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        t1 = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
        mock_cursor.fetchall.return_value = [
            ("12009-1000", t1, 27.99507, -80.7236),
            ("12009-1000", t2, 27.99507, -80.7236),
        ]
        mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight",
            side_effect=[True, True, True, False, False, False],
        )
        mock_cursor.executemany.side_effect = RuntimeError("batch write failed")

        pole_daylight_flags_loader.load_pole_daylight_flags()  # must not raise

        # Falls back to individual execute() calls for the UPDATE, one
        # per row, after the batch attempt failed.
        update_calls = [
            c
            for c in mock_cursor.execute.call_args_list
            if c.args[0] == pole_daylight_flags_loader._UPDATE_IS_DAYLIGHT_SQL
        ]
        assert len(update_calls) == 2

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (2, 0)


class TestLoadPoleDaylightFlagsTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_daylight_flags, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestFailureRecordingUsesAFreshConnection:
    """
    Regression coverage for a real production bug: a genuine failure
    (e.g. SQLSTATE 08S01, "Communication link failure") during the main
    operation was being followed by an attempt to record that failure in
    SP_Execution using the SAME connection/cursor that had just failed --
    which, for a connection-level failure specifically, is close to
    guaranteed to fail too. That SECOND failure then propagated instead
    of the original one, replacing a specific, useful error with a
    confusing, unrelated "failed while trying to log the first failure"
    one. The fix: record the failure via a genuinely separate,
    freshly-opened connection, and make sure even if THAT also fails,
    the ORIGINAL exception is still what actually gets raised.
    """

    def _make_conn(self):
        conn = MagicMock(name="conn")
        cursor = MagicMock(name="cursor")
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_original_exception_still_raised_when_recording_succeeds(self, mocker):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)  # SP_Execution insert succeeds
        # Fails partway through the main operation, AFTER sp_exec_id is set.
        main_cursor.fetchall.side_effect = RuntimeError("communication link failure")

        recovery_conn, recovery_cursor = self._make_conn()

        mocker.patch(
            "shared.pole_daylight_flags_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="communication link failure"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        # The failure was recorded via the SEPARATE, second connection --
        # not the original, broken one.
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

        # The original (broken) connection is still cleaned up too.
        main_cursor.close.assert_called_once()
        main_conn.close.assert_called_once()

    def test_original_exception_still_raised_when_recording_also_fails(self, mocker, caplog):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.fetchall.side_effect = RuntimeError("original communication failure")

        recovery_conn, recovery_cursor = self._make_conn()
        # The recovery attempt ALSO fails -- e.g. the underlying network
        # issue wasn't specific to one connection.
        recovery_cursor.execute.side_effect = RuntimeError("recovery also failed")

        mocker.patch(
            "shared.pole_daylight_flags_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with caplog.at_level("ERROR"):
            # The ORIGINAL exception must be what's raised, not the
            # recovery attempt's own exception.
            with pytest.raises(RuntimeError, match="original communication failure"):
                pole_daylight_flags_loader.load_pole_daylight_flags()

        # Both failures are logged, so nothing is silently lost.
        error_messages = [rec.message for rec in caplog.records if rec.levelname == "ERROR"]
        assert any("original communication failure" in m for m in error_messages)
        assert any(
            "additionally failed to record this run's failure" in m and "recovery also failed" in m
            for m in error_messages
        )

    def test_recovery_connection_is_closed_even_if_recording_fails(self, mocker):
        main_conn, main_cursor = self._make_conn()
        main_cursor.fetchone.return_value = (55,)
        main_cursor.fetchall.side_effect = RuntimeError("boom")

        recovery_conn, recovery_cursor = self._make_conn()
        recovery_cursor.execute.side_effect = RuntimeError("recovery failed too")

        mocker.patch(
            "shared.pole_daylight_flags_loader.get_connection",
            side_effect=[main_conn, recovery_conn],
        )

        with pytest.raises(RuntimeError, match="boom"):
            pole_daylight_flags_loader.load_pole_daylight_flags()

        recovery_cursor.close.assert_called_once()
        recovery_conn.close.assert_called_once()

