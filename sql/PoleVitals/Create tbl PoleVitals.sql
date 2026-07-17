-- PoleVitals: rolling averages of pole health metrics (battery, solar
-- panel, light output) derived FROM PoleTelemetry + PoleModels, bucketed
-- into Hour/Day/Week/Month periods per LocationId.
--
-- Unlike every other table in this project, this one isn't synced from an
-- external API directly -- it's computed FROM already-loaded data
-- (PoleTelemetry joined with PoleModels, and Workweek for the Week
-- bucketing). See shared/pole_vitals_loader.py for the actual aggregation
-- SQL and shared/datetime_utils.py's Eastern-time convention (bucketing
-- uses Eastern wall-clock time, not raw UTC).
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
-- PeriodEnd is EXCLUSIVE (the start of the next period), e.g. an Hour
-- bucket's PeriodEnd is exactly PeriodStart + 1 hour -- this differs from
-- Workweek's own EndDate convention (inclusive, the Saturday itself), a
-- deliberate choice since exclusive bounds are simpler for range queries
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
        PeriodType           VARCHAR(10)       NOT NULL,  -- 'Hour', 'Day', 'Week', or 'Month'
        PeriodStart          DATETIMEOFFSET(3) NOT NULL,
        PeriodEnd            DATETIMEOFFSET(3) NOT NULL,  -- exclusive -- see note above
        AvgBatteryPercentage FLOAT             NULL,
        AvgPanelPercentage   FLOAT             NULL,
        AvgLightPercentage   FLOAT             NULL,
        RecordCount          INT               NOT NULL,  -- how many telemetry readings fed this average
        Source               VARCHAR(50)       NOT NULL,
        SP_ExecId            INT               NULL,
        CONSTRAINT PK_PoleVitals PRIMARY KEY (LocationId, PeriodType, PeriodStart),
        CONSTRAINT CK_PoleVitals_PeriodType CHECK (PeriodType IN ('Hour', 'Day', 'Week', 'Month'))
    );

    CREATE NONCLUSTERED INDEX IX_PoleVitals_PeriodType_PeriodStart
        ON PoleVitals (PeriodType, PeriodStart);  -- for "all locations, this period" queries

    CREATE NONCLUSTERED INDEX IX_PoleVitals_SP_ExecId
        ON PoleVitals (SP_ExecId);
END
