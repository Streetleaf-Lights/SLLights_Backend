-- Column list matches pole_telemetry_loader._ALL_COLUMNS exactly (order
-- included) -- if that list ever changes, regenerate this from it rather
-- than hand-editing, to avoid drift.
--
-- LastUpload is converted to the POLE'S OWN local time zone (via
-- PoleTimeZones, falling back to Eastern for an unresolved location) --
-- same AT TIME ZONE conversion used throughout pole_vitals_loader.py/
-- pole_vitals_api.py, not the raw UTC value PoleTelemetry itself stores.
--
-- PoleTimeZones has its own LocationId/Longitude/Latitude/Source/
-- SP_ExecId columns too, so every reference to any of those five names
-- is qualified with "PoleTelemetry." below, even where only one table
-- was in scope before this join was added -- otherwise SQL Server can't
-- tell which table's column you mean ("Ambiguous column name").
SELECT TOP 1000
    PoleTelemetry.LocationId,
    LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
    -- IsOnline,
    -- IsOpenIssueFault,
    IsDaylight,
    IsDaylightForLedFault,
    IsDaylightForPanelFault,
    -- PoleTelemetry.Source,
    -- PoleTelemetry.SP_ExecId,
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
    CreateTime,
    SolarBoardDcStatus,
    LampBatteryStatus,
    UserName,
    LeadsunId,
    GroupId,
    GroupName,
    GatewayCode,
    LeadsunProjectId,
    LeadsunProjectName,
    ModelId,
    TimeoutFlag,
    PoleTelemetry.Latitude,
    PoleTelemetry.Longitude,
    ControlModelCode,
    ControlModelName,
    ExtraFieldsJson
FROM PoleTelemetry
LEFT JOIN PoleTimeZones ptz ON PoleTelemetry.LocationId = ptz.LocationId
WHERE 1 = 1
-- AND PoleTelemetry.LocationId = '13240'
-- AND PoleTelemetry.LocationId LIKE '%jacks%'
-- AND PoleTelemetry.SP_ExecId = 442
-- AND IsDaylight IS NULL
-- AND IsDaylight = 1
AND IsDaylightForPanelFault IS NULL
    -- AND IsOnline = 0
    -- AND LampPower1 > 0
    -- AND LampPower2 > 0
ORDER BY PoleTelemetry.LastUpload DESC;

