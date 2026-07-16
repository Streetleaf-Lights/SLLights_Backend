-- PoleTelemetry: raw lamp/pole telemetry from the Leadsun API (/lamps).
--
-- Notes / assumptions (confirm before running against a real environment):
--   * Column list is confirmed against a real Leadsun API response (not a
--     guess). If Leadsun ever adds a field not listed here, it lands in
--     ExtraFieldsJson (capitalized, JSON-encoded) instead of being dropped
--     -- promote it to its own column later if it turns out to matter.
--   * Three columns are deliberately renamed away from a literal
--     capitalize-the-API-field-name rule, to avoid confusion with this
--     project's existing conventions:
--       - Leadsun's own "id"        -> LeadsunId          (a bare "Id"
--         column would look like this table's primary key; it isn't --
--         (LocationId, LastUpload) is)
--       - Leadsun's own "projectId" -> LeadsunProjectId    (would otherwise
--       - Leadsun's own "projectName" -> LeadsunProjectName look like a
--         reference to *our* Airtable-sourced Projects table; it's not --
--         it's Leadsun's own internal project grouping)
--     "productName" -> LocationId is the one rename that WAS explicitly
--     requested, not a judgment call.
--   * PRIMARY KEY is the composite (LocationId, LastUpload), matching
--     "upsert is based on the productName and lastUpload" directly.
--   * No FK anywhere -- PoleTelemetry is a separate ingestion pipeline from
--     the Airtable-sourced tables (Poles/Projects/Customers) and isn't
--     meant to reference them.
--   * Retention (6 months, based on LastUpload) is enforced in code
--     (pole_telemetry_loader.load_pole_telemetry(), runs every invocation),
--     not via a SQL Agent job or partition scheme -- simplest option given
--     the loader already runs every 10 minutes anyway.
--   * String columns are trimmed on the way in (Leadsun's real response
--     had at least one field -- lightingState -- with stray trailing
--     whitespace: "lighting-off ").

-- DROP TABLE IF EXISTS PoleTelemetry;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleTelemetry')
BEGIN
    CREATE TABLE PoleTelemetry (
        LocationId             NVARCHAR(100)     NOT NULL,  -- from productName
        LastUpload             DATETIMEOFFSET(3) NOT NULL,
        Source                 VARCHAR(50)       NOT NULL,
        SP_ExecId              INT               NULL,
        BatteryVoltage1        FLOAT             NULL,
        BatteryVoltage2        FLOAT             NULL,
        BatteryElecCurrent1    FLOAT             NULL,
        BatteryElecCurrent2    FLOAT             NULL,
        LampPower1             FLOAT             NULL,
        LampPower2             FLOAT             NULL,
        SolarBoardVoltage      FLOAT             NULL,
        SolarBoardElecCurrent  FLOAT             NULL,
        DcInVoltage            FLOAT             NULL,
        BatteryOutElecCurrent  FLOAT             NULL,
        BatteryTemperature1    FLOAT             NULL,
        BatteryTemperature2    FLOAT             NULL,
        McuTemperature         FLOAT             NULL,
        EnvTemperature         FLOAT             NULL,
        LightingState          NVARCHAR(50)      NULL,
        DcInState              INT               NULL,
        DcOutState             INT               NULL,
        SolarBoardState        INT               NULL,
        Battery1State          INT               NULL,
        Battery2State          INT               NULL,
        Lamp1State             INT               NULL,
        Lamp2State             INT               NULL,
        ControllerCode         NVARCHAR(50)      NULL,
        ProductId              NVARCHAR(50)      NULL,  -- Leadsun's own product id (distinct from productName/LocationId)
        CreateTime             DATETIMEOFFSET(3) NULL,
        SolarBoardDcStatus     VARCHAR(20)       NULL,  -- binary-flag string, e.g. "00000111" -- kept as text, not converted to int
        LampBatteryStatus      VARCHAR(20)       NULL,
        UserName               NVARCHAR(100)     NULL,
        LeadsunId              INT               NULL,  -- Leadsun's own "id" -- see rename note above
        GroupId                INT               NULL,
        GroupName              NVARCHAR(200)     NULL,
        GatewayCode            NVARCHAR(50)      NULL,
        LeadsunProjectId       INT               NULL,  -- Leadsun's own "projectId" -- see rename note above
        LeadsunProjectName     NVARCHAR(200)     NULL,
        ModelId                INT               NULL,
        IsOnline               BIT               NULL,
        TimeoutFlag            INT               NULL,
        Longitude              FLOAT             NULL,
        Latitude               FLOAT             NULL,
        ControlModelCode       VARCHAR(50)       NULL,
        ControlModelName       NVARCHAR(100)     NULL,
        ExtraFieldsJson        NVARCHAR(MAX)     NULL,  -- safety net for any field not listed above
        PRIMARY KEY (LocationId, LastUpload)
    );

    CREATE NONCLUSTERED INDEX IX_PoleTelemetry_LastUpload
        ON PoleTelemetry (LastUpload);  -- for the retention purge query

    CREATE NONCLUSTERED INDEX IX_PoleTelemetry_SP_ExecId
        ON PoleTelemetry (SP_ExecId);
END
