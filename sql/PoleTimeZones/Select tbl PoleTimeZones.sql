SELECT
    ptz.LocationId,
    ptz.ProvisionedPoleId,
    p.PoleNumber,
    ptz.Latitude,
    ptz.Longitude,
    ptz.IanaTimeZone,
    ptz.WindowsTimeZone,
    ptz.Source,
    ptz.SP_ExecId
FROM PoleTimeZones ptz
LEFT JOIN Poles p ON (
    (ptz.LocationId IS NOT NULL AND p.LocationId = ptz.LocationId)
    OR
    (ptz.ProvisionedPoleId IS NOT NULL AND p.ProvisionedPoleId = ptz.ProvisionedPoleId)
)
WHERE 1 = 1
-- AND ptz.LocationId = 'TESTSL1-1001'
-- AND ptz.LocationId LIKE '%jacks%'
-- AND ptz.ProvisionedPoleId IS NOT NULL          -- provisioned poles only
-- AND ptz.WindowsTimeZone IS NULL  -- unresolved/unmapped locations
ORDER BY ptz.LocationId;

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