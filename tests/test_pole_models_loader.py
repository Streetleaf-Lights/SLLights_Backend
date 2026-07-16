"""Tests for shared/pole_models_loader.py"""

import json
import re

import pytest

from shared import pole_models_loader

DTO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}$")


# --------------------------------------------------------------------------
# _capitalize_key
# --------------------------------------------------------------------------


class TestCapitalizeKey:
    def test_camel_case_becomes_pascal_case(self):
        assert pole_models_loader._capitalize_key("modelName") == "ModelName"
        assert pole_models_loader._capitalize_key("sunboardPower") == "SunboardPower"

    def test_does_not_lowercase_the_rest_of_the_string(self):
        assert pole_models_loader._capitalize_key("modelName") != "modelName".capitalize()

    def test_empty_string_is_unchanged(self):
        assert pole_models_loader._capitalize_key("") == ""


# --------------------------------------------------------------------------
# _parse_numeric_string
# --------------------------------------------------------------------------


class TestParseNumericString:
    def test_none_returns_none(self):
        assert pole_models_loader._parse_numeric_string(None) is None

    def test_empty_string_returns_none(self):
        assert pole_models_loader._parse_numeric_string("") is None

    def test_whole_number_string_becomes_int(self):
        result = pole_models_loader._parse_numeric_string("80")
        assert result == 80
        assert isinstance(result, int)

    def test_decimal_string_becomes_float(self):
        result = pole_models_loader._parse_numeric_string("12.8")
        assert result == 12.8
        assert isinstance(result, float)

    def test_non_numeric_string_passes_through_unchanged(self):
        assert pole_models_loader._parse_numeric_string("Lora") == "Lora"

    def test_non_string_value_passes_through_unchanged(self):
        assert pole_models_loader._parse_numeric_string(82) == 82
        assert pole_models_loader._parse_numeric_string(False) is False


# --------------------------------------------------------------------------
# _map_model_record -- against the real confirmed Leadsun /models response
# --------------------------------------------------------------------------


class TestMapModelRecord:
    def test_model_id_stays_native_int_no_conversion_needed(self, make_model_record):
        record = make_model_record(model_id=82)
        result = pole_models_loader._map_model_record(record)
        assert result["ModelId"] == 82
        assert isinstance(result["ModelId"], int)

    def test_numeric_string_fields_are_converted(self, make_model_record):
        record = make_model_record()
        result = pole_models_loader._map_model_record(record)
        assert result["SunboardPower"] == 80
        assert isinstance(result["SunboardPower"], int)
        assert result["SystemVoltage"] == 12.8
        assert isinstance(result["SystemVoltage"], float)
        assert result["BatteryCapacity1"] == 230
        assert result["BatteryCapacity2"] == 230
        assert result["SolarBoardVoltage"] == 18

    def test_null_numeric_field_becomes_none(self, make_model_record):
        record = make_model_record()  # batteryVoltage is None in the sample
        result = pole_models_loader._map_model_record(record)
        assert result["BatteryVoltage"] is None

    def test_lamps_using_stays_a_string_not_converted_to_int(self, make_model_record):
        """
        Deliberate exception to the numeric-string-conversion rule:
        lampsUsing ("00000001") reads as a bitmask, where leading zeros
        are meaningful -- converting it to int would silently lose them.
        """
        record = make_model_record()
        result = pole_models_loader._map_model_record(record)
        assert result["LampsUsing"] == "00000001"
        assert isinstance(result["LampsUsing"], str)

    def test_empty_string_field_stays_empty_string_not_none(self, make_model_record):
        """lightDisType is "" in the real sample -- not a numeric field, so
        it's left as an empty string rather than converted to None."""
        record = make_model_record()
        result = pole_models_loader._map_model_record(record)
        assert result["LightDisType"] == ""

    def test_null_string_fields_stay_none(self, make_model_record):
        record = make_model_record()
        result = pole_models_loader._map_model_record(record)
        assert result["IconUrl"] is None
        assert result["ModelSeries"] is None

    def test_boolean_fields_pass_through_natively(self, make_model_record):
        record = make_model_record()
        result = pole_models_loader._map_model_record(record)
        assert result["IsAc"] is False
        assert result["IsDcOut"] is False

    def test_all_known_fields_from_real_sample_produce_empty_extra_json(self, make_model_record):
        record = make_model_record()
        result = pole_models_loader._map_model_record(record)
        assert result["ExtraFieldsJson"] is None

    def test_unexpected_field_is_captured_in_extra_fields_json(self, make_model_record):
        record = make_model_record(extra_fields={"brandNewSpecField": 42})
        result = pole_models_loader._map_model_record(record)
        extra = json.loads(result["ExtraFieldsJson"])
        assert extra["BrandNewSpecField"] == 42

    def test_missing_model_id_becomes_none(self):
        record = {"modelName": "Some Model"}
        result = pole_models_loader._map_model_record(record)
        assert result["ModelId"] is None


