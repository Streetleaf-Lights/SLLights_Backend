-- Removes 'LastKnown48Hours' from the set of PeriodType values
-- PoleVitals will accept for any NEW row going forward -- by explicit
-- request/correction. Only 'Hour' and 'Last48Hours' remain permitted.
-- This is the migration for an EXISTING, already-deployed database --
-- see "Create tbl PoleVitals.sql" in this same folder for the
-- equivalent, already-tightened definition a brand NEW environment gets
-- automatically (that CREATE script's own IF NOT EXISTS guard means it
-- does nothing at all against a table that already exists, so it can't
-- apply this same change to a live database on its own -- this
-- migration is what actually does that).
--
-- Run this AFTER deploying the updated shared/pole_vitals_loader.py
-- (which no longer computes LastKnown48Hours at all -- see that
-- module's own docstring) and shared/pole_vitals_api.py/poles_api.py
-- (which no longer read it either -- a silent pole's per-pole detail
-- fields now come back null, via the same single Last48Hours join
-- already used for isOnline, instead of falling back to a persisted
-- last-known value). Running this migration before those code changes
-- deploy would break nothing immediately (LastKnown48Hours rows already
-- present are untouched -- see below), but there's no reason to
-- sequence it that way either.
--
-- Existing historical rows with PeriodType = 'LastKnown48Hours' are
-- DELIBERATELY LEFT IN PLACE, by the same explicit convention as this
-- table's own earlier "Remove Day Week and Month PeriodTypes.sql"
-- migration -- this migration only prevents NEW rows with that value,
-- it does not touch any existing ones. WITH NOCHECK below is what makes
-- that possible: it adds the constraint WITHOUT validating
-- already-present rows against it, so existing LastKnown48Hours rows
-- don't cause this ALTER TABLE itself to fail, while any NEW row SQL
-- Server evaluates against the constraint from this point forward still
-- must satisfy it. If those old rows are never needed again, a separate,
-- explicit DELETE FROM PoleVitals WHERE PeriodType = 'LastKnown48Hours'
-- is a deliberate follow-up decision, not something this migration
-- performs on its own.
--
-- One consequence of WITH NOCHECK worth knowing: SQL Server marks a
-- constraint added this way as NOT TRUSTED (visible via
-- sys.check_constraints.is_not_trusted = 1) until it's separately
-- validated -- this doesn't weaken enforcement for NEW rows at all (that
-- part is fully active immediately), it just means the query optimizer
-- won't assume every EXISTING row already satisfies the constraint when
-- planning a query. Given this table's own modest, bounded row counts
-- and the deliberate choice to leave old rows in place rather than
-- validate/clean them up, this is an acceptable, known tradeoff -- same
-- as the prior Day/Week/Month migration accepted for the same reason.
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

-- 2. Recreate it WITH NOCHECK, removing 'LastKnown48Hours' from the
-- allowed set -- existing rows with that value are left untouched and
-- do NOT cause this statement to fail, per the reasoning above.
ALTER TABLE PoleVitals WITH NOCHECK ADD CONSTRAINT CK_PoleVitals_PeriodType
    CHECK (PeriodType IN ('Hour', 'Last48Hours'));
