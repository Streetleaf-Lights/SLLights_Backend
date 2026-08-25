-- Drops PoleModels.BatteryChargingMin entirely -- reverses "Add
-- BatteryChargingMin column.sql". The check IsPanelFaultFlag used this
-- for (average BatteryVoltage1/BatteryVoltage2 against this per-model
-- threshold) has been replaced by a different check entirely (the total
-- of BatteryElecCurrent1 + BatteryElecCurrent2 equal to exactly 200 --
-- see pole_vitals_loader.py's own comments on IsPanelFaultFlag for the
-- full reasoning), which needs no PoleModels involvement at all. Per
-- explicit request, this column "is not meant to be in" the project any
-- longer, not just unused by this one check -- a genuine removal, not a
-- deprecation left in place.
--
-- Run this AFTER deploying the updated pole_vitals_loader.py/
-- pole_vitals_api.py (which no longer reference this column at all) --
-- running it before would only matter if something else still read
-- BatteryChargingMin, which nothing in this project does anymore once
-- those files are deployed; running it after is simply the safer,
-- more conservative order, consistent with how this project has
-- sequenced every other column-level migration so far.
--
-- Not reversible in the sense that matters: any model-specific
-- BatteryChargingMin value a person may have manually UPDATEd since
-- the original migration's own 13.5-for-everyone default is gone for
-- good once this runs -- there's no snapshot/backup taken here. Given
-- the original migration's own comment confirms every model still had
-- the same default 13.5 as of when this project's own records were
-- last reviewed, this is expected to be a non-issue in practice, not a
-- real data-loss risk being glossed over.
--
-- The DF_PoleModels_BatteryChargingMin default constraint must be
-- dropped before the column itself -- SQL Server does not allow
-- dropping a column that still has a DEFAULT constraint attached to it.
IF EXISTS (
    SELECT 1 FROM sys.default_constraints
    WHERE name = 'DF_PoleModels_BatteryChargingMin'
)
BEGIN
    ALTER TABLE PoleModels DROP CONSTRAINT DF_PoleModels_BatteryChargingMin;
END

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleModels') AND name = 'BatteryChargingMin'
)
BEGIN
    ALTER TABLE PoleModels DROP COLUMN BatteryChargingMin;
END
