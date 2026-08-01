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
  AND [LocationId] = '12081-1240'
    -- AND LocationId LIKE '%12057%'
--   AND PoleNumber = 'SLU-1128'
    -- AND PoleNumber LIKE '%12009-100%'
  ORDER BY InstallDate --DESC, [LocationId] DESC, [PoleNumber] DESC;
