"""Tests for shared/provisioned_data_loader.py"""

import json

import pytest

from shared import provisioned_data_loader as m


class TestMapProductRow:
    def test_maps_product_id_to_model_id_with_offset(self):
        result = m._map_product_row((5, "PFX", "Model Five", True))
        assert result["ModelId"] == 1005

    def test_maps_name_to_model_name(self):
        result = m._map_product_row((5, "PFX", "Model Five", True))
        assert result["ModelName"] == "Model Five"

    def test_sunboard_power_is_fixed_at_250(self):
        result = m._map_product_row((5, "PFX", "Model Five", True))
        assert result["SunboardPower"] == 250

    def test_light_power_is_fixed_at_40(self):
        result = m._map_product_row((5, "PFX", "Model Five", True))
        assert result["LightPower"] == 40

    def test_prefix_and_is_active_captured_in_extra_fields_json(self):
        result = m._map_product_row((5, "PFX", "Model Five", True))
        extra = json.loads(result["ExtraFieldsJson"])
        assert extra == {"Prefix": "PFX", "IsActive": True}

    def test_is_active_false_captured_correctly(self):
        result = m._map_product_row((7, "OTHER", "Model Seven", False))
        extra = json.loads(result["ExtraFieldsJson"])
        assert extra["IsActive"] is False

    def test_model_id_offset_keeps_ids_disjoint_from_low_leadsun_ids(self):
        """The whole reason for the +1000 offset, per explicit request:
        product_id=1 must not collide with a Leadsun ModelId of 1."""
        result = m._map_product_row((1, "PFX", "First Product", True))
        assert result["ModelId"] == 1001
        assert result["ModelId"] != 1


class TestBuildRow:
    def test_uses_provisioned_source_not_leadsun(self):
        """Regression guard: _build_row() here must NOT reuse
        pole_models_loader._build_row() directly, since that closes over
        the Leadsun module's own SOURCE_NAME."""
        mapped = m._map_product_row((5, "PFX", "Model Five", True))
        row = m._build_row(mapped, sp_exec_id=99)
        source_index = m._ALL_COLUMNS.index("Source")
        assert row[source_index] == "Provisioned"

    def test_sp_exec_id_included(self):
        mapped = m._map_product_row((5, "PFX", "Model Five", True))
        row = m._build_row(mapped, sp_exec_id=99)
        sp_exec_index = m._ALL_COLUMNS.index("SP_ExecId")
        assert row[sp_exec_index] == 99

    def test_row_length_matches_all_columns(self):
        mapped = m._map_product_row((5, "PFX", "Model Five", True))
        row = m._build_row(mapped, sp_exec_id=99)
        assert len(row) == len(m._ALL_COLUMNS)

    def test_unpopulated_columns_are_none(self):
        """Every PoleModels column this source doesn't populate (Battery,
        CommType, IconUrl, etc.) must come through as None, not be
        missing or raise a KeyError."""
        mapped = m._map_product_row((5, "PFX", "Model Five", True))
        row = m._build_row(mapped, sp_exec_id=99)
        battery_index = m._ALL_COLUMNS.index("Battery")
        assert row[battery_index] is None


class TestFetchProvisionedProducts:
    def test_queries_the_products_table(self, patch_get_provisioned_connection):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = [(1, "PFX", "Model One", True)]

        result = m._fetch_provisioned_products()

        provisioned_cursor.execute.assert_called_once_with(m._FETCH_PRODUCTS_SQL)
        assert result == [(1, "PFX", "Model One", True)]

    def test_closes_cursor_and_connection(self, patch_get_provisioned_connection):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = []

        m._fetch_provisioned_products()

        provisioned_cursor.close.assert_called_once()
        provisioned_conn.close.assert_called_once()

    def test_closes_connection_even_on_failure(self, patch_get_provisioned_connection):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.execute.side_effect = RuntimeError("connection refused")

        with pytest.raises(RuntimeError):
            m._fetch_provisioned_products()

        provisioned_cursor.close.assert_called_once()
        provisioned_conn.close.assert_called_once()


