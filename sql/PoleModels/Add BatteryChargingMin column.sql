-- Adds PoleModels.BatteryChargingMin -- UNLIKE every other column on this
-- table, this is NOT sourced from the Leadsun API at all. It's a fixed,
-- manually-set threshold (13.5, the same value for every model right
-- now) used by pole_vitals_loader.py's IsPanelFaultFlag to decide
-- whether a panel producing zero output is actually a fault:
--
--   A solar panel only needs to charge when BOTH (a) it's daylight, AND
--   (b) the battery actually needs it -- i.e. the average of
--   BatteryVoltage1/BatteryVoltage2 is below BatteryChargingMin. Once
--   the battery is already at or above that threshold, zero panel
--   output is expected, correct behavior (nothing left to charge), not
--   a fault, even during daylight.
--
-- Because this ISN'T an API-sourced field, pole_models_loader.py's own
-- _ALL_COLUMNS list deliberately does NOT include it -- that loader's
-- MERGE never touches this column at all, for either existing or newly
-- inserted models, so this value persists across every future
-- loadPoleModels run rather than getting resurrected to some
-- API-provided (nonexistent) value or reset to NULL.
--
-- NOT NULL DEFAULT 13.5, not NULL: SQL Server backfills every EXISTING
-- row with the default value as part of this same ALTER TABLE, and any
-- FUTURE model inserted by loadPoleModels (which never mentions this
-- column) automatically gets 13.5 too via that same default constraint
-- -- no separate UPDATE statement, and no loader code change, needed for
-- either case.
--
-- "For now" (per the person requesting this): a single, uniform value
-- across every model today, but modeled as its own per-model column
-- (not a hardcoded constant in pole_vitals_loader.py's own SQL) since
-- the expectation is this may become genuinely model-specific later --
-- changing a specific model's value at that point will just be a plain
-- UPDATE PoleModels SET BatteryChargingMin = ... WHERE ModelId = ...,
-- not a code change or redeploy.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleModels') AND name = 'BatteryChargingMin'
)
BEGIN
    ALTER TABLE PoleModels ADD BatteryChargingMin FLOAT NOT NULL CONSTRAINT DF_PoleModels_BatteryChargingMin DEFAULT 13.5;
END
