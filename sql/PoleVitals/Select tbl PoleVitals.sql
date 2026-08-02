SELECT TOP 1000
    LocationId,
    PeriodType,
    PeriodStart,
    PeriodEnd,
    IsOnline,
    IsLedFault,
    IsBatteryFault,
    IsPanelFault,
    IsOpenIssueFault,
    IsPoleFault,
    AvgBatteryPercentage,
    AvgPanelPercentage,
    AvgLightPercentage,
    RecordCount,
    Source,
    SP_ExecId
FROM PoleVitals
WHERE 1 = 1
AND LocationId = '12101-4938'
-- and PeriodStart not like '%-04:%'
-- AND IsOnline IS NOT NULL
-- AND LightStatus = 'Not Working'
-- AND LightStatus <> 'Working' AND LightStatus <> 'DayLight'
-- AND PeriodStart >= '2026-07-20 00:00:00'
AND PeriodType = 'Hour'
ORDER BY LocationId, PeriodType, PeriodStart DESC;

-- SELECT LocationId, COUNT(*)
-- FROM PoleVitals
-- WHERE PeriodType = 'Last48Hours'
-- GROUP BY LocationId
-- HAVING COUNT(*) > 1;
