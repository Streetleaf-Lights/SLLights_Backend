-- Column list matches pole_telemetry_loader._ALL_COLUMNS exactly (order
-- included) -- if that list ever changes, regenerate this from it rather
-- than hand-editing, to avoid drift.
SELECT TOP 1000
    LocationId,
    LastUpload,
    Source,
    SP_ExecId,
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
    IsOnline,
    TimeoutFlag,
    Longitude,
    Latitude,
    ControlModelCode,
    ControlModelName,
    ExtraFieldsJson,
    IsDaylight
FROM PoleTelemetry
WHERE 1 = 1
AND LocationId = '12069-1058'
-- AND SP_ExecId = 442
-- AND IsDaylight IS NOT NULL
ORDER BY LastUpload DESC;

select
    (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight = 0) as CountDaylight0,
    (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight = 1) as CountDaylight1,
    (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight IS NULL) as CountDaylightNull,
    (SELECT COUNT(*) FROM PoleTelemetry WHERE IsDaylight IS NOT NULL) as CountDaylightNotNull,
    (SELECT COUNT(*) FROM PoleTelemetry) as CountTotal

