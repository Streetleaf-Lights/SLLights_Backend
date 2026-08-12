SELECT
    LocationId,
    Latitude,
    Longitude,
    IanaTimeZone,
    WindowsTimeZone,
    Source,
    SP_ExecId
FROM PoleTimeZones
WHERE 1 = 1
AND LocationId = 'JAX-DEMO'
-- AND LocationId LIKE '%jacks%'
-- AND WindowsTimeZone IS NULL  -- unresolved/unmapped locations
ORDER BY LocationId;

-- DELETE FROM PoleTimeZones WHERE LocationId = 'JAX-DEMO';

-- SELECT DISTINCT
--     t.LocationId,
--     p.PoleNumber,
--     p.CountyFips,
--     ptz.WindowsTimeZone AS ExistingWindowsTimeZone,
--     CASE
--         WHEN p.LocationId IS NULL THEN 'No Poles record found for this LocationId at all'
--         WHEN p.CountyFips IS NULL THEN 'Poles record exists but CountyFips is missing'
--         WHEN ptz.LocationId IS NULL THEN 'CountyFips present but not yet resolved (doesn''t match CountyTimeZones, or loadPoleTimeZones hasn''t run for it yet)'
--         ELSE 'PoleTimeZones row exists but WindowsTimeZone is NULL (likely a leftover from the old Lat/Long-based system, before the county switch)'
--     END AS Reason
-- FROM PoleTelemetry t
-- LEFT JOIN Poles p ON t.LocationId = p.LocationId
-- LEFT JOIN PoleTimeZones ptz ON t.LocationId = ptz.LocationId
-- WHERE t.IsDaylight IS NULL
--   AND (ptz.LocationId IS NULL OR ptz.WindowsTimeZone IS NULL)
--   AND t.LastUpload >= DATEADD(HOUR, -48, SYSDATETIMEOFFSET())
--   AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
-- ORDER BY Reason, t.LocationId;