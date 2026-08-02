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
    IsOnline,
    IsOpenIssueFault,
    IsDaylight,
    -- PoleTelemetry.Source,
    -- PoleTelemetry.SP_ExecId,
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
    PoleTelemetry.Longitude,
    PoleTelemetry.Latitude,
    ControlModelCode,
    ControlModelName,
    ExtraFieldsJson
FROM PoleTelemetry
LEFT JOIN PoleTimeZones ptz ON PoleTelemetry.LocationId = ptz.LocationId
WHERE 1 = 1
AND PoleTelemetry.LocationId = '12101-4938'
-- AND PoleTelemetry.SP_ExecId = 442
-- AND IsDaylight IS NOT NULL
    -- AND IsOnline = 0
    -- AND LampPower1 > 0
    -- AND LampPower2 > 0
ORDER BY PoleTelemetry.LastUpload DESC;
