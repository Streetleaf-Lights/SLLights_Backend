-- Drops 13 PoleModels columns that are stored but never read back via
-- any join or business logic. Values continue to be captured in
-- ExtraFieldsJson.
--
-- Columns kept (genuinely consumed):
--   ModelId          -- PK
--   Source/SP_ExecId -- tracking
--   ModelName        -- telemetry mapping (productModel field)
--   SunboardPower    -- Leadsun vitals formula: PanelPercentage = SolarV×SolarI / SunboardPower
--   LightPower       -- Leadsun vitals formula: LightPercentage = (Lamp1+Lamp2) / LightPower
--   ExtraFieldsJson  -- catch-all for all other fields

ALTER TABLE dbo.PoleModels DROP COLUMN Battery;
ALTER TABLE dbo.PoleModels DROP COLUMN SystemVoltage;
ALTER TABLE dbo.PoleModels DROP COLUMN CommType;
ALTER TABLE dbo.PoleModels DROP COLUMN LightDisType;
ALTER TABLE dbo.PoleModels DROP COLUMN IconUrl;
ALTER TABLE dbo.PoleModels DROP COLUMN LampsUsing;
ALTER TABLE dbo.PoleModels DROP COLUMN BatteryVoltage;
ALTER TABLE dbo.PoleModels DROP COLUMN IsAc;
ALTER TABLE dbo.PoleModels DROP COLUMN IsDcOut;
ALTER TABLE dbo.PoleModels DROP COLUMN ModelSeries;
ALTER TABLE dbo.PoleModels DROP COLUMN BatteryCapacity1;
ALTER TABLE dbo.PoleModels DROP COLUMN BatteryCapacity2;
ALTER TABLE dbo.PoleModels DROP COLUMN SolarBoardVoltage;
