SELECT TOP (1000) PoleOpenIssues.[Id]
      ,[IssueId]
      ,[PoleId]
      , p.PoleNumber
      ,[Status]
      ,[PoleStatus]
    --   ,[SP_ExecId]
  FROM [dbo].[PoleOpenIssues] 
  LEFT JOIN Poles p ON PoleOpenIssues.PoleId = p.Id
  WHERE 1 = 1
  AND p.PoleNumber = 'OSC-1099'
    -- AND PoleOpenIssues.[Id] = 'recXUFnjXiVszvreV'
    -- AND PoleOpenIssues.[PoleId] = 'reccrYpcKacWPvsvL'
    -- AND PoleOpenIssues.[IssueId] LIKE '%BRE-1014%'
    ORDER BY P.PoleNumber;

-- -- Quick summary: how many poles currently have an open issue, vs how
-- -- many actually show IsOpenIssueFault=1 anywhere in their recent
-- -- telemetry.
-- SELECT
--     COUNT(DISTINCT p.LocationId) AS PolesWithCurrentOpenIssues,
--     COUNT(DISTINCT CASE WHEN t.IsOpenIssueFault = 1 THEN p.LocationId END) AS PolesWithFlagCorrectlySet
-- FROM Poles p
-- JOIN PoleOpenIssues poi ON poi.PoleId = p.Id
-- JOIN PoleTelemetry t ON t.LocationId = p.LocationId
-- WHERE t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00';

--   SELECT
--     p.LocationId,
--     p.PoleNumber,
--     COUNT(t.LastUpload) AS TotalReadingsChecked,
--     SUM(CASE WHEN t.IsOpenIssueFault = 1 THEN 1 ELSE 0 END) AS ReadingsCorrectlyFlagged,
--     SUM(CASE WHEN ISNULL(t.IsOpenIssueFault, 0) = 0 THEN 1 ELSE 0 END) AS ReadingsIncorrectlyNotFlagged
-- FROM Poles p
-- JOIN PoleOpenIssues poi ON poi.PoleId = p.Id
-- JOIN PoleTelemetry t ON t.LocationId = p.LocationId
-- WHERE t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- GROUP BY p.LocationId, p.PoleNumber
-- ORDER BY ReadingsIncorrectlyNotFlagged DESC;
