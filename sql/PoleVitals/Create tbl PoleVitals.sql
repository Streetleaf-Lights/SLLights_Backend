-- PoleVitals: rolling averages of pole health metrics (battery, solar
-- panel, light output) derived FROM PoleTelemetry + PoleModels, bucketed
-- into Hour/Day periods per LocationId.
--
-- Week and Month period types were removed from active computation (see
-- shared/pole_vitals_loader.py's module docstring and the README for the
-- full history -- a real row-explosion bug in Week's Workweek join, then
-- persistent database CPU contention that never resolved proportionally
-- to the tuning effort spent on it). The CHECK CONSTRAINT below
-- deliberately still allows 'Week'/'Month' as PeriodType values, rather
-- than being tightened to just ('Hour', 'Day') -- if any existing rows
-- with PeriodType IN ('Week', 'Month') are still in this table, a
-- tightened constraint would need those rows dealt with first (deleted,
-- or the constraint added WITH NOCHECK). Whether to delete those
-- historical rows, and whether to tighten this constraint afterward, is
-- an open decision -- ask if you want help with either.
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
-- ignores NULLs.
--
-- IsOnline / LightStatus: see shared/pole_vitals_loader.py's module-level
-- comment for the full per-reading classification and bucket-aggregation
-- logic. One thing worth restating here since it's easy to get wrong:
-- for Day, the "last 6 hours" window IsOnline/LightStatus use is relative
-- to EACH BUCKET'S OWN END, not to whenever load_pole_vitals() happens to
-- run -- a historical bucket recomputed later still reflects that same
-- period's own tail end, not "now".
--
-- PeriodEnd is EXCLUSIVE (the start of the next period), e.g. an Hour
-- bucket's PeriodEnd is exactly PeriodStart + 1 hour -- this differs from
-- Workweek's own EndDate convention (inclusive, the Saturday itself,
-- relevant if Week bucketing is ever reintroduced), a deliberate choice
-- since exclusive bounds are simpler for range queries
-- (`WHERE ts >= PeriodStart AND ts < PeriodEnd`) at hour/day granularity.
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
        PeriodType           VARCHAR(10)       NOT NULL,  -- 'Hour' or 'Day' actively computed --
                                                            -- 'Week'/'Month' still allowed by the
                                                            -- CHECK CONSTRAINT below but no longer
                                                            -- written by load_pole_vitals() -- see
                                                            -- the header comment above
        PeriodStart          DATETIMEOFFSET(3) NOT NULL,
        PeriodEnd            DATETIMEOFFSET(3) NOT NULL,  -- exclusive -- see note above
        AvgBatteryPercentage FLOAT             NULL,
        AvgPanelPercentage   FLOAT             NULL,
        AvgLightPercentage   FLOAT             NULL,
        IsOnline             BIT               NULL,  -- Hour: any reading in the bucket was online.
                                                        -- Day: any reading in the last 6
                                                        -- hours OF THAT BUCKET'S OWN END was online
                                                        -- (not "now", and not the whole period) --
                                                        -- see the header comment above and
                                                        -- pole_vitals_loader.py's module docstring
        LightStatus          VARCHAR(20)        NULL,  -- 'Working' / 'DayLight' / 'Not Working' --
                                                        -- see pole_vitals_loader.py for the exact
                                                        -- per-reading classification and priority-based
                                                        -- bucket aggregation
        RecordCount          INT               NOT NULL,  -- how many telemetry readings fed this average
        Source               VARCHAR(50)       NOT NULL,
        SP_ExecId            INT               NULL,
        CONSTRAINT PK_PoleVitals PRIMARY KEY (LocationId, PeriodType, PeriodStart),
        -- Deliberately NOT tightened to ('Hour', 'Day') -- see the header
        -- comment above for why this is an open decision, not an oversight.
        CONSTRAINT CK_PoleVitals_PeriodType CHECK (PeriodType IN ('Hour', 'Day', 'Week', 'Month')),
        CONSTRAINT CK_PoleVitals_LightStatus CHECK (LightStatus IN ('Working', 'DayLight', 'Not Working'))
    );

    CREATE NONCLUSTERED INDEX IX_PoleVitals_PeriodType_PeriodStart
        ON PoleVitals (PeriodType, PeriodStart);  -- for "all locations, this period" queries

    CREATE NONCLUSTERED INDEX IX_PoleVitals_SP_ExecId
        ON PoleVitals (SP_ExecId);
END
