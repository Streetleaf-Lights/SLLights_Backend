SELECT TOP (1000) [Id]
      ,[Name]
      ,[PoleNumbers]
      ,[PoleIds]
      ,[SP_ExecId]
      ,[CustomerId]
      ,[PolesUnderContract]
      ,[EffectiveDate]
      ,[InstallDates]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Projects]
  WHERE [CustomerId] = 'recwx649JfiRmWqxF'
  ORDER BY [AirTableCreatedDateTime] DESC
  