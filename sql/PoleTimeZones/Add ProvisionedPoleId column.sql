-- Restructures PoleTimeZones to support both Leadsun poles (keyed by
-- VendorPoleId) and provisioned poles (keyed by ProvisionedPoleId), which
-- have no VendorPoleId (it is NULL).
--
-- Changes:
--   1. Add surrogate identity PK (Id) so rows can exist with VendorPoleId NULL.
--   2. Drop the existing PRIMARY KEY on VendorPoleId.
--   3. Make VendorPoleId nullable (was NOT NULL as part of PK).
--   4. Add UNIQUE index on VendorPoleId (WHERE NOT NULL) -- preserves the
--      "one row per Leadsun pole" invariant without a NOT NULL constraint.
--   5. Add ProvisionedPoleId NVARCHAR(64) NULL column.
--   6. Add UNIQUE index on ProvisionedPoleId (WHERE NOT NULL) -- same
--      invariant for provisioned poles.
--   7. Restore the SP_ExecId non-clustered index (dropped with the old PK
--      rebuild isn't needed since it's on a separate column, kept as-is).
--
-- All wrapped in IF NOT EXISTS guards so the script is idempotent and safe
-- to re-run.
--
-- EXEC pattern for all DDL that references the new columns: SQL Server
-- parses and validates column references for the entire batch before
-- executing any statement, so any reference to a new column (including
-- in a WHERE filter of a CREATE INDEX) must be deferred via EXEC to
-- avoid "Invalid column name" at parse time.

-- 1. Add surrogate identity PK if not already present
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTimeZones') AND name = 'Id'
)
BEGIN
    ALTER TABLE PoleTimeZones ADD Id INT IDENTITY(1,1) NOT NULL;
END

-- 2. Drop the existing PRIMARY KEY on VendorPoleId so we can make it nullable
IF EXISTS (
    SELECT 1 FROM sys.key_constraints
    WHERE parent_object_id = OBJECT_ID('PoleTimeZones')
      AND type = 'PK'
      AND name = 'PK__PoleTime__DC22FDDA5E5FF9D6'  -- auto-generated name; guard below handles any name
)
BEGIN
    EXEC('ALTER TABLE PoleTimeZones DROP CONSTRAINT PK__PoleTime__DC22FDDA5E5FF9D6;');
END

-- Drop whatever the actual PK constraint is named (handles auto-generated names)
DECLARE @pkName NVARCHAR(256);
SELECT @pkName = kc.name
FROM sys.key_constraints kc
WHERE kc.parent_object_id = OBJECT_ID('PoleTimeZones')
  AND kc.type = 'PK';

IF @pkName IS NOT NULL AND @pkName NOT LIKE 'PK_PoleTimeZones_Id'
BEGIN
    EXEC('ALTER TABLE PoleTimeZones DROP CONSTRAINT ' + @pkName + ';');
END

-- 3. Add the new PK on Id
IF NOT EXISTS (
    SELECT 1 FROM sys.key_constraints
    WHERE parent_object_id = OBJECT_ID('PoleTimeZones')
      AND type = 'PK'
)
BEGIN
    EXEC('ALTER TABLE PoleTimeZones ADD CONSTRAINT PK_PoleTimeZones_Id PRIMARY KEY CLUSTERED (Id);');
END

-- 4. Make VendorPoleId nullable (requires rebuilding column default -- it had none)
-- SQL Server doesn't support ALTER COLUMN to remove NOT NULL directly when
-- the column was a PK; now that the PK is dropped it is straightforward.
IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTimeZones')
      AND name = 'VendorPoleId'
      AND is_nullable = 0
)
BEGIN
    EXEC('ALTER TABLE PoleTimeZones ALTER COLUMN VendorPoleId NVARCHAR(100) NULL;');
END

-- 5. Unique index on VendorPoleId (filtered: WHERE NOT NULL)
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTimeZones') AND name = 'UX_PoleTimeZones_VendorPoleId'
)
BEGIN
    EXEC('
        CREATE UNIQUE NONCLUSTERED INDEX UX_PoleTimeZones_VendorPoleId
            ON PoleTimeZones (VendorPoleId)
            WHERE VendorPoleId IS NOT NULL;
    ');
END

-- 6. Add ProvisionedPoleId column
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleTimeZones') AND name = 'ProvisionedPoleId'
)
BEGIN
    EXEC('ALTER TABLE PoleTimeZones ADD ProvisionedPoleId NVARCHAR(64) NULL;');
END

-- 7. Unique index on ProvisionedPoleId (filtered: WHERE NOT NULL)
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('PoleTimeZones') AND name = 'UX_PoleTimeZones_ProvisionedPoleId'
)
BEGIN
    EXEC('
        CREATE UNIQUE NONCLUSTERED INDEX UX_PoleTimeZones_ProvisionedPoleId
            ON PoleTimeZones (ProvisionedPoleId)
            WHERE ProvisionedPoleId IS NOT NULL;
    ');
END;
