-- PoleTelemetry joined with PoleModels, with BatteryPercentage/
-- PanelPercentage/LightPercentage computed on the fly -- same formulas
-- and NULLIF guards as pole_vitals_loader.py, so this can be used to
-- spot-check that loader's math directly against raw telemetry.
--
-- LEFT JOIN (not INNER) matches pole_vitals_loader.py's own join: a
-- reading whose ModelId isn't found in PoleModels still shows up here,
-- just with SunboardPower/LightPower/PanelPercentage/LightPercentage all
-- NULL (BatteryPercentage is unaffected either way, since it doesn't
-- depend on PoleModels at all).
SELECT TOP 100
    t.LocationId,
    t.LastUpload,
    t.ModelId,
    pm.ModelName,

    -- Inputs to the three formulas below, included for reference so you
    -- can verify the math by eye without a second query.
    t.BatteryElecCurrent1,
    t.BatteryElecCurrent2,
    t.SolarBoardVoltage,
    t.SolarBoardElecCurrent,
    pm.SunboardPower,
    t.LampPower1,
    t.LampPower2,
    pm.LightPower,

    -- Computed vitals -- must stay in sync with pole_vitals_loader.py's
    -- _HOUR_MERGE_SQL/_DAY_MERGE_SQL/_WEEK_MERGE_SQL/_MONTH_MERGE_SQL
    -- (all four use this exact same TelemetryWithVitals CTE formula).
    (t.BatteryElecCurrent1 + t.BatteryElecCurrent2) / 2.0
        AS BatteryPercentage,
    (t.SolarBoardVoltage * t.SolarBoardElecCurrent) / NULLIF(pm.SunboardPower, 0) * 100.0
        AS PanelPercentage,
    (t.LampPower1 + t.LampPower2) / NULLIF(pm.LightPower, 0) * 100.0
        AS LightPercentage,

    t.Source,
    t.SP_ExecId
FROM PoleTelemetry t
LEFT JOIN PoleModels pm ON t.ModelId = pm.ModelId
WHERE 1 = 1
-- AND t.LocationId = '12101-5540'
-- AND t.SP_ExecId = 442
ORDER BY t.LastUpload DESC;
