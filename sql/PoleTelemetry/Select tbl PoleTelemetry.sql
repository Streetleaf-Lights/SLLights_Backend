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
    BatteryVoltage1,
    BatteryVoltage2,
    BatteryElecCurrent1,
    BatteryElecCurrent2,
    LampPower1,
    LampPower2,
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
-- AND LocationId = '12111-1128'
-- AND SP_ExecId = 442
-- AND IsDaylight IS NOT NULL
    -- AND IsOnline = 0
    -- AND LampPower1 > 0
    -- AND LampPower2 > 0
ORDER BY LastUpload DESC;

-- select
--     (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight = 0) as CountDaylight0,
--     (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight = 1) as CountDaylight1,
--     (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight IS NULL) as CountDaylightNull,
--     (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight IS NOT NULL) as CountDaylightNotNull,
--     (SELECT COUNT(*) FROM PoleTelemetry) as CountTotal

--     1874997	2755001	9630392	4629998	14260390
--     1875149	2774849	9628505	4649998	14278503
--     152     19848   -1887   20000   18113

-- ;WITH TelemetryWithVitals AS (
--     SELECT
--         t.LocationId,
--         (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0 AS BatteryPercentage,
--         (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0 AS PanelPercentage,
--         (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0 AS LightPercentage,
--         CASE WHEN t.IsOnline = 1 THEN 1 ELSE 0 END AS IsOnlineFlag,
--         t.IsOpenIssueFault,
--         ROW_NUMBER() OVER (PARTITION BY t.LocationId ORDER BY t.LastUpload DESC) AS LatestOverall
--     FROM PoleTelemetry t
--     LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
--     WHERE t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--       AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- )
-- SELECT
--     LocationId,
--     AVG(BatteryPercentage) AS AvgBatteryPercentage,
--     AVG(PanelPercentage)   AS AvgPanelPercentage,
--     AVG(LightPercentage)   AS AvgLightPercentage,
--     MAX(IsOnlineFlag)      AS IsOnlineAgg,
--     MAX(CASE WHEN LatestOverall = 1 THEN CAST(IsOpenIssueFault AS TINYINT) END) AS IsOpenIssueFaultAgg,
--     COUNT(*) AS RecordCount
-- FROM TelemetryWithVitals
-- GROUP BY LocationId;

-- SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
-- FROM INFORMATION_SCHEMA.COLUMNS
-- WHERE TABLE_NAME = 'PoleTelemetry' AND COLUMN_NAME = 'IsOpenIssueFault';

-- SELECT COUNT(*) AS TotalRows, MAX(LastUpload) AS MostRecentUpload
-- FROM PoleTelemetry;

-- SELECT TOP 5 StartDateTime, EndDateTime, TotalSuccessfulRecords, TotalErrorRecords, ErrorMessage
-- FROM SP_Execution
-- WHERE Name = 'loadPoleTelemetry'
-- ORDER BY StartDateTime DESC;

-- SELECT cc.name AS ConstraintName, cc.definition AS ConstraintDefinition
-- FROM sys.check_constraints cc
-- WHERE cc.name = 'CK_PoleVitals_PeriodType';
