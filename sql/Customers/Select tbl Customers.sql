SELECT TOP (1000) [Id]
      ,[Name]
      ,[ProjectNames]
      ,[ProjectIds]
      ,CrewAssigned
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
--   AND [Id] = 'recEyhvnc11VrvqQj'
  and name LIKE '%merit%'
    -- AND Active = 0
  ORDER BY [Name];
