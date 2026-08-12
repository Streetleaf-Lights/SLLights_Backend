-- Adds PoleTelemetry.IsDaylightForPanelFault -- a deliberately different
-- daylight definition from both the strict IsDaylight column AND
-- IsLedFaultFlag's own IsDaylightForLedFault, used ONLY by
-- pole_vitals_loader.py's IsPanelFaultFlag. See that file's own comments
-- on IsPanelFaultFlag, and shared/pole_daylight_flags_loader.py's own
-- comments on _UPDATE_IS_DAYLIGHT_SQL and
-- _PANEL_FAULT_SUNRISE_WARMUP_PERIOD, for the full reasoning.
--
-- What it means: TRUE if it's daylight right now, AND it was ALSO
-- already daylight one hour before now -- i.e. "has been daylight
-- continuously for at least an hour" -- giving a solar panel time to
-- physically warm up right after sunrise before zero output counts as a
-- fault. Deliberately ONE-SIDED (sunrise only, via an AND against a
-- "before" check) -- unlike IsDaylightForLedFault, which extends
-- daylight's boundaries symmetrically in both directions, this DELAYS
-- when daylight starts counting for panel-output purposes; there's no
-- equivalent "cooldown" concern right before sunset that would warrant
-- a matching check at the other end of the day.
--
-- Run this AFTER deploying the updated
-- shared/pole_daylight_flags_loader.py (which now computes and writes
-- IsDaylight, IsDaylightForLedFault, AND IsDaylightForPanelFault
-- together) and shared/pole_vitals_loader.py (whose IsPanelFaultFlag now
-- reads IsDaylightForPanelFault instead of IsDaylight) -- otherwise
-- loadPoleVitals will read a column that doesn't exist yet.
--
-- After this runs, every EXISTING PoleTelemetry row -- including ones
-- that already have IsDaylight and/or IsDaylightForLedFault set from
-- before this column existed -- will have IsDaylightForPanelFault NULL.
-- pole_daylight_flags_loader.py's own _FIND_UNFLAGGED_SQL already
-- accounts for this (its WHERE clause now checks all three columns, not
-- just the first two), so simply re-running the EXISTING
-- scripts/backfill_is_daylight_last_48_hours.py picks up every row
-- missing any of the three -- no new backfill script needed for this.
--
-- ****************************************************************
-- WARNING: step 2 rebuilds IX_PoleTelemetry_LastUpload_Covering AGAIN --
-- this is the FIFTH time this specific index has been rebuilt in this
-- project's history. Still covers PoleTelemetry's entire 6-month
-- retention window (100M+ rows) -- same real time/CPU/IO cost as every
-- previous rebuild of this index, not a quick operation. ONLINE=ON/
-- RESUMABLE=ON carry forward for the same reasons as before.
--
-- While rebuilding it anyway, this ALSO adds BatteryVoltage1/
-- BatteryVoltage2 to the INCLUDE list -- these should have been added
-- when IsPanelFaultFlag's BatteryChargingMin check was introduced (that
-- change already made pole_vitals_loader.py read both columns for every
-- row in its scan window), but were missed at the time. Folding that fix
-- into this same rebuild avoids paying for a SIXTH one just to correct
-- it separately.
-- ****************************************************************
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a column just added by one
-- ALTER TABLE isn't visible to an index creation referencing it later in
-- that SAME batch.

-- 1. Add the column and its own small, filtered index (supports
-- pole_daylight_flags_loader.py's own "find not-yet-flagged rows" query
-- specifically for rows where THIS column, but not necessarily the
-- other two, is still NULL -- the exact backfill-gap case this
-- migration's own docstring above describes).
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IsDaylightForPanelFault'
)
BEGIN
    ALTER TABLE PoleTelemetry ADD IsDaylightForPanelFault BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IX_PoleTelemetry_IsDaylightForPanelFault'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_PoleTelemetry_IsDaylightForPanelFault
        ON PoleTelemetry (IsDaylightForPanelFault)
        WHERE IsDaylightForPanelFault IS NULL;
END
GO

-- 2. Rebuild the big covering index with IsDaylightForPanelFault (and
-- the previously-missing BatteryVoltage1/BatteryVoltage2, per the
-- warning above) added to its INCLUDE list.
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
        BatteryVoltage1,
        BatteryVoltage2,
        LampPower1,
        LampPower2,
        SolarBoardVoltage,
        SolarBoardElecCurrent,
        ModelId,
        IsOnline,
        IsOpenIssueFault,
        IsDaylight,
        IsDaylightForLedFault,
        IsDaylightForPanelFault
    )
    WITH (ONLINE = ON, RESUMABLE = ON);