class TestBuildRow:
    def test_row_length_matches_all_columns(self, make_model_record):
        mapped = pole_models_loader._map_model_record(make_model_record())
        row = pole_models_loader._build_row(mapped, sp_exec_id=42)
        assert len(row) == len(pole_models_loader._ALL_COLUMNS)

    def test_row_order_matches_all_columns(self, make_model_record):
        mapped = pole_models_loader._map_model_record(make_model_record(model_id=99))
        row = pole_models_loader._build_row(mapped, sp_exec_id=7)

        as_dict = dict(zip(pole_models_loader._ALL_COLUMNS, row))
        assert as_dict["ModelId"] == 99
        assert as_dict["Source"] == "Leadsun"
        assert as_dict["SP_ExecId"] == 7


# --------------------------------------------------------------------------
# Staging / MERGE SQL structural checks
# --------------------------------------------------------------------------


class TestStagingMergeSqlStructure:
    def test_staging_table_ddl_has_guard_and_matches_all_columns(self):
        sql = pole_models_loader._STAGING_TABLE_SQL
        assert "IF OBJECT_ID('tempdb..#PoleModelsStaging')" in sql
        match = re.search(r"CREATE TABLE #PoleModelsStaging \((.+)\);", sql, re.DOTALL)
        cols = [line.strip().split()[0] for line in match.group(1).strip().split(",")]
        assert cols == pole_models_loader._ALL_COLUMNS

    def test_staging_insert_placeholder_count_matches_all_columns(self):
        sql = pole_models_loader._STAGING_INSERT_SQL
        assert sql.count("?") == len(pole_models_loader._ALL_COLUMNS)

    def test_merge_from_staging_match_key_is_model_id(self):
        sql = pole_models_loader._MERGE_FROM_STAGING_SQL
        assert "ON target.ModelId = source.ModelId" in sql

    def test_merge_from_staging_uses_intersect(self):
        assert "INTERSECT" in pole_models_loader._MERGE_FROM_STAGING_SQL

    def test_merge_from_staging_casts_extra_fields_json_to_avoid_ntext_bug(self):
        sql = pole_models_loader._MERGE_FROM_STAGING_SQL
        assert "CAST(target.ExtraFieldsJson AS NVARCHAR(MAX))" in sql
        assert "CAST(source.ExtraFieldsJson AS NVARCHAR(MAX))" in sql

    def test_merge_from_staging_insert_covers_all_columns(self):
        sql = pole_models_loader._MERGE_FROM_STAGING_SQL
        insert_cols = re.search(r"INSERT \(([^)]+)\)", sql).group(1)
        cols = {c.strip() for c in insert_cols.split(",")}
        assert cols == set(pole_models_loader._ALL_COLUMNS)

    def test_row_upsert_placeholder_count_matches_all_columns(self):
        sql = pole_models_loader._ROW_UPSERT_SQL
        assert sql.count("?") == len(pole_models_loader._ALL_COLUMNS)

    def test_sp_exec_id_excluded_from_diff_check_but_present_in_update(self):
        assert "SP_ExecId" not in pole_models_loader._DIFF_CHECK_COLUMNS
        assert "SP_ExecId" in pole_models_loader._NON_KEY_COLUMNS

    def test_no_fk_references(self):
        assert "REFERENCES" not in pole_models_loader._MERGE_FROM_STAGING_SQL


# --------------------------------------------------------------------------
# load_pole_models() -- full flow
# --------------------------------------------------------------------------


