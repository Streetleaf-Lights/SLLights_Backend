-- Two additions to the existing Users table, needed for the new
-- user-management endpoints (shared/users_management_api.py):
--
-- 1. 'Deactivated' as a valid Status value, for delete_user(). This
--    project prefers soft deletes where a status lifecycle already
--    exists (Users already has 'Pending' -> 'Active') -- deactivating
--    preserves the row (audit trail, referential integrity with
--    anything that references UserId later) rather than removing it
--    outright. If a hard delete is what's actually wanted instead, skip
--    this migration and have delete_user() issue a DELETE statement.
--
-- 2. An index on ResetToken, since register_user()/reset_password() both
--    look a user up BY that token's value (not by Id) -- without an
--    index, that's a full table scan on every register/reset attempt.
--
-- The Status CHECK constraint's exact name isn't assumed here (this
-- project didn't have that DDL available when this migration was
-- written) -- found dynamically by inspecting its definition instead of
-- a hardcoded name that might not match. If your Status column has no
-- CHECK constraint at all, the first block below is a no-op (safe) and
-- you can just start writing 'Deactivated' directly.

DECLARE @ConstraintName NVARCHAR(128);
SELECT @ConstraintName = cc.name
FROM sys.check_constraints cc
JOIN sys.columns col
    ON col.object_id = cc.parent_object_id
    AND col.column_id = (
        SELECT TOP 1 column_id FROM sys.columns
        WHERE object_id = cc.parent_object_id AND name = 'Status'
    )
WHERE cc.parent_object_id = OBJECT_ID('Users')
  AND cc.definition LIKE '%Pending%'
  AND cc.definition LIKE '%Active%'
  AND cc.definition NOT LIKE '%Deactivated%';

IF @ConstraintName IS NOT NULL
BEGIN
    DECLARE @Sql NVARCHAR(MAX) = N'ALTER TABLE Users DROP CONSTRAINT ' + QUOTENAME(@ConstraintName) + N';';
    EXEC sp_executesql @Sql;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID('Users')
      AND definition LIKE '%Deactivated%'
)
BEGIN
    ALTER TABLE Users
        ADD CONSTRAINT CK_Users_Status
        CHECK (Status IN ('Pending', 'Active', 'Deactivated'));
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('Users') AND name = 'IX_Users_ResetToken'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_Users_ResetToken
        ON Users (ResetToken);
END
