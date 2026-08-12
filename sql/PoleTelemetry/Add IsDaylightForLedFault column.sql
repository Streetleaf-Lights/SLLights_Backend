-- Adds PoleTelemetry.IsDaylightForLedFault -- a deliberately more
-- forgiving daylight definition than the existing, strict IsDaylight
-- column, used ONLY by pole_vitals_loader.py's IsLedFaultFlag. See that
-- file's own comments on IsLedFaultFlag, and
-- shared/pole_daylight_flags_loader.py's own comments on
-- _UPDATE_IS_DAYLIGHT_SQL and _LED_FAULT_GRACE_PERIOD, for the full
-- reasoning.
--
-- Why this needed to become a SEPARATE column rather than just adjusting
-- IsDaylight itself: confirmed in practice, a real lamp doesn't always
-- turn on the INSTANT the sun crosses the sunset threshold -- a lamp was
-- still off 30 minutes after IsDaylight flipped to 0, then correctly on
-- by the next reading. Extending IsDaylight itself to cover that lag
-- would fix that IsLedFault false positive, but would break IsPanelFault
-- in the opposite direction: IsPanelFault checks the OPPOSITE condition
-- (IsDaylight = 0 means "don't require panel output"), so an IsDaylight
-- that stayed "1" past real sunset would incorrectly start REQUIRING
-- panel output during an hour that's actually already dark. The two
-- fault flags need genuinely different daylight definitions.
--
-- Run this AFTER deploying the updated shared/pole_daylight_flags_loader.py
-- (which now computes and writes both IsDaylight and
-- IsDaylightForLedFault together) and shared/pole_vitals_loader.py
-- (whose IsLedFaultFlag now reads IsDaylightForLedFault instead of
-- IsDaylight) -- otherwise loadPoleVitals will read a column that
-- doesn't exist yet.
--
-- After this runs, every EXISTING PoleTelemetry row -- including ones
-- that already have IsDaylight set from before this column existed --
-- will have IsDaylightForLedFault NULL. pole_daylight_flags_loader.py's
-- own _FIND_UNFLAGGED_SQL already accounts for this (its WHERE clause
-- checks "IsDaylight IS NULL OR IsDaylightForLedFault IS NULL", not just
-- the first), so simply re-running the EXISTING
-- scripts/backfill_is_daylight_last_48_hours.py picks up every row
-- missing either column -- no new backfill script needed for this.
--
-- ****************************************************************
-- WARNING: step 2 rebuilds IX_PoleTelemetry_LastUpload_Covering AGAIN --
-- this is the FOURTH time this specific index has been rebuilt in this
-- project's history (originally built with IsDaylight included, rebuilt
-- to remove it when IsOpenIssueFault replaced it, rebuilt again to add
-- IsDaylight back, now rebuilt again to add IsDaylightForLedFault).
-- Still covers PoleTelemetry's entire 6-month retention window (100M+
-- rows) -- same real time/CPU/IO cost as every previous rebuild of this
-- index, not a quick operation. ONLINE=ON/RESUMABLE=ON carry forward
-- for the same reasons as before.
-- ****************************************************************
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a column just added by one
-- ALTER TABLE isn't visible to an index creation referencing it later in
-- that SAME batch.

-- 1. Add the column and its own small, filtered index (supports
-- pole_daylight_flags_loader.py's own "find not-yet-flagged rows" query
-- specifically for rows where THIS column, but not necessarily
-- IsDaylight, is still NULL -- the exact backfill-gap case this
-- migration's own docstring above describes).
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IsDaylightForLedFault'
)
BEGIN
    ALTER TABLE PoleTelemetry ADD IsDaylightForLedFault BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IX_PoleTelemetry_IsDaylightForLedFault'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_PoleTelemetry_IsDaylightForLedFault
        ON PoleTelemetry (IsDaylightForLedFault)
        WHERE IsDaylightForLedFault IS NULL;
END
GO

-- 2. Rebuild the big covering index with IsDaylightForLedFault added to
-- its INCLUDE list -- pole_vitals_loader.py's IsLedFaultFlag now reads
-- t.IsDaylightForLedFault for every row in its scan window, same
-- reasoning that put IsDaylight/IsOnline/IsOpenIssueFault/ModelId in
-- this index already.
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
        IsDaylight,
        IsDaylightForLedFault
    )
    WITH (ONLINE = ON, RESUMABLE = ON);
