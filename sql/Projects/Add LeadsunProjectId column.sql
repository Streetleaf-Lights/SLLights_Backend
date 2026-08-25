-- Adds Projects.LeadsunProjectId -- sourced from Airtable's own "Leadsun
-- ProjectID" field, correlating with PoleTelemetry.LeadsunProjectId
-- (Leadsun's own numeric project identifier, confirmed INT in a real
-- /lamps response, e.g. 442, 314). Lets a Project be joined directly to
-- its own PoleTelemetry rows via this shared identifier, rather than
-- only indirectly through Poles.ProjectId -> Poles.LocationId ->
-- PoleTelemetry.LocationId.
--
-- INT, matching PoleTelemetry.LeadsunProjectId's own type -- if it ever
-- turns out Airtable's own value doesn't fit (e.g. arrives as a
-- non-numeric string), this column would need widening to a VARCHAR
-- instead; not expected given Leadsun's own side is confirmed numeric,
-- but worth keeping in mind since Airtable's own copy is a separate,
-- independently-entered value, not a live join to Leadsun's own system.
--
-- Run this AFTER deploying the updated shared/projects_loader.py (whose
-- _PROJECT_UPSERT_SQL now includes LeadsunProjectId) -- otherwise
-- loadProjects will fail every run the moment it tries to write to a
-- column that doesn't exist yet, the same class of failure this
-- project's own earlier column-addition migrations already document.
--
-- Safe to re-run -- the addition is guarded.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Projects') AND name = 'LeadsunProjectId'
)
BEGIN
    ALTER TABLE Projects ADD LeadsunProjectId INT NULL;
END

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('Projects') AND name = 'IX_Projects_LeadsunProjectId'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_Projects_LeadsunProjectId
        ON Projects (LeadsunProjectId);
END
