SELECT TOP (1000) [FIPS]
      ,[CountyName]
      ,[State]
      ,[Latitude]
      ,[Longitude]
      ,[IanaTimeZone]
      ,[WindowsTimeZone]
      ,[CoordinateOverrideReason]
  FROM [dbo].[CountyTimeZones]
  WHERE 1 = 1
    AND CountyName LIKE '%Dade%'
    -- AND [FIPS] = '12031'