class TestLoadProvisionedPoleModels:
    def test_full_success_flow(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = [
            (1, "PFX", "Model One", True),
            (2, "PFX", "Model Two", False),
        ]
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_models()

        # SP_Execution start row uses the Provisioned source name
        insert_call = mock_cursor.execute.call_args_list[0]
        assert insert_call.args[1] == "loadProvisionedPoleModels"
        assert insert_call.args[4] == "Provisioned"

        # Provisioned DB was queried, then closed before our own DB's
        # write phase reopened -- confirmed indirectly via the separate
        # mock connections both being used and both being closed.
        provisioned_cursor.execute.assert_called_once_with(m._FETCH_PRODUCTS_SQL)
        provisioned_conn.close.assert_called_once()

        # Final SP_Execution update reflects 2 successful upserts
        final_update = mock_cursor.execute.call_args_list[-1]
        assert final_update.args[0].strip().startswith("UPDATE SP_Execution")
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (2, 0)

    def test_no_products_still_completes_successfully(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_models()  # must not raise

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 0)

    def test_provisioned_db_failure_is_recorded_and_reraised(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.execute.side_effect = RuntimeError("provisioned db unreachable")
        mock_cursor.fetchone.return_value = (42,)

        with pytest.raises(RuntimeError, match="provisioned db unreachable"):
            m.load_provisioned_pole_models()

        # The error-recording UPDATE reused/reopened our own db connection
        # even though the failure happened while working with the OTHER
        # (provisioned) database.
        error_update = mock_cursor.execute.call_args_list[-1]
        assert "ErrorMessage" in error_update.args[0]
        assert "provisioned db unreachable" in error_update.args[2]

    def test_row_level_failure_falls_back_and_is_counted(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = [
            (1, "PFX", "Model One", True),
            (2, "PFX", "Model Two", False),
        ]
        mock_cursor.fetchone.return_value = (42,)
        # First executemany (bulk staging insert) fails -> falls back to
        # row-by-row -- one of the two individual upserts also fails.
        mock_cursor.executemany.side_effect = RuntimeError("bulk merge failed")
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            None,  # staging table create
            None,  # truncate staging (after bulk failure)
            None,  # row 1 upsert succeeds
            RuntimeError("row 2 failed"),  # row 2 upsert fails
            None,  # final SP_Execution update
        ]

        m.load_provisioned_pole_models()  # must not raise -- row failures are isolated

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 1)

    def test_connection_lifecycle_our_db_opened_twice(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        """Short-lived connection for the start row, closed, then a
        fresh one reopened for the write phase -- same fetch-then-write
        discipline as every other loader here."""
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_models()

        get_connection_mock = patch_get_connection_provisioned_data
        assert get_connection_mock.call_count == 2


class TestMapSerialRow:
    def test_maps_device_uid_to_provisioned_pole_id(self):
        result = m._map_serial_row(("UID-1", "SN-1", 5, "2026-01-01"))
        assert result["ProvisionedPoleId"] == "UID-1"

    def test_maps_serial_number_for_the_join_key(self):
        result = m._map_serial_row(("UID-1", "SN-1", 5, "2026-01-01"))
        assert result["SerialNumber"] == "SN-1"

    def test_maps_product_id_to_pole_model_id_with_same_offset(self):
        """Must match the exact same +1000 offset _map_product_row() uses,
        so a serial's own PoleModelId lands on the same PoleModels row
        load_provisioned_pole_models() wrote for that product_id."""
        result = m._map_serial_row(("UID-1", "SN-1", 5, "2026-01-01"))
        assert result["PoleModelId"] == 1005
        assert result["PoleModelId"] == m._map_product_row((5, "PFX", "Name", True))["ModelId"]

    def test_maps_created_at_verbatim_no_conversion(self):
        result = m._map_serial_row(("UID-1", "SN-1", 5, "2026-01-01 12:00:00"))
        assert result["ProvisionedPoleCreatedDateTime"] == "2026-01-01 12:00:00"

    def test_station_id_is_not_in_this_source_tuple_at_all(self):
        """station_id is deliberately never even read -- confirms the
        4-tuple shape (device_uid, serial_number, product_id, created_at)
        matches _FETCH_SERIALS_SQL's own SELECT column list exactly."""
        result = m._map_serial_row(("UID-1", "SN-1", 5, "2026-01-01"))
        assert set(result.keys()) == {
            "SerialNumber", "ProvisionedPoleId", "PoleModelId", "ProvisionedPoleCreatedDateTime",
        }


class TestDedupeSerialsBySerialNumber:
    def test_no_duplicates_returns_all_rows(self):
        rows = [
            {"SerialNumber": "A", "ProvisionedPoleCreatedDateTime": "2026-01-01"},
            {"SerialNumber": "B", "ProvisionedPoleCreatedDateTime": "2026-01-02"},
        ]
        result = m._dedupe_serials_by_serial_number(rows)
        assert len(result) == 2

    def test_duplicate_keeps_the_one_with_latest_created_at(self):
        older = {"SerialNumber": "A", "ProvisionedPoleId": "OLD", "ProvisionedPoleCreatedDateTime": "2026-01-01"}
        newer = {"SerialNumber": "A", "ProvisionedPoleId": "NEW", "ProvisionedPoleCreatedDateTime": "2026-06-01"}

        result = m._dedupe_serials_by_serial_number([older, newer])

        assert len(result) == 1
        assert result[0]["ProvisionedPoleId"] == "NEW"

    def test_duplicate_order_reversed_still_keeps_latest_created_at(self):
        """Confirms it's genuinely comparing timestamps, not just
        'whichever came last in the input list'."""
        newer = {"SerialNumber": "A", "ProvisionedPoleId": "NEW", "ProvisionedPoleCreatedDateTime": "2026-06-01"}
        older = {"SerialNumber": "A", "ProvisionedPoleId": "OLD", "ProvisionedPoleCreatedDateTime": "2026-01-01"}

        result = m._dedupe_serials_by_serial_number([newer, older])

        assert len(result) == 1
        assert result[0]["ProvisionedPoleId"] == "NEW"

    def test_null_created_at_never_beats_a_real_timestamp(self):
        with_date = {"SerialNumber": "A", "ProvisionedPoleId": "HAS_DATE", "ProvisionedPoleCreatedDateTime": "2026-01-01"}
        without_date = {"SerialNumber": "A", "ProvisionedPoleId": "NO_DATE", "ProvisionedPoleCreatedDateTime": None}

        result = m._dedupe_serials_by_serial_number([with_date, without_date])

        assert result[0]["ProvisionedPoleId"] == "HAS_DATE"

    def test_empty_list_returns_empty_list(self):
        assert m._dedupe_serials_by_serial_number([]) == []


class TestFetchProvisionedSerials:
    def test_queries_the_serials_table(self, patch_get_provisioned_connection):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = [("UID-1", "SN-1", 5, "2026-01-01")]

        result = m._fetch_provisioned_serials()

        provisioned_cursor.execute.assert_called_once_with(m._FETCH_SERIALS_SQL)
        assert result == [("UID-1", "SN-1", 5, "2026-01-01")]

    def test_closes_cursor_and_connection(self, patch_get_provisioned_connection):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = []

        m._fetch_provisioned_serials()

        provisioned_cursor.close.assert_called_once()
        provisioned_conn.close.assert_called_once()


class TestLoadProvisionedPoleSerials:
    def test_full_success_flow(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_conn, provisioned_cursor = patch_get_provisioned_connection
        provisioned_cursor.fetchall.return_value = [
            ("UID-1", "SN-1", 5, "2026-01-01"),
            ("UID-2", "SN-2", 6, "2026-01-02"),
        ]
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_serials()

        insert_call = mock_cursor.execute.call_args_list[0]
        assert insert_call.args[1] == "loadProvisionedPoleSerials"
        assert insert_call.args[4] == "Provisioned"

        provisioned_cursor.execute.assert_called_once_with(m._FETCH_SERIALS_SQL)
        provisioned_conn.close.assert_called_once()

        final_update = mock_cursor.execute.call_args_list[-1]
        assert final_update.args[0].strip().startswith("UPDATE SP_Execution")
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (2, 0)

    def test_this_is_an_update_only_no_insert_into_poles(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.fetchall.return_value = [("UID-1", "SN-1", 5, "2026-01-01")]
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_serials()

        executed_sql_statements = [c.args[0] for c in mock_cursor.execute.call_args_list]
        assert not any("INSERT INTO Poles" in sql for sql in executed_sql_statements)
        assert any("UPDATE p" in sql and "FROM Poles p" in sql for sql in executed_sql_statements)

    def test_no_serials_still_completes_successfully(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_serials()  # must not raise

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (0, 0)

    def test_duplicate_serial_numbers_are_deduped_before_staging(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.fetchall.return_value = [
            ("UID-OLD", "SN-1", 5, "2026-01-01"),
            ("UID-NEW", "SN-1", 5, "2026-06-01"),  # same serial_number, newer
        ]
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_serials()

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 0)  # deduped down to 1, not 2

    def test_provisioned_db_failure_is_recorded_and_reraised(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.execute.side_effect = RuntimeError("provisioned db unreachable")
        mock_cursor.fetchone.return_value = (42,)

        with pytest.raises(RuntimeError, match="provisioned db unreachable"):
            m.load_provisioned_pole_serials()

        error_update = mock_cursor.execute.call_args_list[-1]
        assert "ErrorMessage" in error_update.args[0]
        assert "provisioned db unreachable" in error_update.args[2]

    def test_row_level_failure_falls_back_and_is_counted(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.fetchall.return_value = [
            ("UID-1", "SN-1", 5, "2026-01-01"),
            ("UID-2", "SN-2", 6, "2026-01-02"),
        ]
        mock_cursor.fetchone.return_value = (42,)
        mock_cursor.executemany.side_effect = RuntimeError("bulk update failed")
        mock_cursor.execute.side_effect = [
            None,  # SP_Execution insert
            None,  # staging table create
            None,  # truncate staging (after bulk failure)
            None,  # row 1 update succeeds
            RuntimeError("row 2 failed"),  # row 2 update fails
            None,  # final SP_Execution update
        ]

        m.load_provisioned_pole_serials()  # must not raise

        final_update = mock_cursor.execute.call_args_list[-1]
        success, errors = final_update.args[2], final_update.args[3]
        assert (success, errors) == (1, 1)

    def test_connection_lifecycle_our_db_opened_twice(
        self,
        patch_get_connection_provisioned_data,
        patch_get_provisioned_connection,
        mock_conn,
        mock_cursor,
    ):
        provisioned_cursor = patch_get_provisioned_connection[1]
        provisioned_cursor.fetchall.return_value = []
        mock_cursor.fetchone.return_value = (42,)

        m.load_provisioned_pole_serials()

        assert patch_get_connection_provisioned_data.call_count == 2
