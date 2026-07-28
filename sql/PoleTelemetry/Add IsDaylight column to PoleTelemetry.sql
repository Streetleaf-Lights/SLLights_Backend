-- One-time migration for environments where PoleTelemetry already exists
-- without the IsDaylight column (added when pole_daylight_flags_loader.py
-- was introduced). Safe to re-run -- both additions are guarded.
--
-- After running this, pole_daylight_flags_loader.py will treat every
-- existing row as unflagged (IsDaylight IS NULL) and backfill them
-- incrementally over subsequent runs, the same way PoleTimeZones
-- backfills newly-discovered LocationIds.
--
-- The GO between the two blocks below is required, not stylistic: SQL
-- Server compiles a whole batch before executing any of it, and a column
-- just added by ALTER TABLE isn't visible to a CREATE INDEX referencing
-- it later in that SAME batch ("Invalid column name" at compile time,
-- even though the ALTER TABLE itself would have succeeded). GO forces
-- the ALTER TABLE to run as its own batch first, so the CREATE INDEX
-- batch compiles against the table's already-updated column list.

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
