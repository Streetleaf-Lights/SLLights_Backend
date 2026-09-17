-- Column list matches pole_telemetry_loader._ALL_COLUMNS exactly (order
-- included) -- if that list ever changes, regenerate this from it rather
-- than hand-editing, to avoid drift.
--
-- PERFORMANCE: resolved via a PoleContext CTE that looks up the pole's
-- LocationId/ProvisionedPoleId BEFORE touching PoleTelemetry. This lets
-- SQL Server seek on PoleTelemetry's composite PK (LocationId, LastUpload)
-- rather than scanning the whole table and joining afterwards.
--
-- The OR join pattern caused a full scan on multi-million-row tables.
-- Resolving the telemetry key(s) upfront from the small Poles table, then
-- using UNION ALL to pull Leadsun and provisioned rows separately, gives
-- SQL Server a seek on each branch independently.
--
-- ► To change the filter: edit the WHERE clause inside PoleContext.
-- ► To change the row limit: edit the TOP value in the final SELECT.
--
-- PoleTimeZones join is split by source:
--   Leadsun rows  → ptz.LocationId
--   Provisioned   → ptz.ProvisionedPoleId
-- so both sources get the correct timezone.

WITH PoleContext AS (
    SELECT
        Id           AS PoleId,
        PoleNumber,
        LocationId          AS LeadsunLocationId,
        ProvisionedPoleId   AS ProvisionedPoleId
    FROM Poles
    WHERE PoleNumber = 'TESTSL1-1002'  -- ← change filter here
    -- WHERE LocationId = 'DRH-Orl'
    -- WHERE ProvisionedPoleId = '0a10aced202194944a071358'
),
TelemetryForPole AS (
    -- Leadsun branch: seeks on Poles.LocationId
    SELECT t.*
    FROM PoleContext pc
    JOIN PoleTelemetry t ON t.LocationId = pc.LeadsunLocationId
    WHERE pc.LeadsunLocationId IS NOT NULL

    UNION ALL

    -- Provisioned branch: seeks on Poles.ProvisionedPoleId
    SELECT t.*
    FROM PoleContext pc
    JOIN PoleTelemetry t ON t.LocationId = pc.ProvisionedPoleId
    WHERE pc.ProvisionedPoleId IS NOT NULL
)
SELECT TOP 1000  -- ← change row limit here
    t.LocationId,
    pc.PoleNumber,
    t.LastUpload AT TIME ZONE ISNULL(
        COALESCE(ptz_l.WindowsTimeZone, ptz_p.WindowsTimeZone),
        'Eastern Standard Time'
    ) AS LastUpload,
    IsOnline,
    IsOpenIssueFault,
    IsDaylight,
    IsDaylightForLedFault,
    IsDaylightForPanelFault,
    t.Source,
    SolarBoardVoltage,
    SolarBoardElecCurrent,
    LampPower1,
    LampPower2,
    BatteryVoltage1,
    BatteryVoltage2,
    BatteryElecCurrent1,
    BatteryElecCurrent2,
    DcInVoltage,
    BatteryOutElecCurrent,
    BatteryTemperature1,
    BatteryTemperature2,
    McuTemperature,
    EnvTemperature,
    LightingState,
    DcInState,
    DcOutState,
    SolarBoardState,
    Battery1State,
    Battery2State,
    Lamp1State,
    Lamp2State,
    ControllerCode,
    ProductId,
    UserName,
    LeadsunId,
    GroupId,
    GroupName,
    GatewayCode,
    LeadsunProjectId,
    LeadsunProjectName,
    ModelId,
    TimeoutFlag,
    t.Latitude,
    t.Longitude,
    ControlModelCode,
    ControlModelName,
    BatterySoC,
    LightRatio,
    PanelPercentage,
    BatteryFault,
    LEDFault,
    ControllerFault,
    ExtraFieldsJson
FROM TelemetryForPole t
JOIN PoleContext pc ON 1 = 1
LEFT JOIN PoleTimeZones ptz_l ON t.Source = 'Leadsun'     AND t.LocationId = ptz_l.LocationId
LEFT JOIN PoleTimeZones ptz_p ON t.Source = 'Provisioned' AND t.LocationId = ptz_p.ProvisionedPoleId
WHERE 1 = 1
    -- AND t.IsDaylight IS NULL
    -- AND t.IsDaylight = 1
    -- AND t.IsDaylightForPanelFault IS NULL
    -- AND t.IsOnline = 0
    -- AND t.LampPower1 > 0
    -- AND t.Source = 'Provisioned'
    -- AND t.IsOpenIssueFault = 1
ORDER BY t.LastUpload DESC;
