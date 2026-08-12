-- Restores PoleTelemetry.IsDaylight -- previously removed by "Replace
-- IsDaylight with IsOpenIssueFault.sql" when this project moved to a
-- fixed 7:00AM-8:00PM clock window for IsLedFault. That fixed window
-- turned out to have a real, unavoidable flaw: whichever Hour bucket
-- straddles the actual sunrise/sunset moment for a given day/location
-- gets misclassified in one direction or the other (e.g. the pole's
-- lamp correctly off just after real sunrise, but still flagged as a
-- fault because the bucket falls entirely before the fixed clock
-- boundary). Real, per-day, per-location sunrise/sunset math (via the
-- restored shared/daylight_utils.py, using the astral library) doesn't
-- have that flaw -- it tracks the sun's ACTUAL elevation, not a fixed
-- clock proxy for it.
--
-- Run this AFTER deploying the updated shared/daylight_utils.py,
-- shared/pole_daylight_flags_loader.py, updated function_app.py (which
-- now calls load_pole_daylight_flags() again, between
-- load_pole_timezones() and load_pole_vitals()), and updated
-- pole_vitals_loader.py (which now reads t.IsDaylight directly for
-- IsLedFault instead of the fixed clock window) -- otherwise
-- load_pole_vitals() will read a column that doesn't exist yet.
--
-- After this runs, pole_daylight_flags_loader.py will treat every
-- existing PoleTelemetry row as unflagged (IsDaylight IS NULL) and
-- backfill them incrementally over subsequent runs, the same
-- incremental-backfill behavior this loader has always had -- not a new
-- concern introduced by restoring it.
--
-- ****************************************************************
-- WARNING: step 2 rebuilds IX_PoleTelemetry_LastUpload_Covering AGAIN --
-- this is the third time this specific index has been rebuilt in this
-- project's history (originally built with IsDaylight included, rebuilt
-- to remove it when IsOpenIssueFault replaced it, now rebuilt again to
-- add IsDaylight back). Still covers PoleTelemetry's entire 6-month
-- retention window (100M+ rows) -- same real time/CPU/IO cost as every
-- previous rebuild of this index, not a quick operation. ONLINE=ON/
-- RESUMABLE=ON carry forward for the same reasons as before.
-- ****************************************************************
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a column just added by one
-- ALTER TABLE isn't visible to an index creation referencing it later in
-- that SAME batch.

-- 1. Restore the column and its own small, filtered index (supports
-- pole_daylight_flags_loader.py's own "find not-yet-flagged rows" query
-- specifically -- a different, narrower index than the big covering one
-- below, which supports pole_vitals_loader.py's much wider scans).
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IsDaylight'
)
BEGIN
    ALTER TABLE PoleTelemetry ADD IsDaylight BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IX_PoleTelemetry_IsDaylight'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_PoleTelemetry_IsDaylight
        ON PoleTelemetry (IsDaylight)
        WHERE IsDaylight IS NULL;
END
GO

-- 2. Rebuild the big covering index with IsDaylight back in its INCLUDE
-- list -- pole_vitals_loader.py's MERGE statements will now read
-- t.IsDaylight for every row in their scan window, same reasoning that
-- put IsOnline/IsOpenIssueFault/ModelId in this index already.
IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IX_PoleTelemetry_LastUpload_Covering'
)
BEGIN
    DROP INDEX IX_PoleTelemetry_LastUpload_Covering ON PoleTelemetry;
END
GO

CREATE NONCLUSTERED INDEX IX_PoleTelemetry_LastUpload_Covering
    ON PoleTelemetry (LastUpload)
    INCLUDE (
        BatteryElecCurrent1,
        BatteryElecCurrent2,
        LampPower1,
        LampPower2,
        SolarBoardVoltage,
        SolarBoardElecCurrent,
        ModelId,
        IsOnline,
        IsOpenIssueFault,
        IsDaylight
    )
    WITH (ONLINE = ON, RESUMABLE = ON);
