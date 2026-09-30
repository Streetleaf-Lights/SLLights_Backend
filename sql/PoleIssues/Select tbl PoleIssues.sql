SELECT TOP (1000)
    pi.[Id],
    pi.[IssueId],
    pi.[PoleId],
    p.PoleNumber,
    pi.[Status],
    pi.[PoleStatus],
    pi.[DateReported],
    pi.[ProblemDetails],
    pi.[IssueSource]
    -- pi.[SP_ExecId]
FROM [dbo].[PoleIssues] pi
LEFT JOIN Poles p ON pi.PoleId = p.Id
WHERE 1 = 1
-- AND pi.[Status] = 'Open'
-- AND p.PoleNumber = 'OSC-1099'
AND p.PoleNumber LIKE '%TESTSL1%'
-- AND pi.[Id] = 'recXUFnjXiVszvreV'
-- AND pi.[PoleId] = 'reccrYpcKacWPvsvL'
-- AND pi.[IssueId] LIKE '%BRE-1014%'
ORDER BY p.PoleNumber, DateReported;

-- -- Quick summary: how many poles currently have an open issue vs
-- -- how many correctly show IsOpenIssueFault=1 in recent telemetry.
-- SELECT
--     COUNT(DISTINCT p.VendorPoleId) AS PolesWithCurrentOpenIssues,
--     COUNT(DISTINCT CASE WHEN t.IsOpenIssueFault = 1 THEN p.VendorPoleId END) AS PolesWithFlagCorrectlySet
-- FROM Poles p
-- JOIN PoleIssues pi ON pi.PoleId = p.Id
-- JOIN PoleTelemetry t ON t.PoleId = p.VendorPoleId
-- WHERE pi.Status = 'Open'
--   AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00';

-- SELECT
--     p.VendorPoleId,
--     p.PoleNumber,
--     COUNT(t.LastUpload) AS TotalReadingsChecked,
--     SUM(CASE WHEN t.IsOpenIssueFault = 1 THEN 1 ELSE 0 END) AS ReadingsCorrectlyFlagged,
--     SUM(CASE WHEN ISNULL(t.IsOpenIssueFault, 0) = 0 THEN 1 ELSE 0 END) AS ReadingsIncorrectlyNotFlagged
-- FROM Poles p
-- JOIN PoleIssues pi ON pi.PoleId = p.Id
-- JOIN PoleTelemetry t ON t.PoleId = p.VendorPoleId
-- WHERE pi.Status = 'Open'
--   AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- GROUP BY p.VendorPoleId, p.PoleNumber
-- ORDER BY ReadingsIncorrectlyNotFlagged DESC;
