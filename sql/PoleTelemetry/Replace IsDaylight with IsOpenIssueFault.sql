-- Combined into one migration deliberately, not split into two separate
-- files: adding IsOpenIssueFault and removing IsDaylight both touch
-- IX_PoleTelemetry_LastUpload_Covering, and that index's rebuild step
-- needs IsOpenIssueFault to already exist and IsDaylight to still exist
-- (so it can be dropped from the INCLUDE list cleanly). Splitting this
-- into two files would create a real, easy-to-get-wrong ordering
-- dependency between them; keeping it in one file with GO-separated
-- batches makes the correct order the only order.
--
-- What this does, in order:
--   1. Adds IsOpenIssueFault (replaces Daylight-based LightStatus
--      classification -- see pole_vitals_loader.py's module docstring
--      for the new fault-flag design this supports).
--   2. Drops IX_PoleTelemetry_IsDaylight (the filtered index that
--      existed solely to support IsDaylight).
--   3. Rebuilds IX_PoleTelemetry_LastUpload_Covering without IsDaylight,
--      with IsOpenIssueFault in its place -- the rollup queries will
--      read it constantly, same reasoning that put IsOnline/ModelId in
--      this index originally.
--   4. Drops the IsDaylight column itself, now that nothing references it.
--
-- Run this AFTER deploying the updated pole_telemetry_loader.py/
-- pole_vitals_loader.py (neither reads/writes IsDaylight anymore, and
-- pole_telemetry_loader.py now writes IsOpenIssueFault) and after
-- removing pole_daylight_flags_loader.py's call from function_app.py --
-- otherwise the old loader will immediately start failing every run
-- trying to write to a column that no longer exists.
--
-- ****************************************************************
-- WARNING: step 3 rebuilds IX_PoleTelemetry_LastUpload_Covering, which
-- covers PoleTelemetry's ENTIRE 6-month retention window (100M+ rows
-- per that index's own original migration). This WILL take real time
-- and consume real CPU/IO, same caution as when that index was first
-- built. ONLINE=ON/RESUMABLE=ON carry forward for the same reasons --
-- see "Add covering index on PoleTelemetry LastUpload.sql" for the full
-- original reasoning, unchanged here. Recommended: run via a client
-- that won't itself time out or silently disconnect mid-build (SSMS or
-- Azure Data Studio, not something with an aggressive command timeout).
-- ****************************************************************
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a column just added/dropped
-- by one ALTER TABLE isn't necessarily visible (or safely absent) to a
-- statement compiled in the same batch. Each GO forces the preceding
-- statement(s) to run as their own batch first.

-- 1. Add IsOpenIssueFault.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IsOpenIssueFault'
)
BEGIN
    ALTER TABLE PoleTelemetry ADD IsOpenIssueFault BIT NULL;
END
GO

-- 2. Drop the filtered index that exists purely to support IsDaylight.
IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IX_PoleTelemetry_IsDaylight'
)
BEGIN
    DROP INDEX IX_PoleTelemetry_IsDaylight ON PoleTelemetry;
END
GO

-- 3. Rebuild the big covering index: IsDaylight out, IsOpenIssueFault in.
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
        IsOpenIssueFault
    )
    WITH (ONLINE = ON, RESUMABLE = ON);
GO

-- 4. Drop IsDaylight itself, now that nothing references it.
IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IsDaylight'
)
BEGIN
    ALTER TABLE PoleTelemetry DROP COLUMN IsDaylight;
END
