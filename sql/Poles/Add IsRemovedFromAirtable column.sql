-- Adds Poles.IsRemovedFromAirtable -- per explicit request, flags a Pole
-- whose own Id is no longer present in loadPoles' own Airtable fetch,
-- without actually deleting that row (deleting it outright would break
-- PoleTelemetry/PoleVitals/PoleTimeZones' own references to that same
-- LocationId, and would permanently lose that pole's own telemetry
-- history -- flagging it in place preserves both).
--
-- Set by shared/airtable_removal_utils.flag_records_removed_from_airtable(),
-- called once per run from load_poles(), AFTER that run's own upsert
-- phase (including its own chunked staging-table MERGE and any
-- row-by-row fallback) has fully completed -- see that function's own
-- docstring for the full reasoning, in particular why this can't be
-- expressed as a WHEN NOT MATCHED BY SOURCE clause on either
-- _MERGE_FROM_STAGING_SQL or _POLE_UPSERT_SQL: neither one's own MERGE
-- ever has visibility into the COMPLETE current Airtable fetch at once
-- (each staging chunk only ever holds one _UPSERT_BATCH_SIZE-sized
-- slice, not all ~14,000 poles together), so evaluating "not matched by
-- source" against just one chunk would incorrectly flag every pole
-- outside that specific chunk as removed. Also covers the safety guard
-- against an empty/failed Airtable fetch mass-flagging every existing
-- row -- see that same function's own docstring.
--
-- BIT NOT NULL DEFAULT 0: every existing row gets 0 (not removed) the
-- moment this column is added -- correct as a starting assumption, since
-- the very next loadPoles run will immediately re-evaluate every row's
-- own real status against a fresh Airtable fetch anyway.
--
-- Run this BEFORE deploying the updated shared/poles_loader.py --
-- otherwise loadPoles will fail every run the moment it tries to write
-- to a column that doesn't exist yet, the same class of failure this
-- project's own earlier column-addition migrations already document.
--
-- Safe to re-run.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Poles') AND name = 'IsRemovedFromAirtable'
)
BEGIN
    ALTER TABLE Poles ADD IsRemovedFromAirtable BIT NOT NULL DEFAULT 0;
END
