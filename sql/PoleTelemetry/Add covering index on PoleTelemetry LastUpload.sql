-- One-time migration: adds a covering index on PoleTelemetry(LastUpload)
-- to eliminate the CLUSTERED INDEX SCAN (not seek) that Hour/Day/Week/
-- Month's shared base query (the TelemetryWithVitals CTE in
-- pole_vitals_loader.py) was doing against PoleTelemetry, instead of
-- seeking directly to the relevant lookback window and reading every
-- needed column straight from the index.
--
-- Confirmed via SQL Server's own MissingIndexGroup suggestion in the
-- real query plan while diagnosing Week's slowness (Impact=11.13) --
-- this is a DIFFERENT, separate fix from the Workweek join index found
-- earlier: it addresses the shared PoleTelemetry scan cost across ALL
-- FOUR period types (Hour/Day/Week/Month all read PoleTelemetry via
-- this same base query shape), not just Week's Workweek-specific join.
--
-- Column list matches that suggestion exactly -- do not hand-edit
-- without re-checking against a fresh missing-index suggestion if
-- pole_vitals_loader.py's formulas ever change which PoleTelemetry
-- columns they read.
--
-- ****************************************************************
-- WARNING, genuinely different from every other index added so far:
-- ****************************************************************
-- This covers PoleTelemetry's ENTIRE 6-month retention window --
-- likely well over a hundred million rows, not the ~10 million in any
-- single period type's own lookback window (Workweek was ~260 rows;
-- PoleTimeZones resolves incrementally). This WILL take real time to
-- build and WILL consume real CPU/IO on an already resource-
-- constrained database while it runs, even with ONLINE = ON.
--
-- ONLINE = ON: lets PoleTelemetry stay readable/writable by every
-- loader (including the concurrent 10-minute ETL cycle) while this
-- builds, rather than holding an exclusive lock for the whole
-- duration. Confirmed supported on Azure SQL Database generally,
-- including Hyperscale.
--
-- RESUMABLE = ON: given this project's own recent history of long-
-- running operations getting interrupted (connection drops, function
-- timeouts, killed sessions), this lets the build be PAUSED and
-- RESUMED without losing prior progress, rather than restarting from
-- zero if something interrupts it partway through. Requires
-- ONLINE = ON. If this does get interrupted, just re-run this same
-- script again -- it'll pick up where it left off rather than starting
-- over, and the IF NOT EXISTS guard means it's safe to re-run either
-- way.
--
-- Recommended: run this via a client that won't itself time out or
-- silently disconnect mid-build (SSMS or Azure Data Studio, not
-- something with an aggressive command timeout).

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTelemetry') AND name = 'IX_PoleTelemetry_LastUpload_Covering'
)
BEGIN
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
            IsDaylight
        )
        WITH (ONLINE = ON, RESUMABLE = ON);
END
