-- Adds six columns to PoleTelemetry for provisioned-pole device-reported
-- values extracted from ExtraFieldsJson at ingestion time.
--
-- BatterySoC / LightRatio / PanelPercentage are the device's own
-- pre-computed percentages, used directly as AvgBatteryPercentage /
-- AvgLightPercentage / AvgPanelPercentage in PoleVitals instead of
-- deriving them from raw current/power readings (which don't translate
-- cleanly for this hardware).
--
-- BatteryFault / LEDFault / ControllerFault are the device's own fault
-- flags, used directly as IsBatteryFault / IsLedFault / IsPanelFault in
-- PoleVitals (subject to the same daylight-condition guards as before).
--
-- All columns are NULL for Leadsun rows -- populated only by the
-- provisioned telemetry loader.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.PoleTelemetry')
      AND name = 'BatterySoC'
)
    ALTER TABLE dbo.PoleTelemetry ADD BatterySoC FLOAT NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.PoleTelemetry')
      AND name = 'LightRatio'
)
    ALTER TABLE dbo.PoleTelemetry ADD LightRatio FLOAT NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.PoleTelemetry')
      AND name = 'PanelPercentage'
)
    ALTER TABLE dbo.PoleTelemetry ADD PanelPercentage FLOAT NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.PoleTelemetry')
      AND name = 'BatteryFault'
)
    ALTER TABLE dbo.PoleTelemetry ADD BatteryFault BIT NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.PoleTelemetry')
      AND name = 'LEDFault'
)
    ALTER TABLE dbo.PoleTelemetry ADD LEDFault BIT NULL;

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.PoleTelemetry')
      AND name = 'ControllerFault'
)
    ALTER TABLE dbo.PoleTelemetry ADD ControllerFault BIT NULL;
