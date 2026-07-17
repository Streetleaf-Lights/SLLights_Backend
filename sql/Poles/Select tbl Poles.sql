SELECT TOP (1000) [Id]
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
  -- AND [LocationId] = '12101-5540'
