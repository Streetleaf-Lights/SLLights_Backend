-- Replaces Customers.IsRemovedFromAirtable (a since-superseded column
-- name/migration) with Customers.Active -- per explicit request, so the
-- same column reads sensibly if a future source OTHER than Airtable
-- ever needs to reconcile against this same table (a column literally
-- named "IsRemovedFromAirtable" wouldn't make sense once/if that
-- happens). If you already ran "Add IsRemovedFromAirtable column.sql"
-- against this database, this migration DROPS that column entirely --
-- its data is NOT migrated forward automatically (see below for why,
-- and what to do about it).
--
-- The SEMANTICS are also inverted, not just the name: IsRemovedFromAirtable
-- = 1 meant "no longer present"; Active = 1 means "present" -- the
-- opposite polarity. Set by shared/airtable_removal_utils.
-- flag_records_removed_from_airtable(), called once per run from
-- load_customers(), AFTER that run's own upsert phase has fully
-- completed -- see that function's own docstring for the full
-- reasoning (in particular: why this can't just be a WHEN NOT MATCHED
-- BY SOURCE clause tacked onto Customers' own existing MERGE, and the
-- safety guard against an empty/failed Airtable fetch mass-marking
-- every existing row inactive).
--
-- On data loss: rather than write one-off conversion logic to carry
-- the old column's own (inverted) values forward into the new column,
-- this migration relies on the NEXT loadCustomers run to correctly
-- repopulate Active from a fresh Airtable fetch -- functionally
-- equivalent to what the old column held (just inverted), recovered
-- automatically on the very next scheduled sync, not lost permanently.
--
-- BIT NOT NULL DEFAULT 1: every existing row gets 1 (active) the
-- moment this column is added -- correct as a starting assumption,
-- since the very next loadCustomers run will immediately re-evaluate
-- every row's own real status against a fresh Airtable fetch anyway.
-- Note this default is the OPPOSITE of IsRemovedFromAirtable's own
-- DEFAULT 0 -- not a typo, a direct consequence of the inverted
-- polarity: "not yet evaluated" now means "assume active" rather than
-- "assume not removed", which are the same starting assumption
-- expressed through opposite column semantics.
--
-- Run this BEFORE deploying the updated shared/customers_loader.py --
-- otherwise loadCustomers will fail every run the moment it tries to
-- write to a column that doesn't exist yet, the same class of failure
-- this project's own earlier column-addition migrations already
-- document.
--
-- Safe to re-run.

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Customers') AND name = 'IsRemovedFromAirtable'
)
BEGIN
    ALTER TABLE Customers DROP COLUMN IsRemovedFromAirtable;
END

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Customers') AND name = 'Active'
)
BEGIN
    ALTER TABLE Customers ADD Active BIT NOT NULL DEFAULT 1;
END
