"""Tests for shared/airtable_removal_utils.py"""

from unittest.mock import MagicMock

import pytest

from shared import airtable_removal_utils as m


class TestFlagRecordsRemovedFromAirtable:
    def test_empty_current_ids_does_nothing_and_returns_zero(self):
        """Critical safety guard: an empty current_ids (e.g. from a
        transient/failed Airtable fetch) must NOT mass-flag every
        existing row as removed."""
        cursor = MagicMock()
        result = m.flag_records_removed_from_airtable(cursor, "Poles", [])
        assert result == 0
        cursor.execute.assert_not_called()
        cursor.executemany.assert_not_called()

    def test_creates_a_table_specific_staging_table(self):
        cursor = MagicMock()
        m.flag_records_removed_from_airtable(cursor, "Customers", ["rec1"])

        create_sql = cursor.execute.call_args_list[0].args[0]
        assert "CREATE TABLE #CurrentAirtableIds_Customers" in create_sql
        assert "IF OBJECT_ID('tempdb..#CurrentAirtableIds_Customers')" in create_sql

    def test_bulk_inserts_every_current_id_into_the_staging_table(self):
        cursor = MagicMock()
        m.flag_records_removed_from_airtable(cursor, "Projects", ["rec1", "rec2", "rec3"])

        insert_sql, rows = cursor.executemany.call_args.args
        assert "INSERT INTO #CurrentAirtableIds_Projects" in insert_sql
        assert rows == [("rec1",), ("rec2",), ("rec3",)]

    def test_final_update_targets_the_given_table_and_column(self):
        cursor = MagicMock()
        m.flag_records_removed_from_airtable(cursor, "Poles", ["rec1"])

        update_sql = cursor.execute.call_args_list[1].args[0]
        assert "UPDATE t" in update_sql
        assert "SET t.Active" in update_sql
        assert "FROM Poles t" in update_sql
        assert "LEFT JOIN #CurrentAirtableIds_Poles c ON c.Id = t.Id" in update_sql

    def test_returns_cursor_rowcount(self):
        cursor = MagicMock()
        cursor.rowcount = 7
        result = m.flag_records_removed_from_airtable(cursor, "Customers", ["rec1"])
        assert result == 7

    def test_call_order_is_create_then_insert_then_update(self):
        """The staging table must exist before the bulk insert, and be
        fully populated before the UPDATE reads from it."""
        cursor = MagicMock()
        call_log = []
        cursor.execute.side_effect = lambda sql, *a: call_log.append(("execute", sql))
        cursor.executemany.side_effect = lambda sql, rows: call_log.append(("executemany", sql))

        m.flag_records_removed_from_airtable(cursor, "Customers", ["rec1"])

        assert len(call_log) == 3
        assert "CREATE TABLE" in call_log[0][1]
        assert call_log[1][0] == "executemany"
        assert "UPDATE t" in call_log[2][1]

    def test_different_tables_get_independently_named_staging_tables(self):
        """No cross-table collision risk, even within the same run (e.g.
        if a future caller processed multiple tables on one connection
        without closing it between calls)."""
        cursor = MagicMock()
        m.flag_records_removed_from_airtable(cursor, "Customers", ["rec1"])
        m.flag_records_removed_from_airtable(cursor, "Projects", ["rec1"])

        create_sqls = [c.args[0] for c in cursor.execute.call_args_list if "CREATE TABLE" in c.args[0]]
        assert "#CurrentAirtableIds_Customers" in create_sqls[0]
        assert "#CurrentAirtableIds_Projects" in create_sqls[1]

    def test_single_current_id_still_works(self):
        cursor = MagicMock()
        cursor.rowcount = 1
        result = m.flag_records_removed_from_airtable(cursor, "Poles", ["rec1"])
        assert result == 1
        _, rows = cursor.executemany.call_args.args
        assert rows == [("rec1",)]

    def test_large_id_list_all_bulk_inserted_in_one_executemany_call(self):
        """Confirms this doesn't fall back to one INSERT per id even at
        real-world Poles-table scale -- a single executemany() call,
        not thousands of individual execute() calls."""
        cursor = MagicMock()
        current_ids = [f"rec{i}" for i in range(14000)]

        m.flag_records_removed_from_airtable(cursor, "Poles", current_ids)

        assert cursor.executemany.call_count == 1
        _, rows = cursor.executemany.call_args.args
        assert len(rows) == 14000
        assert rows[0] == ("rec0",)
        assert rows[-1] == ("rec13999",)


class TestFastExecutemanyIsolation:
    """
    Regression guard for a real production incident: poles_loader.py's
    own load_poles() sets cursor.fast_executemany = True near the top of
    that function, for its OWN, separate bulk insert into #PolesStaging
    -- a cursor-level setting, not scoped to any one statement or table.
    That same cursor object is what gets passed into
    flag_records_removed_from_airtable(). Leaving fast_executemany at
    True for THIS function's own executemany() call (into a DIFFERENT,
    freshly-created temp table, #CurrentAirtableIds_Poles) surfaced in
    production as "Result set index cannot be less than 0 or greater
    than the number of result sets (Parameter 'resultSetIndex')" during
    a real loadPoles run, once this function started actually executing
    at Poles' own ~14,000-id scale.
    """

    def test_fast_executemany_is_off_during_this_functions_own_bulk_insert(self):
        cursor = MagicMock()
        cursor.fast_executemany = True  # simulates poles_loader.py's own cursor state

        observed_during_call = []
        def capture(sql, rows):
            observed_during_call.append(cursor.fast_executemany)
        cursor.executemany.side_effect = capture

        m.flag_records_removed_from_airtable(cursor, "Poles", ["rec1"])

        assert observed_during_call == [False]

    def test_callers_original_fast_executemany_value_is_restored_after(self):
        cursor = MagicMock()
        cursor.fast_executemany = True

        m.flag_records_removed_from_airtable(cursor, "Poles", ["rec1"])

        assert cursor.fast_executemany is True

    def test_restored_even_when_caller_never_set_it_at_all(self):
        """A plain MagicMock auto-creates any attribute accessed on it,
        so this doesn't prove much on its own for a mock -- but confirms
        the getattr(..., False) default path doesn't itself raise or
        misbehave for a cursor that never had fast_executemany touched,
        matching customers_loader.py/projects_loader.py, neither of
        which ever sets it."""
        cursor = MagicMock(spec=["execute", "executemany", "rowcount"])

        m.flag_records_removed_from_airtable(cursor, "Customers", ["rec1"])  # must not raise

        assert cursor.fast_executemany is False

    def test_restored_even_if_the_bulk_insert_itself_raises(self):
        """The restore must happen even on failure, via the function's
        own try/finally -- not just on the success path -- so a
        genuinely failed run doesn't ALSO leave the caller's cursor in
        the wrong fast_executemany state for whatever it does next
        (e.g. the caller's own exception-handling path, which may issue
        further queries on this same cursor/connection)."""
        cursor = MagicMock()
        cursor.fast_executemany = True
        cursor.executemany.side_effect = RuntimeError("insert failed")

        with pytest.raises(RuntimeError, match="insert failed"):
            m.flag_records_removed_from_airtable(cursor, "Poles", ["rec1"])

        assert cursor.fast_executemany is True
