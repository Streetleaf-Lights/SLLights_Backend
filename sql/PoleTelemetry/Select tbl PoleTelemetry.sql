-- Column list matches pole_telemetry_loader._ALL_COLUMNS exactly (order
-- included) -- if that list ever changes, regenerate this from it rather
-- than hand-editing, to avoid drift.
SELECT TOP 1000
    LocationId,
    LastUpload,
    IsOnline,
    IsOpenIssueFault,
    -- Source,
    -- SP_ExecId,
    LampPower1,
    LampPower2,
    BatteryVoltage1,
    BatteryVoltage2,
    BatteryElecCurrent1,
    BatteryElecCurrent2,
    SolarBoardVoltage,
    SolarBoardElecCurrent,
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
    Longitude,
    Latitude,
    ControlModelCode,
    ControlModelName,
    ExtraFieldsJson
FROM PoleTelemetry
WHERE 1 = 1
AND (LocationId = '12101-4938')
-- AND SP_ExecId = 442
-- AND IsDaylight IS NOT NULL
    -- AND IsOnline = 0
    -- AND LampPower1 > 0
    -- AND LampPower2 > 0
ORDER BY LastUpload DESC;

WITH TelemetryWithFaultFlags AS (
    SELECT
        p.PoleNumber,
        t.LastUpload AT TIME ZONE ISNULL(ptz.WindowsTimeZone, 'Eastern Standard Time') AS LastUpload,
        t.IsDaylight,
        -- t.IsDaylightForLedFault,
        CASE
            WHEN t.IsDaylightForLedFault = 1 THEN 0
            WHEN (t.LampPower1 + t.LampPower2) = 0 THEN 1
            ELSE 0
        END AS IsLedFaultFlag,
        -- t.LampPower1,
        -- t.LampPower2,
        t.IsDaylightForPanelFault,
        CASE
            WHEN t.IsDaylightForPanelFault = 0 THEN 0
            WHEN (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 >= ISNULL(pm.BatteryChargingMin, 13.5) THEN 0
            WHEN (t.SolarBoardVoltage * t.SolarBoardElecCurrent) = 0 THEN 1
            ELSE 0
        END AS IsPanelFaultFlag,
        t.SolarBoardVoltage,
        t.SolarBoardElecCurrent,
        -- t.BatteryVoltage1,
        -- t.BatteryVoltage2,
        (t.BatteryVoltage1 + t.BatteryVoltage2) / 2.0 AS AvgBatteryVoltage,
        ISNULL(pm.BatteryChargingMin, 13.5) AS BatteryChargingMin
    FROM PoleTelemetry t
    LEFT JOIN Poles p ON t.LocationId = p.LocationId
    LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
    LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
    WHERE t.LocationId = '12057-4424'
        AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
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
ORDER BY LastUpload DESC;