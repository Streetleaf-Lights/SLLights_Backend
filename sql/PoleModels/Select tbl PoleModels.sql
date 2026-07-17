-- Column list matches pole_models_loader._ALL_COLUMNS exactly (order
-- included) -- if that list ever changes, regenerate this from it rather
-- than hand-editing, to avoid drift.
SELECT
    ModelId,
    Source,
    SP_ExecId,
    ModelName,
    SunboardPower,
    LightPower,
    Battery,
    SystemVoltage,
    CommType,
    LightDisType,
    IconUrl,
    LampsUsing,
    BatteryVoltage,
    IsAc,
    IsDcOut,
    ModelSeries,
    BatteryCapacity1,
    BatteryCapacity2,
    SolarBoardVoltage,
    ExtraFieldsJson
FROM PoleModels
WHERE 1 = 1
-- AND ModelId = 82
ORDER BY ModelId;
