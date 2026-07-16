-- PoleModels: device model catalog/specs from the Leadsun API (/models).
--
-- Notes / assumptions (confirm before running against a real environment):
--   * Column list is confirmed against a real Leadsun /models response
--     (not a guess). If Leadsun ever adds a field not listed here, it
--     lands in ExtraFieldsJson (capitalized, JSON-encoded) instead of
--     being dropped -- promote it to its own column later if it matters.
--   * PRIMARY KEY is ModelId alone -- NOT a composite key like PoleTelemetry.
--     PoleModels is a reference/lookup table (specs per device model), not
--     per-device telemetry, so there's no LocationId/LastUpload concept
--     here at all. ModelId arrives as a real JSON integer from the API
--     (not a string), so no rename or numeric conversion is needed for it
--     -- unlike PoleTelemetry's "id", it doesn't collide with any of this
--     project's existing conventions, since it genuinely IS this table's
--     primary key.
--   * Several fields arrive from the API as numeric-looking STRINGS
--     ("80", "12.8", ...) and are converted to real int/float values by
--     pole_models_loader._parse_numeric_string() before landing here --
--     stored as FLOAT uniformly (safe for both whole and fractional
--     values) rather than picking INT vs FLOAT per column.
--   * LampsUsing ("00000001" in the sample) is deliberately NOT converted
--     to a number despite looking numeric -- read as a bitmask-style
--     string (same reasoning as PoleTelemetry's SolarBoardDcStatus/
--     LampBatteryStatus), where leading zeros are meaningful.
--   * No FK anywhere -- PoleModels is part of the same Leadsun ingestion
--     pipeline as PoleTelemetry but isn't referenced by / doesn't reference
--     any other table here.

-- DROP TABLE IF EXISTS PoleModels;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleModels')
BEGIN
    CREATE TABLE PoleModels (
        ModelId           INT           NOT NULL PRIMARY KEY,
        Source            VARCHAR(50)   NOT NULL,
        SP_ExecId         INT           NULL,
        ModelName         NVARCHAR(100) NULL,
        SunboardPower     FLOAT         NULL,
        LightPower        FLOAT         NULL,
        Battery           FLOAT         NULL,
        SystemVoltage     FLOAT         NULL,
        CommType          NVARCHAR(50)  NULL,
        LightDisType      NVARCHAR(50)  NULL,
        IconUrl           NVARCHAR(500) NULL,
        LampsUsing        VARCHAR(20)   NULL,
        BatteryVoltage    FLOAT         NULL,
        IsAc              BIT           NULL,
        IsDcOut           BIT           NULL,
        ModelSeries       NVARCHAR(100) NULL,
        BatteryCapacity1  FLOAT         NULL,
        BatteryCapacity2  FLOAT         NULL,
        SolarBoardVoltage FLOAT         NULL,
        ExtraFieldsJson   NVARCHAR(MAX) NULL
    );

    CREATE NONCLUSTERED INDEX IX_PoleModels_SP_ExecId
        ON PoleModels (SP_ExecId);
END
