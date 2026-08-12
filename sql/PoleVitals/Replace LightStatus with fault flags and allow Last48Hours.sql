-- Replaces PoleVitals.LightStatus (Daylight-based Working/DayLight/Not
-- Working classification) with five fault-flag columns (IsLedFault/
-- IsBatteryFault/IsPanelFault/IsOpenIssueFault/IsPoleFault -- see
-- shared/pole_vitals_loader.py's own module docstring for the full
-- design), and allows 'Last48Hours' as a PeriodType (a new, third period
-- type -- a single continuously-updated rolling window per pole, unlike
-- Hour/Day's discrete historical buckets; see that same module's
-- comments on _LAST_48_HOURS_MERGE_SQL for why it's structured
-- differently).
--
-- Run this AFTER deploying the updated pole_vitals_loader.py (which no
-- longer reads or writes LightStatus, and now writes the five new fault
-- columns plus 'Last48Hours' rows) -- otherwise it will fail every run
-- trying to write to columns that don't exist yet, or violate the old
-- CK_PoleVitals_PeriodType constraint when it tries to insert a
-- 'Last48Hours' row.
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a column just added/dropped
-- by one ALTER TABLE isn't necessarily safe to reference in a CHECK
-- constraint compiled in the same batch.
--
-- CK_PoleVitals_PeriodType must be dropped and recreated, not ALTERed in
-- place -- SQL Server has no ALTER CHECK CONSTRAINT. The existing
-- constraint already allows 'Week'/'Month' even though they're no longer
-- computed (an open decision from an earlier migration, unrelated to
-- this one -- see "Create tbl PoleVitals.sql" for that history); this
-- migration only adds 'Last48Hours' to that same set, without resolving
-- the separate Week/Month question.

-- 1. Add the five new fault-flag columns.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IsLedFault'
)
BEGIN
    ALTER TABLE PoleVitals ADD IsLedFault BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IsBatteryFault'
)
BEGIN
    ALTER TABLE PoleVitals ADD IsBatteryFault BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IsPanelFault'
)
BEGIN
    ALTER TABLE PoleVitals ADD IsPanelFault BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IsOpenIssueFault'
)
BEGIN
    ALTER TABLE PoleVitals ADD IsOpenIssueFault BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IsPoleFault'
)
BEGIN
    ALTER TABLE PoleVitals ADD IsPoleFault BIT NULL;
END
GO

-- 2. Drop the old LightStatus CHECK constraint and column -- must drop
-- the constraint first, or the column drop fails.
IF EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE object_id = OBJECT_ID('CK_PoleVitals_LightStatus')
)
BEGIN
    ALTER TABLE PoleVitals DROP CONSTRAINT CK_PoleVitals_LightStatus;
END
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'LightStatus'
)
BEGIN
    ALTER TABLE PoleVitals DROP COLUMN LightStatus;
END
GO

-- 3. Drop and recreate CK_PoleVitals_PeriodType to also allow
-- 'Last48Hours' -- SQL Server has no ALTER CHECK CONSTRAINT.
IF EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE object_id = OBJECT_ID('CK_PoleVitals_PeriodType')
)
BEGIN
    ALTER TABLE PoleVitals DROP CONSTRAINT CK_PoleVitals_PeriodType;
END
GO

ALTER TABLE PoleVitals ADD CONSTRAINT CK_PoleVitals_PeriodType
    CHECK (PeriodType IN ('Hour', 'Day', 'Last48Hours', 'Week', 'Month'));
