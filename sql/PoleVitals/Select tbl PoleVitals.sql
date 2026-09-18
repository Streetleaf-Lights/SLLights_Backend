-- PERFORMANCE: PoleContext CTE resolves the pole's PoleId/ProvisionedPoleId
-- from the small Poles table first, then VitalsForPole uses UNION ALL to seek
-- on PoleVitals' composite PK (PoleId, PeriodType, PeriodStart) for each
-- source separately -- rather than scanning PoleVitals and OR-joining Poles
-- across every row.
--
-- ► To change the filter: edit the WHERE clause inside PoleContext.

WITH PoleContext AS (
    SELECT
        PoleNumber,
        VendorPoleId  AS LeadsunVendorPoleId,
        ProvisionedPoleId AS ProvisionedPoleId
    FROM Poles
    WHERE PoleNumber = 'TESTSL1-1002'  -- ← change filter here
    -- WHERE PoleId = 'DRH-Orl'
    -- WHERE ProvisionedPoleId = '0a10aced202194944a071358'
),
VitalsForPole AS (
    -- Leadsun branch: seeks on PK using Poles.VendorPoleId
    SELECT v.*
    FROM PoleContext pc
    JOIN PoleVitals v ON v.PoleId = pc.LeadsunVendorPoleId
    WHERE pc.LeadsunVendorPoleId IS NOT NULL

    UNION ALL

    -- Provisioned branch: seeks on PK using Poles.ProvisionedPoleId
    SELECT v.*
    FROM PoleContext pc
    JOIN PoleVitals v ON v.PoleId = pc.ProvisionedPoleId
    WHERE pc.ProvisionedPoleId IS NOT NULL
)
SELECT TOP 1000
    v.PoleId,
    pc.PoleNumber,
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
    v.SP_ExecId
FROM VitalsForPole v
JOIN PoleContext pc ON 1 = 1
WHERE 1 = 1
    -- AND IsOnline = 0
    -- AND PeriodStart >= '2026-08-17 19:00:00'
    -- AND IsPoleFault = 0
    -- AND IsOpenIssueFault = 1
    -- AND PeriodType = 'Last48Hours'
ORDER BY pc.PoleNumber, v.PoleId, PeriodStart DESC;


-- ── Diagnostics (uncomment one block at a time) ───────────────────────────

-- Duplicate Last48Hours rows per pole (should always be 1):
-- SELECT PoleId, COUNT(*)
-- FROM PoleVitals
-- WHERE PeriodType = 'Last48Hours'
-- GROUP BY PoleId
-- HAVING COUNT(*) > 1;

-- Inspect a specific pole's Last48Hours row:
-- SELECT *
-- FROM PoleVitals
-- WHERE PoleId = '12101-4938' AND PeriodType = 'Last48Hours';

-- Recent loadPoleVitals runs:
-- SELECT TOP 5 StartDateTime, EndDateTime, TotalSuccessfulRecords, TotalErrorRecords, ErrorMessage
-- FROM SP_Execution
-- WHERE Name = 'loadPoleVitals'
-- ORDER BY StartDateTime DESC;

-- Latest telemetry for a pole:
-- SELECT TOP 5 LastUpload, IsDaylight, LampPower1, LampPower2, SolarBoardVoltage, SolarBoardElecCurrent
-- FROM PoleTelemetry
-- WHERE PoleId = '12101-4938'
-- ORDER BY LastUpload DESC;
