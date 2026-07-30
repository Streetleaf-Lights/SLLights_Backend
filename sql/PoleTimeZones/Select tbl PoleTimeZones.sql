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
AND LocationId = '12057-1815'
AND WindowsTimeZone IS NULL  -- unresolved/unmapped locations
ORDER BY LocationId;