-- WITH TelemetryWithFaultFlags AS (
--     SELECT
--         p.PoleNumber,
--         t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
--         t.IsDaylight,
--         t.IsDaylightForLedFault,
--         CASE
--             WHEN t.IsDaylightForLedFault = 1 THEN 0
--             WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
--             ELSE 0
--         END AS IsLedFaultFlag,
--         t.LampPower1,
--         t.LampPower2,
--         t.IsDaylightForPanelFault,
--         CASE
--             WHEN t.IsDaylightForPanelFault = 0 THEN 0
--             WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= pm.BatteryChargingMin THEN 0
--             WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
--             ELSE 0
--         END AS IsPanelFaultFlag,
--         t.SolarBoardVoltage,
--         t.SolarBoardElecCurrent,
--         -- t.BatteryVoltage1,
--         -- t.BatteryVoltage2,
--         (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 AS AvgBatteryVoltage,
--         pm.BatteryChargingMin
--     FROM PoleTelemetry t
--     LEFT JOIN Poles p ON t.LocationId = p.LocationId
--     LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
--     LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
--     WHERE t.LocationId = '12057-1335'
--         AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--       AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- )
-- SELECT
--     *,
--     CASE
--         WHEN IsLedFaultFlag = 1 AND IsPanelFaultFlag = 1 THEN 'LED + Panel'
--         WHEN IsLedFaultFlag = 1 THEN 'LED'
--         WHEN IsPanelFaultFlag = 1 THEN 'Panel'
--     END AS FaultType
-- FROM TelemetryWithFaultFlags
-- -- WHERE IsLedFaultFlag = 1 OR IsPanelFaultFlag = 1
-- ORDER BY LastUpload DESC;

WITH TelemetryWithFaultFlags AS (
    SELECT
        p.PoleNumber,
        t.LocationId,
        t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
        t.IsOnline,
        t.IsDaylight,
        t.IsDaylightForLedFault,
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        t.LampPower1,
        t.LampPower2,
        t.IsDaylightForPanelFault,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.SolarBoardVoltage,
        t.SolarBoardElecCurrent,
        t.BatteryVoltage1,
        t.BatteryVoltage2,
        (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 AS AvgBatteryVoltage,
        ISNULL(pm.BatteryChargingMin, 13.5) AS BatteryChargingMin,
        t.BatteryElecCurrent1,
        t.BatteryElecCurrent2
    FROM PoleTelemetry t
    LEFT JOIN Poles p ON t.LocationId = p.LocationId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
    WHERE t.LocationId IN (
        SELECT p.LocationId
        FROM Poles p
        WHERE p.ProjectId = 'rec2i59akR8bVf93v'
          AND p.LocationId IS NOT NULL
    )
    -- AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
    AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
)
SELECT
    *,
    CASE
        WHEN IsLedFaultFlag = 1 AND IsPanelFaultFlag = 1 THEN 'LED + Panel'
        WHEN IsLedFaultFlag = 1 THEN 'LED'
        WHEN IsPanelFaultFlag = 1 THEN 'Panel'
    END AS FaultType
FROM TelemetryWithFaultFlags
-- WHERE IsLedFaultFlag = 1 OR IsPanelFaultFlag = 1
ORDER BY LocationId, LastUpload DESC;

-- SELECT
--     COUNT(*) AS TotalRowsLast48Hours,
--     SUM(CASE WHEN t.IsDaylightForLedFault IS NOT NULL THEN 1 ELSE 0 END) AS FlaggedCount,
--     SUM(CASE WHEN t.IsDaylightForLedFault IS NULL AND ptz.WindowsTimeZone IS NOT NULL THEN 1 ELSE 0 END) AS StillPendingCount,
--     SUM(CASE WHEN t.IsDaylightForLedFault IS NULL AND (ptz.LocationId IS NULL OR ptz.WindowsTimeZone IS NULL) THEN 1 ELSE 0 END) AS UnresolvableTimezoneCount,
--     SUM(CASE WHEN t.IsDaylightForPanelFault IS NOT NULL THEN 1 ELSE 0 END) AS FlaggedCount2,
--     SUM(CASE WHEN t.IsDaylightForPanelFault IS NULL AND ptz.WindowsTimeZone IS NOT NULL THEN 1 ELSE 0 END) AS StillPendingCount2,
--     SUM(CASE WHEN t.IsDaylightForPanelFault IS NULL AND (ptz.LocationId IS NULL OR ptz.WindowsTimeZone IS NULL) THEN 1 ELSE 0 END) AS UnresolvableTimezoneCount2
-- FROM PoleTelemetry t
-- LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
-- WHERE t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00';

-- SELECT DISTINCT
--     t.LocationId,
--     t.UserName,
--     t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
--     p.PoleNumber,
--     p.CountyFips,
--     ptz.WindowsTimeZone AS ExistingWindowsTimeZone,
--     CASE
--         WHEN p.LocationId IS NULL THEN 'No Poles record found for this LocationId at all'
--         WHEN p.CountyFips IS NULL THEN 'Poles record exists but CountyFips is missing'
--         WHEN ptz.LocationId IS NULL THEN 'CountyFips present but not yet resolved (doesn''t match CountyTimeZones, or loadPoleTimeZones hasn''t run for it yet)'
--         ELSE 'PoleTimeZones row exists but WindowsTimeZone is NULL (likely a leftover from the old Lat/Long-based system, before the county switch)'
--     END AS Reason
-- FROM PoleTelemetry t
-- LEFT JOIN Poles p ON t.LocationId = p.LocationId
-- LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
-- WHERE t.IsDaylight IS NULL
--   AND (ptz.LocationId IS NULL OR ptz.WindowsTimeZone IS NULL)
--   AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- ORDER BY Reason, t.LocationId, LastUpload DESC;

-- UPDATE PoleTelemetry
-- SET IsDaylightForLedFault = NULL
-- WHERE LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND LastUpload <> '9999-12-31 23:59:59.999 +00:00';

-- UPDATE PoleTelemetry
-- SET IsDaylightForPanelFault = NULL
-- WHERE LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND LastUpload <> '9999-12-31 23:59:59.999 +00:00';

-- DELETE FROM PoleTelemetry WHERE LocationId = 'JAX-DEMO';
