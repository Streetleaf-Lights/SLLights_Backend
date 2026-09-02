SELECT TOP (1000) [Id]
      ,[Name]
      ,[PoleNumbers]
      ,[PoleIds]
      ,Active
      ,[SP_ExecId]
      ,[CustomerId]
      ,[PolesUnderContract]
      ,[EffectiveDate]
      ,[InstallDates]
      ,[LeadsunProject]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Projects]
  WHERE 1 = 1
    -- AND id = 'recN0tGiFX8nUkO2B'
--   AND [CustomerId] = 'recLWjsXN8vskXZbm'
  -- AND PoleNumbers LIKE '%HIL-1333%'
    -- AND Name LIKE '%acacia%'
    -- AND LeadsunProject IS NOT NULL
    -- AND Active = 1
  ORDER BY [AirTableCreatedDateTime] DESC

-- SELECT DISTINCT t.LocationId
-- FROM PoleTelemetry t
-- WHERE t.LocationId IN (
--     SELECT p.LocationId
--     FROM Poles p
--     JOIN Projects proj ON p.ProjectId = proj.Id
--     WHERE proj.Name LIKE '%acacia%'
--       AND p.LocationId IS NOT NULL
-- )
-- ORDER BY t.LocationId;
