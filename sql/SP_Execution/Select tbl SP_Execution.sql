SELECT TOP 1000
    Id,
    Name,
    Environment,
    Source,
    StartDateTime,
    EndDateTime,
    TotalSuccessfulRecords,
    TotalErrorRecords,
    BatchCount,
    IsFinalBatch,
    ErrorMessage
FROM SP_Execution
WHERE 1 = 1
AND Name <> 'loadProvisionedPoleTelemetry'
-- AND Environment = 'Prod'
-- AND Name = 'loadPoleVitals'
-- AND Name = 'loadProvisionedPoleVitals'
-- AND ErrorMessage IS NOT NULL
ORDER BY StartDateTime DESC;
