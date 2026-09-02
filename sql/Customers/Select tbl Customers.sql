SELECT TOP (1000) [Id]
      ,[Name]
      ,[ProjectNames]
      ,[ProjectIds]
      ,Active
      ,[SP_ExecId]
      ,[Address]
      ,[City]
      ,[State]
      ,[Zip]
      ,[Phone]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Customers]
  WHERE 1 = 1
--   AND [Id] = 'recEKgOsGbo5LtZpa'
--   and name LIKE '%Swiss%'
    AND Active = 1
  ORDER BY [Name]
