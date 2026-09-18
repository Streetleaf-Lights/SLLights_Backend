SELECT --TOP (1000) 
        [Id]
      ,[PoleNumber]
      ,[VendorPoleId]
      ,[ControllerId]
      ,ProvisionedPoleId
      ,PoleModelId
      ,CountyFips
      ,Active
      ,[ProjectId]
      ,[CustomerId]
      ,[InstallDate]
      ,[Lat]
      ,[Long]
      ,[SP_ExecId]
      ,[AirTableCreatedDateTime]
      ,ProvisionedPoleCreatedDateTime
  FROM [dbo].[Poles]
  WHERE 1 = 1
    -- AND Id = 'recBlYYoOlMDMisfv' 
--   AND [VendorPoleId] = 'WREC-1044'
    -- AND VendorPoleId LIKE '%jacks%'
    -- AND PoleNumber = 'TESTSL1-1001'
    AND PoleNumber LIKE '%TESTSL1-100%'
    -- AND (Long IS NULL OR Lat IS NULL)
    -- AND CountyFips IS NOT NULL
    -- AND ControllerId IS NULL
    -- AND ProjectId = 'recsfujvjjvIbycaZ'
    -- AND Active = 1
  ORDER BY [VendorPoleId], [PoleNumber] DESC;

-- SELECT VendorPoleId, COUNT(*) AS PoleCount
-- FROM Poles
-- WHERE VendorPoleId IS NOT NULL
-- GROUP BY VendorPoleId
-- HAVING COUNT(*) > 1;

-- SELECT
--     p.Id,
--     p.PoleNumber,
--     p.VendorPoleId,
--     p.CountyFips,
--     CASE
--         WHEN p.CountyFips IS NULL THEN 'Missing entirely'
--         ELSE 'Not found in CountyTimeZones'
--     END AS Reason
-- FROM Poles p
-- LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
-- WHERE p.VendorPoleId IS NOT NULL
--   AND ctz.FIPS IS NULL
-- ORDER BY p.VendorPoleId;

-- SELECT
--     p.CountyFips,
--     COUNT(*) AS PoleCount
-- FROM Poles p
-- LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
-- WHERE p.VendorPoleId IS NOT NULL
--   AND ctz.FIPS IS NULL
-- GROUP BY p.CountyFips
-- ORDER BY PoleCount DESC;
