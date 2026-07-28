SELECT --TOP (1000) 
        [Id]
      ,[PoleNumber]
      ,[LocationId]
      ,[ProjectId]
      ,[CustomerId]
      ,[InstallDate]
      ,[Lat]
      ,[Long]
      ,[SP_ExecId]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Poles]
  WHERE 1 = 1
  AND [LocationId] = '12009-1000'
--   AND PoleNumber = 'PAS-5199'
    -- AND PoleNumber LIKE '%12009-100%'
  ORDER BY [LocationId] DESC, [PoleNumber] DESC;
