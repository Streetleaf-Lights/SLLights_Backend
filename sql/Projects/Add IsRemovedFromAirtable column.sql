-- Adds Projects.IsRemovedFromAirtable -- per explicit request, flags a
-- Project whose own Id is no longer present in loadProjects' own
-- Airtable fetch, without actually deleting that row (deleting it
-- outright would break any FK-style references from Poles.ProjectId,
-- and would permanently lose whatever LeadsunProject/telemetry history
-- had already been aggregated for it -- flagging it in place preserves
-- both).
--
-- Set by shared/airtable_removal_utils.flag_records_removed_from_airtable(),
-- called once per run from load_projects(), AFTER that run's own
-- upsert phase has fully completed -- see that function's own docstring
-- for the full reasoning (in particular: why this can't just be a
-- WHEN NOT MATCHED BY SOURCE clause tacked onto Projects' own existing
-- MERGE, and the safety guard against an empty/failed Airtable fetch
-- mass-flagging every existing row). Entirely independent of that same
-- MERGE's own JSON_MODIFY()-based handling of LeadsunProject -- this
-- flag is set by a completely separate UPDATE, never touching that
-- column at all.
--
-- BIT NOT NULL DEFAULT 0: every existing row gets 0 (not removed) the
-- moment this column is added -- correct as a starting assumption, since
-- the very next loadProjects run will immediately re-evaluate every
-- row's own real status against a fresh Airtable fetch anyway.
--
-- Run this BEFORE deploying the updated shared/projects_loader.py --
-- otherwise loadProjects will fail every run the moment it tries to
-- write to a column that doesn't exist yet, the same class of failure
-- this project's own earlier column-addition migrations already
-- document.
--
-- Safe to re-run.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Projects') AND name = 'IsRemovedFromAirtable'
)
BEGIN
    ALTER TABLE Projects ADD IsRemovedFromAirtable BIT NOT NULL DEFAULT 0;
END
