-- Removes 'Day', 'Week', and 'Month' from the set of PeriodType values
-- PoleVitals will accept for any NEW row going forward -- by explicit
-- request. Only 'Hour', 'Last48Hours', and 'LastKnown48Hours' remain
-- permitted. This is the migration for an EXISTING, already-deployed
-- database -- see "Create tbl PoleVitals.sql" in this same folder for
-- the equivalent, already-tightened definition a brand NEW environment
-- gets automatically (that CREATE script's own IF NOT EXISTS guard means
-- it does nothing at all against a table that already exists, so it
-- can't apply this same change to a live database on its own -- this
-- migration is what actually does that).
--
-- Run this AFTER deploying the updated pole_vitals_loader.py (which no
-- longer computes 'Day' at all -- 'Week'/'Month' were already dropped
-- from active computation well before this migration, in an earlier,
-- separate change) -- otherwise this loader will start failing every
-- run the moment it tries to write a 'Day' row, the same class of
-- CHECK-constraint-violation (547) failure this project's own earlier
-- PeriodType migrations already document for the opposite direction
-- (adding a new value the loader had already started trying to write).
--
-- Existing historical rows with PeriodType IN ('Day', 'Week', 'Month')
-- are DELIBERATELY LEFT IN PLACE, by explicit request -- this migration
-- only prevents NEW rows with those values, it does not touch any
-- existing ones. This is exactly the situation "Create tbl PoleVitals.sql"'s
-- own much earlier version anticipated ("if any existing rows with
-- PeriodType IN ('Week', 'Month') are still in this table, a tightened
-- constraint would need those rows dealt with first (deleted, or the
-- constraint added WITH NOCHECK)") -- WITH NOCHECK below is that exact
-- mechanism: it adds the constraint WITHOUT validating already-present
-- rows against it, so existing 'Day'/'Week'/'Month' rows are left alone
-- and don't cause this ALTER TABLE itself to fail, while any NEW row
-- SQL Server evaluates against the constraint from this point forward
-- still must satisfy it.
--
-- One consequence of WITH NOCHECK worth knowing: SQL Server marks a
-- constraint added this way as NOT TRUSTED (visible via
-- sys.check_constraints.is_not_trusted = 1) until it's separately
-- validated -- this doesn't weaken enforcement for NEW rows at all (that
-- part is fully active immediately), it just means the query optimizer
-- won't assume every EXISTING row already satisfies the constraint when
-- planning a query, which could very rarely affect a plan's own
-- assumptions for a query that specifically filters on PeriodType.
-- Given this table's own modest, bounded row counts and the deliberate
-- choice to leave old rows in place rather than validate/clean them up,
-- this is an acceptable, known tradeoff -- not something this migration
-- attempts to resolve further.
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

-- 2. Recreate it WITH NOCHECK, removing 'Day'/'Week'/'Month' from the
-- allowed set -- existing rows with those values are left untouched and
-- do NOT cause this statement to fail, per the reasoning above.
ALTER TABLE PoleVitals WITH NOCHECK ADD CONSTRAINT CK_PoleVitals_PeriodType
    CHECK (PeriodType IN ('Hour', 'Last48Hours', 'LastKnown48Hours'));
