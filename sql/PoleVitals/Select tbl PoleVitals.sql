SELECT
    LocationId,
    PeriodType,
    PeriodStart,
    PeriodEnd,
    AvgBatteryPercentage,
    AvgPanelPercentage,
    AvgLightPercentage,
    RecordCount,
    Source,
    SP_ExecId
FROM PoleVitals
WHERE 1 = 1
-- AND LocationId = '12101-5540'
ORDER BY LocationId, PeriodType, PeriodStart DESC;
