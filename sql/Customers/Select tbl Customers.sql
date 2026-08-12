SELECT TOP (1000) [Id]
      ,[Name]
      ,[ProjectNames]
      ,[ProjectIds]
      ,[SP_ExecId]
      ,[Address]
      ,[City]
      ,[State]
      ,[Zip]
      ,[Phone]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Customers]
  WHERE 1 = 1
  AND [Id] = 'recD6nliOfFlp0VFh'
--   and name = 'Streetleaf'
  ORDER BY [AirTableCreatedDateTime] DESC
