-- Cleanup after the column renames (which already succeeded).
-- The column renames did NOT break the existing PKs or indexes -- SQL Server
-- tracks columns by internal ID, not name. Only the index/constraint NAMES
-- need updating for clarity, and any non-clustered indexes that were
-- explicitly named after the old column.
--
-- NOTE: sp_rename on a clustered PK over millions of rows is a full table
-- rewrite and will timeout. Instead, just rename the constraint name itself
-- (lightweight metadata-only operation) and rename/rebuild non-clustered
-- indexes as needed.

-- ── Rename PK constraints to match new column names (metadata only) ───────────
-- These are name-only changes -- no data movement, no timeout risk.

EXEC sp_rename 'dbo.PoleTelemetry.PK_PoleTelemetry', 'PK_PoleTelemetry_PoleId', 'OBJECT';
EXEC sp_rename 'dbo.PoleVitals.PK_PoleVitals', 'PK_PoleVitals_PoleId', 'OBJECT';

-- ── Rename non-clustered indexes that referenced the old column name ───────────
-- sp_rename on an index is also metadata-only (no rebuild).

-- PoleTelemetry covering index (if it exists under the old name)
IF EXISTS (SELECT 1 FROM sys.indexes
           WHERE name = 'IX_PoleTelemetry_LocationId_LastUpload'
             AND object_id = OBJECT_ID('dbo.PoleTelemetry'))
    EXEC sp_rename 'dbo.PoleTelemetry.IX_PoleTelemetry_LocationId_LastUpload',
                   'IX_PoleTelemetry_PoleId_LastUpload', 'INDEX';

-- PoleVitals index (if it exists under the old name)
IF EXISTS (SELECT 1 FROM sys.indexes
           WHERE name = 'IX_PoleVitals_LocationId_PeriodType_PeriodStart'
             AND object_id = OBJECT_ID('dbo.PoleVitals'))
    EXEC sp_rename 'dbo.PoleVitals.IX_PoleVitals_LocationId_PeriodType_PeriodStart',
                   'IX_PoleVitals_PoleId_PeriodType_PeriodStart', 'INDEX';

-- ── PoleTimeZones filtered index (if not already recreated) ───────────────────
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'UX_PoleTimeZones_VendorPoleId'
                 AND object_id = OBJECT_ID('dbo.PoleTimeZones'))
    EXEC('CREATE UNIQUE NONCLUSTERED INDEX UX_PoleTimeZones_VendorPoleId
              ON dbo.PoleTimeZones (VendorPoleId)
              WHERE VendorPoleId IS NOT NULL;');
