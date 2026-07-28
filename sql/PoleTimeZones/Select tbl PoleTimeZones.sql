SELECT
    LocationId,
    Longitude,
    Latitude,
    IanaTimeZone,
    WindowsTimeZone,
    Source,
    SP_ExecId
FROM PoleTimeZones
WHERE 1 = 1
-- AND LocationId = '12081-5012'
AND WindowsTimeZone IS NULL  -- unresolved/unmapped locations
ORDER BY LocationId;
