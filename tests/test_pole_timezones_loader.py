"""Tests for shared/pole_timezones_loader.py"""

import pytest

from shared import pole_timezones_loader


class TestFindUnresolvedLocationsSql:
    def test_only_selects_locations_not_already_in_pole_timezones(self):
        sql = pole_timezones_loader._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId" in sql
        assert "WHERE ptz.LocationId IS NULL" in sql

    def test_excludes_rows_with_missing_coordinates(self):
        sql = pole_timezones_loader._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "t.Longitude IS NOT NULL" in sql
        assert "t.Latitude IS NOT NULL" in sql

    def test_groups_by_location_id(self):
        sql = pole_timezones_loader._FIND_UNRESOLVED_LOCATIONS_SQL
        assert "GROUP BY t.LocationId" in sql


class TestUpsertTimezoneSql:
    def test_is_merge_not_plain_insert(self):
        sql = pole_timezones_loader._UPSERT_TIMEZONE_SQL
        assert "MERGE PoleTimeZones AS target" in sql
        assert "WHEN MATCHED THEN UPDATE SET" in sql
        assert "WHEN NOT MATCHED THEN" in sql

    def test_match_key_is_location_id(self):
        sql = pole_timezones_loader._UPSERT_TIMEZONE_SQL
        assert "ON target.LocationId = source.LocationId" in sql

    def test_has_seven_placeholders(self):
        sql = pole_timezones_loader._UPSERT_TIMEZONE_SQL
        # LocationId, Longitude, Latitude, IanaTimeZone, WindowsTimeZone, Source, SP_ExecId
        assert sql.count("?") == 7


class TestLoadPoleTimezonesSuccessFlow:
    def test_full_success_flow_resolves_and_upserts_each_location(
        self, patch_get_connection_pole_timezones, mocker, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (99,)
        mock_cursor.fetchall.return_value = [
            ("12009-1000", -80.7236, 27.99507),
            ("55555-2000", -87.6298, 41.8781),
        ]
        mock_resolve = mocker.patch(
            "shared.pole_timezones_loader.resolve_windows_timezone",
            side_effect=[
                ("America/New_York", "Eastern Standard Time"),
                ("America/Chicago", "Central Standard Time"),
            ],
        )

        pole_timezones_loader.load_pole_timezones()

        calls = mock_cursor.execute.call_args_list
        # insert SP_Execution, find-unresolved, 2x upsert MERGE, final update
        assert len(calls) == 5

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleTimeZones", "Dev", "Leadsun")

        assert calls[1].args[0] == pole_timezones_loader._FIND_UNRESOLVED_LOCATIONS_SQL

        upsert_call_1 = calls[2].args
        assert upsert_call_1[1:] == (
            "12009-1000", -80.7236, 27.99507, "America/New_York", "Eastern Standard Time",
            "Leadsun", 99,
        )
        upsert_call_2 = calls[3].args
        assert upsert_call_2[1:] == (
            "55555-2000", -87.6298, 41.8781, "America/Chicago", "Central Standard Time",
            "Leadsun", 99,
        )

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[4].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 99)

        assert mock_resolve.call_count == 2
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_no_unresolved_locations_still_closes_out_execution_row(
        self, patch_get_connection_pole_timezones, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = []

        pole_timezones_loader.load_pole_timezones()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 3  # insert, find-unresolved, final update
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[2].args
        assert (success, errors, batch_count) == (0, 0, 1)

    def test_unmapped_windows_timezone_still_upserts_with_null_and_logs_warning(
        self, patch_get_connection_pole_timezones, mocker, mock_cursor, caplog
    ):
        """A location resolving to an IANA zone with no Windows mapping
        still gets a row (so it isn't re-attempted every cycle), just
        with WindowsTimeZone left NULL."""
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("99999-0000", 2.3522, 48.8566)]
        mocker.patch(
            "shared.pole_timezones_loader.resolve_windows_timezone",
            return_value=("Europe/Paris", None),
        )

        with caplog.at_level("WARNING"):
            pole_timezones_loader.load_pole_timezones()

        upsert_args = mock_cursor.execute.call_args_list[2].args
        assert upsert_args[1:] == ("99999-0000", 2.3522, 48.8566, "Europe/Paris", None, "Leadsun", 1)
        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 0)  # still counted as a success, not an error
        assert any("has no resolved Windows timezone" in rec.message for rec in caplog.records)


class TestLoadPoleTimezonesPartialFailure:
    def test_one_location_failing_does_not_block_the_others(
        self, patch_get_connection_pole_timezones, mocker, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [
            ("bad-location", None, None),
            ("12009-1000", -80.7236, 27.99507),
        ]
        mocker.patch(
            "shared.pole_timezones_loader.resolve_windows_timezone",
            side_effect=[
                RuntimeError("boom"),
                ("America/New_York", "Eastern Standard Time"),
            ],
        )

        pole_timezones_loader.load_pole_timezones()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)

    def test_logs_error_for_failed_location(
        self, patch_get_connection_pole_timezones, mocker, mock_cursor, caplog
    ):
        mock_cursor.fetchone.return_value = (1,)
        mock_cursor.fetchall.return_value = [("bad-location", None, None)]
        mocker.patch(
            "shared.pole_timezones_loader.resolve_windows_timezone",
            side_effect=RuntimeError("boom"),
        )

        with caplog.at_level("ERROR"):
            pole_timezones_loader.load_pole_timezones()

        assert any(
            "failed to resolve/store timezone for bad-location" in rec.message
            for rec in caplog.records
        )


class TestLoadPoleTimezonesTopLevelFailure:
    def test_sp_execution_insert_failure_reraises(
        self, patch_get_connection_pole_timezones, mock_conn, mock_cursor
    ):
        mock_cursor.execute.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            pole_timezones_loader.load_pole_timezones()

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
