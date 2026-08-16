-- Allows 'LastKnown48Hours' as a PoleVitals.PeriodType value -- a new,
-- fourth period type (see shared/pole_vitals_loader.py's own comments on
-- _LAST_KNOWN_48_HOURS_COPY_FROM_LAST_48_HOURS_SQL and
-- _LAST_KNOWN_48_HOURS_FRESH_COMPUTE_FOR_OFFLINE_POLES_SQL for the full
-- design): identical to a currently-active pole's own Last48Hours row,
-- but persists with that pole's own last-known 48 hours of activity once
-- it goes silent, rather than disappearing the way Last48Hours itself
-- deliberately does.
--
-- Run this BEFORE deploying the updated pole_vitals_loader.py -- without
-- it, EVERY load_pole_vitals() run will fail its new LastKnown48Hours
-- step with a CHECK constraint violation (547) the moment it tries to
-- write a 'LastKnown48Hours' row, the same class of failure the two
-- earlier PeriodType migrations for 'Last48Hours' already document.
--
-- The column itself needs NO widening this time -- PeriodType is already
-- VARCHAR(20) (from "Widen PeriodType to fit Last48Hours.sql"), and
-- 'LastKnown48Hours' is 16 characters, comfortably within that. Only the
-- CHECK constraint itself needs updating, so this is a smaller, simpler
-- migration than that one -- no need to touch PK_PoleVitals or
-- IX_PoleVitals_PeriodType_PeriodStart, since neither of those actually
-- depends on the column's own width/type, only the CHECK constraint
-- does. SQL Server has no ALTER CHECK CONSTRAINT, so this is a plain
-- drop-and-recreate with the one new value added, same convention as
-- both earlier PeriodType migrations.
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a constraint just dropped
-- by one ALTER TABLE isn't necessarily safe to recreate against in a
-- statement compiled in the same batch.

-- 1. Drop the existing CHECK constraint.
IF EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE object_id = OBJECT_ID('CK_PoleVitals_PeriodType')
)
BEGIN
    ALTER TABLE PoleVitals DROP CONSTRAINT CK_PoleVitals_PeriodType;
END
GO

-- 2. Recreate it, adding 'LastKnown48Hours' to the allowed set --
-- otherwise identical to its previous definition, including the still-
-- open 'Week'/'Month' question from "Create tbl PoleVitals.sql"'s own
-- header comment (unrelated to this migration, not resolved here).
ALTER TABLE PoleVitals ADD CONSTRAINT CK_PoleVitals_PeriodType
    CHECK (PeriodType IN ('Hour', 'Day', 'Last48Hours', 'LastKnown48Hours', 'Week', 'Month'));
