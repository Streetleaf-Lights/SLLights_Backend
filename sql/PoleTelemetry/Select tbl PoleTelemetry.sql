-- Column list matches pole_telemetry_loader._ALL_COLUMNS exactly (order
-- included) -- if that list ever changes, regenerate this from it rather
-- than hand-editing, to avoid drift.
--
-- PERFORMANCE: resolved via a PoleContext CTE that looks up the pole's
-- PoleId/ProvisionedPoleId BEFORE touching PoleTelemetry. This lets
-- SQL Server seek on PoleTelemetry's composite PK (PoleId, LastUpload)
-- rather than scanning the whole table and joining afterwards.
--
-- ► To change the filter: edit the WHERE clause inside PoleContext.
-- ► To change the row limit: edit the TOP value in the final SELECT.
--
-- PoleTimeZones join is split by source:
--   Leadsun rows  → ptz.VendorPoleId
--   Provisioned   → ptz.ProvisionedPoleId

WITH PoleContext AS (
    SELECT
        Id                AS PoleId,
        PoleNumber,
        VendorPoleId      AS LeadsunVendorPoleId,
        ProvisionedPoleId AS ProvisionedPoleId
    FROM Poles
    WHERE PoleNumber = 'JAX-DEMO'  -- ← change filter here
    -- WHERE VendorPoleId = 'DRH-Orl'
    -- WHERE ProvisionedPoleId = '0a10aced202194944a071358'
),
TelemetryForPole AS (
    -- Leadsun branch: seeks on Poles.VendorPoleId
    SELECT t.*
    FROM PoleContext pc
    JOIN PoleTelemetry t ON t.PoleId = pc.LeadsunVendorPoleId
    WHERE pc.LeadsunVendorPoleId IS NOT NULL

    UNION ALL

    -- Provisioned branch: seeks on Poles.ProvisionedPoleId
    SELECT t.*
    FROM PoleContext pc
    JOIN PoleTelemetry t ON t.PoleId = pc.ProvisionedPoleId
    WHERE pc.ProvisionedPoleId IS NOT NULL
)
SELECT TOP 1000  -- ← change row limit here
    t.PoleId,
    pc.PoleNumber,
    t.LastUpload AT TIME ZONE ISNULL(
        COALESCE(ptz_l.WindowsTimeZone, ptz_p.WindowsTimeZone),
        'Eastern Standard Time'
    ) AS LastUpload,
    IsOnline,
    -- IsOpenIssueFault,
    IsDaylight,
    -- IsDaylightForLedFault,
    -- IsDaylightForPanelFault,
    -- t.Source,
    SolarBoardVoltage,
    SolarBoardElecCurrent,
    LampPower1,
    -- LampPower2,
    BatteryVoltage1,
    BatteryVoltage2,
    BatteryElecCurrent1,
    BatteryElecCurrent2,
    ControllerCode,
    -- ProductId,
    UserName,
    -- LeadsunId,
    -- GroupId,
    -- GroupName,
    -- GatewayCode,
    -- LeadsunProjectId,
    -- LeadsunProjectName,
    -- ModelId,
    -- t.Latitude,
    -- t.Longitude,
    -- Provisioned-pole device-reported values (NULL for Leadsun rows)
    BatterySoC,
    LightRatio,
    PanelPercentage,
    BatteryFault,
    LEDFault,
    ControllerFault,
    ExtraFieldsJson
FROM TelemetryForPole t
JOIN PoleContext pc ON 1 = 1
LEFT JOIN PoleTimeZones ptz_l ON t.Source = 'Leadsun'     AND t.PoleId = ptz_l.VendorPoleId
LEFT JOIN PoleTimeZones ptz_p ON t.Source = 'Provisioned' AND t.PoleId = ptz_p.ProvisionedPoleId
WHERE 1 = 1
    -- AND t.IsDaylight IS NULL
    -- AND t.IsDaylight = 1
    -- AND t.IsDaylightForPanelFault IS NULL
    -- AND t.IsOnline = 0
    -- AND t.LampPower1 > 0
    -- AND t.Source = 'Provisioned'
    -- AND t.IsOpenIssueFault = 1
ORDER BY t.LastUpload DESC;

-- SELECT
--     p.PoleNumber,
--     p.ControllerId,
--     COUNT(*) AS TotalReadings,
--     MIN(t.LastUpload) AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS FirstReadingLocal,
--     MAX(t.LastUpload) AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastReadingLocal
-- FROM Poles p
-- JOIN PoleTimeZones ptz ON p.VendorPoleId = ptz.VendorPoleId
-- JOIN PoleTelemetry t ON t.PoleId = p.VendorPoleId
--     AND t.IsOnline = 1
--     AND t.LampPower1 = 0
--     AND t.LastUpload >= '2026-10-02 20:00:00 -04:00'
--     AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- WHERE NOT EXISTS (
--     SELECT 1 FROM PoleTelemetry t2
--     WHERE t2.PoleId = p.VendorPoleId
--       AND t2.LampPower1 > 0
--       AND t2.LastUpload >= '2026-10-02 20:00:00 -04:00'
--       AND t2.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- )
-- GROUP BY p.PoleNumber, p.ControllerId, ptz.WindowsTimeZone
-- ORDER BY p.PoleNumber;
