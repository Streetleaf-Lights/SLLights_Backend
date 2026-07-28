SELECT TOP 1000
    LocationId,
    PeriodType,
    PeriodStart,
    PeriodEnd,
    IsOnline,
    LightStatus,
    AvgBatteryPercentage,
    AvgPanelPercentage,
    AvgLightPercentage,
    RecordCount,
    Source,
    SP_ExecId
FROM PoleVitals
WHERE 1 = 1
-- AND LocationId = '12009-1000'
-- and PeriodStart not like '%-04:%'
-- AND IsOnline IS NOT NULL
AND LightStatus = 'Not Working'
-- AND LightStatus <> 'Working' AND LightStatus <> 'DayLight'
AND PeriodStart >= '2026-07-20 00:00:00'
AND PeriodType = 'Hour'
ORDER BY LocationId, PeriodType, PeriodStart DESC;

-- SELECT SP_ExecId, PeriodType, COUNT(*) AS RowsWritten
-- FROM PoleVitals
-- WHERE SP_ExecId IN (3348, 3353, 3358)
-- GROUP BY SP_ExecId, PeriodType
-- ORDER BY SP_ExecId, PeriodType;
