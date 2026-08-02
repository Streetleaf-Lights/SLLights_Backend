"""Tests for shared/pole_daylight_flags_loader.py"""

from datetime import datetime, timezone

import pytest

from shared import pole_daylight_flags_loader
from shared.datetime_utils import to_dto_string


class TestFindUnflaggedSql:
    def test_only_selects_rows_missing_is_daylight(self):
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert "WHERE t.IsDaylight IS NULL" in sql

    def test_only_trusts_locations_with_a_resolved_windows_timezone(self):
        """A NULL WindowsTimeZone means PoleTimeZones' stored coordinates
        for that location couldn't be trusted (Null Island, out-of-range
        values) -- is_daylight() has no defensive validation of its own,
        so this must not feed it those same coordinates."""
        sql = pole_daylight_flags_loader._FIND_UNFLAGGED_SQL
        assert "ptz.WindowsTimeZone IS NOT NULL" in sql

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
        reading_1_time = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        reading_2_time = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
        mock_cursor.fetchall.return_value = [
            ("12009-1000", reading_1_time, 27.99507, -80.7236),
            ("12009-1000", reading_2_time, 27.99507, -80.7236),
        ]
        mock_resolve = mocker.patch(
            "shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, False]
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
        assert executemany_params == [
            (True, "12009-1000", to_dto_string(reading_1_time)),
            (False, "12009-1000", to_dto_string(reading_2_time)),
        ]

        assert mock_resolve.call_count == 2
        mock_resolve.assert_any_call(reading_1_time, 27.99507, -80.7236)

        final_update_args = calls[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (2, 0)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

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
        written_last_upload = params[0][2]
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
        assert params[0][2] == to_dto_string(reading_time)

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
        written_last_upload = update_calls[0].args[3]
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
            side_effect=[ValueError("boom"), True],
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
        mocker.patch("shared.pole_daylight_flags_loader.is_daylight", side_effect=[True, False])
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
