-- Diagnose why some provisioned PoleTelemetry rows still have NULL daylight flags.
-- Run both queries. The results identify which of the two root causes applies.

-- ============================================================
-- CASE A: PoleTimeZones row EXISTS but flags are still NULL
-- ============================================================
-- Means load_provisioned_pole_timezones() resolved the timezone fine, but
-- is_daylight() failed for these rows (e.g. the argument-order bug now fixed).
-- Fix: re-run loadProvisionedPoleDaylightFlags -- these rows will be
-- picked up on the next batch automatically.

SELECT
    t.PoleId,
    t.LastUpload,
    t.IsDaylight,
    t.IsDaylightForLedFault,
    t.IsDaylightForPanelFault,
    ptz.ProvisionedPoleId,
    ptz.WindowsTimeZone,
    ptz.Latitude,
    ptz.Longitude
FROM PoleTelemetry t
JOIN PoleTimeZones ptz ON t.PoleId = ptz.ProvisionedPoleId
WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL OR t.IsDaylightForPanelFault IS NULL)
  AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
ORDER BY t.LastUpload DESC;

-- ============================================================
-- CASE B: PoleTimeZones row MISSING entirely
-- ============================================================
-- Means load_provisioned_pole_timezones() couldn't resolve the timezone
-- because Poles.CountyFips is NULL or doesn't match CountyTimeZones.
-- The INNER JOIN in _FIND_PROVISIONED_UNFLAGGED_SQL silently excludes
-- these poles, so they will never get flagged until the timezone is resolved.
-- Fix: populate Poles.CountyFips for these PoleIds, then re-run
-- loadProvisionedPoleTimeZones followed by loadProvisionedPoleDaylightFlags.

SELECT DISTINCT
    t.PoleId,
    p.PoleNumber,
    p.CountyFips,
    p.ProvisionedPoleId
FROM PoleTelemetry t
LEFT JOIN PoleTimeZones ptz ON t.PoleId = ptz.ProvisionedPoleId
LEFT JOIN Poles p ON t.PoleId = p.ProvisionedPoleId
WHERE (t.IsDaylight IS NULL OR t.IsDaylightForLedFault IS NULL OR t.IsDaylightForPanelFault IS NULL)
  AND t.LastUpload <> '9999-12-31 23:59:59.999 +00:00'
  AND ptz.ProvisionedPoleId IS NULL
ORDER BY t.PoleId;
