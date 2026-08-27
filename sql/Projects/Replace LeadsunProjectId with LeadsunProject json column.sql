-- Replaces Projects.LeadsunProjectId (a plain INT column) with
-- Projects.LeadsunProject (NVARCHAR(MAX), a JSON object) -- per explicit
-- request. If you already have a LeadsunProjectId INT column in this
-- database, this migration DROPS it and its own index entirely -- its
-- data is NOT migrated forward automatically (see below for why, and
-- what to do about it).
--
-- The new column always has at least a "ProjectId" key (Airtable's own
-- "Leadsun ProjectID" field, written by projects_loader.py, as a JSON
-- STRING even though the underlying value is numeric-looking);
-- ProjectName/UserName/groups/products are filled in separately, later,
-- by shared/pole_telemetry_loader.update_leadsun_project_details(),
-- aggregated fresh from PoleTelemetry after each loadPoleTelemetry run
-- -- see that function's own docstring for the full shape and
-- field-mapping reasoning (in particular: "ProductId" in the new JSON
-- is Leadsun's own raw "id"/LeadsunId, genuinely NOT the same
-- identifier as "ProvidedProductId", which is PoleTelemetry.ProductId/
-- Leadsun's own raw "productId" -- easy to confuse, deliberately
-- disambiguated with different key names).
--
-- On data loss: the OLD LeadsunProjectId INT column only ever held a
-- bare number (Airtable's own value) with no groups/products structure
-- at all -- there was no earlier migration or loader that ever
-- populated anything richer than that into it. Rather than write
-- one-off conversion logic to carry that bare number forward into the
-- new column's own "ProjectId" JSON key, this migration relies on the
-- NEXT loadProjects run to repopulate LeadsunProject with
-- {"ProjectId": ...} from Airtable directly (projects_loader.py's own
-- MERGE INSERTs a fresh {"ProjectId": ...} for a row with no existing
-- LeadsunProject value at all) -- functionally equivalent to what the
-- old column held, recovered automatically on the very next scheduled
-- Airtable sync, not lost permanently. Run loadPoleTelemetry (or
-- specifically update_leadsun_project_details()) again afterward to
-- rebuild groups/products fresh too.
--
-- Run this AFTER deploying the updated shared/projects_loader.py (whose
-- _PROJECT_UPSERT_SQL now references LeadsunProject, not
-- LeadsunProjectId) and shared/pole_telemetry_loader.py (which now
-- defines update_leadsun_project_details()) -- otherwise loadProjects
-- will fail every run the moment it tries to write to a column that
-- doesn't exist yet, the same class of failure this project's own
-- earlier column-addition migrations already document.
--
-- Safe to re-run.
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a column/index just
-- dropped by one ALTER isn't necessarily safe to reference again in a
-- statement compiled in the same batch.

IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('Projects') AND name = 'IX_Projects_LeadsunProjectId'
)
BEGIN
    DROP INDEX IX_Projects_LeadsunProjectId ON Projects;
END
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Projects') AND name = 'LeadsunProjectId'
)
BEGIN
    ALTER TABLE Projects DROP COLUMN LeadsunProjectId;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Projects') AND name = 'LeadsunProject'
)
BEGIN
    ALTER TABLE Projects ADD LeadsunProject NVARCHAR(MAX) NULL;
END
GO
