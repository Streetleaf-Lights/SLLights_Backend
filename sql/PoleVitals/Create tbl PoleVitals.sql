-- PoleVitals: rolling health metrics (battery, solar panel, light
-- output, and per-reading fault flags) derived FROM PoleTelemetry +
-- PoleModels + PoleTimeZones, bucketed by LocationId + PeriodType.
--
-- This is the CONSOLIDATED, up-to-date schema -- it supersedes the
-- separate "Create tbl PoleVitals.sql" / "Add IsOnline and LightStatus
-- columns to PoleVitals.sql" / "Replace LightStatus with fault flags and
-- allow Last48Hours.sql" / "Widen PeriodType to fit Last48Hours.sql" /
-- "Allow LastKnown48Hours PeriodType.sql" migrations that used to live
-- in this same folder (each one incrementally evolved the table over
-- time; this single script reflects where they all ended up, for a
-- brand NEW environment being set up from scratch). If you're instead
-- bringing an EXISTING, already-migrated PoleVitals table up to date
-- with the latest change (removing 'Day'/'Week'/'Month' from the
-- allowed PeriodType set), see "Remove Day Week and Month PeriodTypes.sql"
-- in this same folder instead -- this CREATE script's own IF NOT EXISTS
-- guard means it silently does nothing at all against a table that
-- already exists, so it can't apply that change to a live database on
-- its own.
--
-- Four period types were ever computed at different points in this
-- project's history: Hour, Day, Week, and Month, plus a later-added
-- Last48Hours (a single, continuously-updated rolling window per pole,
-- not a discrete historical bucket) and LastKnown48Hours (identical to
-- Last48Hours, but persists for an offline pole rather than
-- disappearing). By explicit request, only Hour, Last48Hours, and
-- LastKnown48Hours are computed and permitted going forward -- Day,
-- Week, and Month have all been removed entirely (Week/Month due to a
-- real row-explosion bug in Week's own Workweek join, followed by
-- persistent database CPU contention that never resolved proportionally
-- to the tuning effort spent on it; Day for consolidation, once it was
-- the only one left of the three "historical discrete bucket" period
-- types). See shared/pole_vitals_loader.py's own module docstring for
-- the full history and design of what's actually computed today.
--
-- Existing historical rows with PeriodType IN ('Day', 'Week', 'Month')
-- are NOT deleted by removing them from the CHECK CONSTRAINT below --
-- only NEW rows with those values are prevented going forward. See
-- "Remove Day Week and Month PeriodTypes.sql" for how an existing,
-- already-populated table applies this same tightening via WITH NOCHECK
-- specifically so it doesn't reject those already-present rows.
--
-- Unlike every other table in this project, this one isn't synced from an
-- external API directly -- it's computed FROM already-loaded data
-- (PoleTelemetry joined with PoleModels and PoleTimeZones). See
-- shared/pole_vitals_loader.py for the actual aggregation SQL --
-- bucketing uses each POLE'S OWN local wall-clock time (via PoleTimeZones,
-- falling back to Eastern for an unresolved location), not one hardcoded
-- zone for every pole.
--
-- Per-reading formulas (averaged across readings within each bucket to
-- produce this table's Avg* columns):
--   BatteryPercentage = (BatteryElecCurrent1 + BatteryElecCurrent2) / 2
--   PanelPercentage   = (SolarBoardVoltage * SolarBoardElecCurrent) / SunboardPower * 100
--                       (SunboardPower from PoleModels, joined on ModelId)
--   LightPercentage   = (LampPower1 + LampPower2) / LightPower * 100
--                       (LightPower from PoleModels, joined on ModelId)
-- A reading whose model can't be found, or whose SunboardPower/LightPower
-- is 0, contributes NULL for that specific percentage (NULLIF-guarded in
-- the loader's SQL) rather than erroring or skewing the average -- AVG()
-- ignores NULLs. For Last48Hours/LastKnown48Hours specifically,
-- AvgPanelPercentage/AvgLightPercentage are further restricted to only
-- readings taken during daylight with the battery genuinely charging
-- (for Panel) or at night (for Light) -- see pole_vitals_loader.py's own
-- comments on those two constants for the exact conditions.
--
-- Fault flags (IsLedFault/IsBatteryFault/IsPanelFault/IsOpenIssueFault/
-- IsPoleFault) replace an earlier Daylight-based LightStatus
-- classification ('Working'/'DayLight'/'Not Working') that has since
-- been removed from this table entirely -- LightStatus no longer exists
-- anywhere in this project. See shared/pole_vitals_loader.py's own
-- module-level comment for the full per-reading classification and
-- bucket-aggregation logic behind each fault flag.
--
-- IsOnline: Hour/Last48Hours both use "was ANY reading in the bucket/
-- window online". See shared/pole_vitals_loader.py's own module
-- docstring for the exact aggregation.
--
-- PeriodEnd is EXCLUSIVE (the start of the next period) for Hour, e.g.
-- an Hour bucket's PeriodEnd is exactly PeriodStart + 1 hour -- chosen
-- since exclusive bounds are simpler for range queries
-- (`WHERE ts >= PeriodStart AND ts < PeriodEnd`).
--
-- No FK anywhere -- same reasoning as PoleTelemetry/PoleModels: this
-- project doesn't enforce FKs where load/compute order makes it
-- impractical, and PoleVitals is computed after both PoleModels and
-- PoleTelemetry anyway so a FK wouldn't actually be at risk here, it's
-- just kept consistent with the rest of the schema.

-- DROP TABLE IF EXISTS PoleVitals;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleVitals')
BEGIN
    CREATE TABLE PoleVitals (
        LocationId           NVARCHAR(100)     NOT NULL,
        PeriodType           VARCHAR(20)       NOT NULL,  -- 'Hour', 'Last48Hours', or
                                                            -- 'LastKnown48Hours' -- see the
                                                            -- header comment above for the
                                                            -- full history of what this
                                                            -- column has allowed over time
        PeriodStart          DATETIMEOFFSET(3) NOT NULL,
        PeriodEnd            DATETIMEOFFSET(3) NOT NULL,  -- exclusive for Hour -- see note above
        AvgBatteryPercentage FLOAT             NULL,
        AvgPanelPercentage   FLOAT             NULL,
        AvgLightPercentage   FLOAT             NULL,
        IsOnline             BIT               NULL,
        IsLedFault           BIT               NULL,
        IsBatteryFault       BIT               NULL,
        IsPanelFault         BIT               NULL,
        IsOpenIssueFault     BIT               NULL,
        IsPoleFault          BIT               NULL,
        RecordCount          INT               NOT NULL,  -- how many telemetry readings fed this average
        Source               VARCHAR(50)       NOT NULL,
        SP_ExecId            INT               NULL,
        CONSTRAINT PK_PoleVitals PRIMARY KEY (LocationId, PeriodType, PeriodStart),
        CONSTRAINT CK_PoleVitals_PeriodType CHECK (PeriodType IN ('Hour', 'Last48Hours', 'LastKnown48Hours'))
    );

    CREATE NONCLUSTERED INDEX IX_PoleVitals_PeriodType_PeriodStart
        ON PoleVitals (PeriodType, PeriodStart);  -- for "all locations, this period" queries

    CREATE NONCLUSTERED INDEX IX_PoleVitals_SP_ExecId
        ON PoleVitals (SP_ExecId);
END
