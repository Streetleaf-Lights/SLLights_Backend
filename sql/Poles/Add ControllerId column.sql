-- Adds Poles.ControllerId -- sourced from Airtable's "Controller ID"
-- field, confirmed to match up with PoleTelemetry.ProductId (Leadsun's
-- own name for the exact same underlying identifier -- two systems'
-- own naming for one shared concept, not two different concepts). See
-- shared/poles_loader.py's own comments on AIRTABLE_POLES_FIELDS and
-- _map_record_to_pole()'s own ControllerId mapping for the full
-- reasoning, including why this stays a separate, independently-sourced
-- column here rather than being merged with PoleTelemetry.ProductId
-- into one shared column, or renamed to "ProductId" to match Leadsun's
-- own term -- every other column in this table is likewise named after
-- whatever label AIRTABLE ITSELF uses for it, not a cross-system alias,
-- and this stays consistent with that.
--
-- NVARCHAR(50), matching PoleTelemetry.ProductId's own type/width,
-- since this column is explicitly meant to hold values that correspond
-- to (and should be comparable against) that one.
--
-- Run this AFTER deploying the updated shared/poles_loader.py (whose
-- AIRTABLE_POLES_FIELDS/_POLE_UPSERT_SQL/staging-table SQL now all
-- include ControllerId) -- otherwise loadPoles will fail every run the
-- moment it tries to write to a column that doesn't exist yet, the same
-- class of failure this project's own earlier column-addition
-- migrations (e.g. "Add CountyFips column.sql") already document.
--
-- Safe to re-run -- the addition is guarded.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Poles') AND name = 'ControllerId'
)
BEGIN
    ALTER TABLE Poles ADD ControllerId NVARCHAR(50) NULL;
END
