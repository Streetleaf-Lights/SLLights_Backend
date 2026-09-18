SELECT TOP (1000) [Id]
      ,[Name]
      ,[PoleNumbers]
      ,[PoleIds]
      ,Active
      ,[LeadsunProject]
      ,[SP_ExecId]
      ,[CustomerId]
      ,[PolesUnderContract]
      ,[EffectiveDate]
      ,[InstallDates]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Projects]
  WHERE 1 = 1
    AND [Id] = 'recagXKKY76we25Zw'
--   AND [CustomerId] = 'recLWjsXN8vskXZbm'
  -- AND PoleNumbers LIKE '%HIL-1333%'
    -- AND Name LIKE '%acacia%'
    -- AND LeadsunProject IS NOT NULL
    -- AND Active = 1
  ORDER BY [AirTableCreatedDateTime] DESC;

-- SELECT DISTINCT t.PoleId
-- FROM PoleTelemetry t
-- WHERE t.PoleId IN (
--     SELECT p.VendorPoleId
--     FROM Poles p
--     JOIN Projects proj ON p.ProjectId = proj.Id
--     WHERE proj.Name LIKE '%acacia%'
--       AND p.VendorPoleId IS NOT NULL
-- )
-- ORDER BY t.PoleId;
