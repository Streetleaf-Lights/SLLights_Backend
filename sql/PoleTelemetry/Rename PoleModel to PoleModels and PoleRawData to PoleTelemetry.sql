-- One-time migration: rename the live PoleModel -> PoleModels and
-- PoleRawData -> PoleTelemetry tables (plus their indexes) to match the
-- renamed application code. Run this ONCE per environment (Dev/Staging/
-- Prod) BEFORE deploying the renamed code -- the code now refers to
-- "PoleModels" and "PoleTelemetry" by name in its MERGE/INSERT/DELETE
-- statements, so it will fail with an "invalid object name" error against
-- a database that still has the old table names.
--
-- Safe to run more than once: each rename is guarded so it only fires if
-- the OLD name still exists (i.e. this hasn't already been applied).
--
-- Unlike the guarded "Create tbl X.sql" scripts (which represent the
-- desired state for a FRESH database that doesn't have these tables at
-- all), this script is a one-time transition step for databases that
-- already have data in the old-named tables -- sp_rename preserves all
-- existing rows, it just changes the object's name.

-- ---------------------------------------------------------------------
-- PoleModel -> PoleModels
-- ---------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleModel')
   AND NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleModels')
BEGIN
    EXEC sp_rename 'PoleModel', 'PoleModels';

    -- Index rename uses 'table.index' format (index names are only
    -- unique per table, not database-wide) -- and needs the NEW table
    -- name, since sp_rename above already applied.
    IF EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'IX_PoleModel_SP_ExecId' AND object_id = OBJECT_ID('PoleModels')
    )
        EXEC sp_rename 'PoleModels.IX_PoleModel_SP_ExecId', 'IX_PoleModels_SP_ExecId', 'INDEX';
END

-- ---------------------------------------------------------------------
-- PoleRawData -> PoleTelemetry
-- ---------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleRawData')
   AND NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleTelemetry')
BEGIN
    EXEC sp_rename 'PoleRawData', 'PoleTelemetry';

    IF EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'IX_PoleRawData_LastUpload' AND object_id = OBJECT_ID('PoleTelemetry')
    )
        EXEC sp_rename 'PoleTelemetry.IX_PoleRawData_LastUpload', 'IX_PoleTelemetry_LastUpload', 'INDEX';

    IF EXISTS (
        SELECT 1 FROM sys.indexes
        WHERE name = 'IX_PoleRawData_SP_ExecId' AND object_id = OBJECT_ID('PoleTelemetry')
    )
        EXEC sp_rename 'PoleTelemetry.IX_PoleRawData_SP_ExecId', 'IX_PoleTelemetry_SP_ExecId', 'INDEX';
END
