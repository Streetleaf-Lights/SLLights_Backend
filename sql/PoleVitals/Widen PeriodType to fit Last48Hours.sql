-- Widens PoleVitals.PeriodType from VARCHAR(10) to VARCHAR(20).
--
-- 'Last48Hours' is 11 characters -- one character too long for the
-- original VARCHAR(10) column, sized back when only 'Hour'/'Day'/
-- 'Week'/'Month' (all 6 characters or fewer) existed. SQL Server was
-- silently truncating 'Last48Hours' to 10 characters ('Last48Hou') on
-- insert, and THAT truncated value then failed
-- CK_PoleVitals_PeriodType (since 'Last48Hou' isn't one of the allowed
-- strings, even though the full 'Last48Hours' is) -- surfacing as a
-- CHECK constraint violation (547) that looked unrelated to the real
-- root cause: a column too narrow for the new value.
--
-- PeriodType is part of PK_PoleVitals (LocationId, PeriodType,
-- PeriodStart) and has its own nonclustered index
-- (IX_PoleVitals_PeriodType_PeriodStart), so it can't be widened via a
-- plain ALTER COLUMN while either references it -- SQL Server requires
-- dropping both, plus the CHECK constraint, widening the column, then
-- recreating all three.
--
-- Run this AFTER confirming CK_PoleVitals_PeriodType already allows
-- 'Last48Hours' (it should, from the earlier "Replace LightStatus with
-- fault flags and allow Last48Hours.sql" migration) -- this migration
-- drops and recreates that same constraint with the identical
-- definition, so it's safe regardless of whether that step already ran.
--
-- ****************************************************************
-- WARNING: dropping and recreating PK_PoleVitals means dropping and
-- rebuilding its underlying clustered index (the default for a primary
-- key unless declared otherwise) -- this touches every row in the
-- table, not just newly-inserted ones. Smaller in scale than the
-- PoleTelemetry covering-index rebuild from an earlier migration (this
-- table holds aggregated Hour/Day/Last48Hours rows, not raw readings),
-- but still a real operation, not an instant one on a table of any
-- meaningful size.
-- ****************************************************************
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a constraint just dropped
-- by one ALTER TABLE isn't necessarily safe to recreate against in a
-- statement compiled in the same batch.

-- 1. Drop the CHECK constraint (references PeriodType).
IF EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE object_id = OBJECT_ID('CK_PoleVitals_PeriodType')
)
BEGIN
    ALTER TABLE PoleVitals DROP CONSTRAINT CK_PoleVitals_PeriodType;
END
GO

-- 2. Drop the nonclustered index (also references PeriodType).
IF EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IX_PoleVitals_PeriodType_PeriodStart'
)
BEGIN
    DROP INDEX IX_PoleVitals_PeriodType_PeriodStart ON PoleVitals;
END
GO

-- 3. Drop the primary key (PeriodType is part of its key columns).
IF EXISTS (
    SELECT 1 FROM sys.objects
    WHERE object_id = OBJECT_ID('PK_PoleVitals') AND type = 'PK'
)
BEGIN
    ALTER TABLE PoleVitals DROP CONSTRAINT PK_PoleVitals;
END
GO

-- 4. Widen the column itself.
ALTER TABLE PoleVitals ALTER COLUMN PeriodType VARCHAR(20) NOT NULL;
GO

-- 5. Recreate the primary key.
ALTER TABLE PoleVitals ADD CONSTRAINT PK_PoleVitals
    PRIMARY KEY (LocationId, PeriodType, PeriodStart);
GO

-- 6. Recreate the nonclustered index.
CREATE NONCLUSTERED INDEX IX_PoleVitals_PeriodType_PeriodStart
    ON PoleVitals (PeriodType, PeriodStart);
GO

-- 7. Recreate the CHECK constraint (same definition as before -- already
-- includes 'Last48Hours' from the earlier migration).
ALTER TABLE PoleVitals ADD CONSTRAINT CK_PoleVitals_PeriodType
    CHECK (PeriodType IN ('Hour', 'Day', 'Last48Hours', 'Week', 'Month'));
