-- Replaces Projects.IsRemovedFromAirtable (a since-superseded column
-- name/migration) with Projects.Active -- per explicit request, so the
-- same column reads sensibly if a future source OTHER than Airtable
-- ever needs to reconcile against this same table. If you already ran
-- "Add IsRemovedFromAirtable column.sql" against this database, this
-- migration DROPS that column entirely -- its data is NOT migrated
-- forward automatically (see below for why, and what to do about it).
--
-- The SEMANTICS are also inverted, not just the name: IsRemovedFromAirtable
-- = 1 meant "no longer present"; Active = 1 means "present" -- the
-- opposite polarity. Set by shared/airtable_removal_utils.
-- flag_records_removed_from_airtable(), called once per run from
-- load_projects(), AFTER that run's own upsert phase has fully
-- completed -- see that function's own docstring for the full
-- reasoning. Entirely independent of that same MERGE's own
-- JSON_MODIFY()-based handling of LeadsunProject -- this column is set
-- by a completely separate UPDATE, never touching that column at all.
--
-- On data loss: rather than write one-off conversion logic to carry
-- the old column's own (inverted) values forward into the new column,
-- this migration relies on the NEXT loadProjects run to correctly
-- repopulate Active from a fresh Airtable fetch -- functionally
-- equivalent to what the old column held (just inverted), recovered
-- automatically on the very next scheduled sync, not lost permanently.
--
-- BIT NOT NULL DEFAULT 1: every existing row gets 1 (active) the
-- moment this column is added -- correct as a starting assumption,
-- since the very next loadProjects run will immediately re-evaluate
-- every row's own real status against a fresh Airtable fetch anyway.
-- Note this default is the OPPOSITE of IsRemovedFromAirtable's own
-- DEFAULT 0 -- not a typo, a direct consequence of the inverted
-- polarity.
--
-- Run this BEFORE deploying the updated shared/projects_loader.py --
-- otherwise loadProjects will fail every run the moment it tries to
-- write to a column that doesn't exist yet, the same class of failure
-- this project's own earlier column-addition migrations already
-- document.
--
-- Safe to re-run.

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Projects') AND name = 'IsRemovedFromAirtable'
)
BEGIN
    ALTER TABLE Projects DROP COLUMN IsRemovedFromAirtable;
END

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Projects') AND name = 'Active'
)
BEGIN
    ALTER TABLE Projects ADD Active BIT NOT NULL DEFAULT 1;
END
