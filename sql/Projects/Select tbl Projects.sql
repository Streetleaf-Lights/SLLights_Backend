SELECT TOP (1000) [Id]
      ,[Name]
      ,StickerNumbers
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
    -- AND [Id] = 'recagXKKY76we25Zw'
--   AND [CustomerId] = 'recLWjsXN8vskXZbm'
  -- AND PoleNumbers LIKE '%HIL-1333%'
    AND Name LIKE '%babcock%'
    -- AND LeadsunProject IS NOT NULL
    -- AND Active = 1
  ORDER BY [AirTableCreatedDateTime] DESC;
