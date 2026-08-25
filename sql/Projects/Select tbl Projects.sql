SELECT TOP (1000) [Id]
      ,[Name]
      ,[PoleNumbers]
      ,[PoleIds]
      ,[SP_ExecId]
      ,[CustomerId]
      ,[PolesUnderContract]
      ,[EffectiveDate]
      ,[InstallDates]
      ,[LeadsunProjectId]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Projects]
  WHERE 1 = 1
    -- AND id = 'recN0tGiFX8nUkO2B'
  -- AND [CustomerId] = 'recwx649JfiRmWqxF'
  -- AND PoleNumbers LIKE '%HIL-1333%'
    -- AND Name LIKE '%acacia%'
    AND LeadsunProjectId IS NOT NULL
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
