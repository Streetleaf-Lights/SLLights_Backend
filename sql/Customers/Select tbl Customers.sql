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
  AND [Id] = 'recEKgOsGbo5LtZpa'
--   and name = 'Streetleaf'
  ORDER BY [AirTableCreatedDateTime] DESC