class TestLoadPoleModelsSuccessFlow:
    def test_full_success_flow_two_records(
        self,
        patch_get_connection_pole_models,
        patch_fetch_models,
        mock_conn,
        mock_cursor,
        make_model_record,
    ):
        mock_cursor.fetchone.return_value = (5,)
        record1 = make_model_record(model_id=82)
        record2 = make_model_record(model_id=83)
        patch_fetch_models.return_value = [record1, record2]

        pole_models_loader.load_pole_models()

        calls = mock_cursor.execute.call_args_list
        # insert SP_Execution, staging create, merge-from-staging, truncate,
        # final update
        assert len(calls) == 5

        insert_sql, name, env, start_time, source = calls[0].args
        assert "INSERT INTO SP_Execution" in insert_sql
        assert (name, env, source) == ("loadPoleModels", "Dev", "Leadsun")
        assert DTO_PATTERN.match(start_time)

        assert "CREATE TABLE #PoleModelsStaging" in calls[1].args[0]
        assert "MERGE PoleModels" in calls[2].args[0]
        assert calls[3].args[0] == "TRUNCATE TABLE #PoleModelsStaging"

        assert mock_cursor.executemany.call_count == 1
        staging_sql, batch = mock_cursor.executemany.call_args.args
        assert "INSERT INTO #PoleModelsStaging" in staging_sql
        assert len(batch) == 2
        assert batch[0][0] == 82
        assert batch[0][2] == 5  # SP_ExecId position
        assert batch[1][0] == 83

        update_sql, end_time, success, errors, batch_count, sp_exec_id = calls[4].args
        assert "UPDATE SP_Execution" in update_sql
        assert (success, errors, batch_count, sp_exec_id) == (2, 0, 1, 5)
        assert DTO_PATTERN.match(end_time)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_empty_model_list_still_closes_out_execution_row(
        self, patch_get_connection_pole_models, patch_fetch_models, mock_cursor
    ):
        patch_fetch_models.return_value = []

        pole_models_loader.load_pole_models()

        calls = mock_cursor.execute.call_args_list
        assert len(calls) == 2  # insert + final update -- no staging table needed
        _, _end_time, success, errors, batch_count, _sp_exec_id = calls[1].args
        assert (success, errors, batch_count) == (0, 0, 1)
        mock_cursor.executemany.assert_not_called()

    def test_records_missing_model_id_are_counted_as_errors(
        self, patch_get_connection_pole_models, patch_fetch_models, mock_cursor, make_model_record
    ):
        good_record = make_model_record(model_id=82)
        bad_record = {"modelName": "Broken"}  # missing modelId
        patch_fetch_models.return_value = [good_record, bad_record]

        pole_models_loader.load_pole_models()

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)


class TestLoadPoleModelsPartialFailure:
    def test_chunk_failure_falls_back_to_row_by_row(
        self,
        patch_get_connection_pole_models,
        patch_fetch_models,
        mock_cursor,
        make_model_record,
    ):
        patch_fetch_models.return_value = [
            make_model_record(model_id=82),
            make_model_record(model_id=83),
        ]
        mock_cursor.executemany.side_effect = RuntimeError("chunk failed")
        # insert, staging create, truncate-after-failure, row1, row2 (fails),
        # final update
        mock_cursor.execute.side_effect = [
            None, None, None, None, RuntimeError("bad row"), None,
        ]

        pole_models_loader.load_pole_models()  # must not raise

        final_update_args = mock_cursor.execute.call_args_list[-1].args
        success, errors = final_update_args[2], final_update_args[3]
        assert (success, errors) == (1, 1)


class TestLoadPoleModelsTopLevelFailure:
    def test_fetch_failure_updates_error_message_and_reraises(
        self, patch_get_connection_pole_models, patch_fetch_models, mock_conn, mock_cursor
    ):
        mock_cursor.fetchone.return_value = (5,)
        patch_fetch_models.side_effect = RuntimeError("leadsun api is down")

        with pytest.raises(RuntimeError, match="leadsun api is down"):
            pole_models_loader.load_pole_models()

        error_update_calls = [
            call for call in mock_cursor.execute.call_args_list if "ErrorMessage" in call.args[0]
        ]
        assert len(error_update_calls) == 1
        _, _end_time, err_msg, _success, _errors, sp_exec_id = error_update_calls[0].args
        assert err_msg == "leadsun api is down"
        assert sp_exec_id == 5

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
