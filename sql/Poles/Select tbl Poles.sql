SELECT --TOP (1000) 
        [Id]
      ,[PoleNumber]
      ,[LocationId]
      ,CountyFips
      ,[ProjectId]
      ,[CustomerId]
      ,[InstallDate]
      ,[Lat]
      ,[Long]
      ,[SP_ExecId]
      ,[AirTableCreatedDateTime]
  FROM [dbo].[Poles]
  WHERE 1 = 1
    -- AND Id = 'recwRDOn9vQboBEIS'
--   AND [LocationId] = 'JAX-DEMO'
    -- AND LocationId LIKE '%jacks%'
--   AND PoleNumber = 'PAS-9398'
    -- AND PoleNumber LIKE '%12009-100%'
    -- AND (Long IS NULL OR Lat IS NULL)
    -- AND CountyFips IS NOT NULL
    AND CustomerId = 'recRXOKVBlGRpplTm'
  ORDER BY [LocationId], [PoleNumber] DESC;

-- SELECT LocationId, COUNT(*) AS PoleCount
-- FROM Poles
-- WHERE LocationId IS NOT NULL
-- GROUP BY LocationId
-- HAVING COUNT(*) > 1;

-- SELECT
--     p.Id,
--     p.PoleNumber,
--     p.LocationId,
--     p.CountyFips,
--     CASE
--         WHEN p.CountyFips IS NULL THEN 'Missing entirely'
--         ELSE 'Not found in CountyTimeZones'
--     END AS Reason
-- FROM Poles p
-- LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
-- WHERE p.LocationId IS NOT NULL
--   AND ctz.FIPS IS NULL
-- ORDER BY p.LocationId;

-- SELECT
--     p.CountyFips,
--     COUNT(*) AS PoleCount
-- FROM Poles p
-- LEFT JOIN CountyTimeZones ctz ON p.CountyFips = ctz.FIPS
-- WHERE p.LocationId IS NOT NULL
--   AND ctz.FIPS IS NULL
-- GROUP BY p.CountyFips
-- ORDER BY PoleCount DESC;
