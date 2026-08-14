SELECT TOP 1000
    t.LocationId,
    PoleNumber,
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
    t.SP_ExecId
FROM PoleVitals t
LEFT JOIN Poles p ON t.LocationId = p.LocationId
WHERE 1 = 1
-- AND t.LocationId LIKE '%12057-1398%'
-- and PeriodStart not like '%-04:%'
-- AND IsOnline IS NOT NULL
-- AND PeriodStart >= '2026-07-20 00:00:00'
-- AND IsPoleFault = 0
AND PoleNumber = 'HIL-1014'
AND (PeriodType = 'Last48Hours' OR PeriodType = 'Hour')
ORDER BY LocationId, PeriodStart DESC;

-- SELECT LocationId, COUNT(*)
-- FROM PoleVitals
-- WHERE PeriodType = 'Last48Hours'
-- GROUP BY LocationId
-- HAVING COUNT(*) > 1;

-- SELECT *
-- FROM PoleVitals
-- WHERE LocationId = '12101-4938' AND PeriodType = 'Last48Hours';

-- SELECT TOP 5 StartDateTime, EndDateTime, TotalSuccessfulRecords, TotalErrorRecords, ErrorMessage
-- FROM SP_Execution
-- WHERE Name = 'loadPoleVitals'
-- ORDER BY StartDateTime DESC;

-- SELECT TOP 5 LastUpload, IsDaylight, LampPower1, LampPower2, SolarBoardVoltage, SolarBoardElecCurrent
-- FROM PoleTelemetry
-- WHERE LocationId = '12101-4938'
-- ORDER BY LastUpload DESC;
