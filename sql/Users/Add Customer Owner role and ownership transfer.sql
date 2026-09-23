-- Adds 'Customer Owner' to the Users.Role CHECK constraint and adds three
-- columns for the ownership transfer flow:
--   OwnershipTransferToken           UNIQUEIDENTIFIER NULL
--   OwnershipTransferTokenExpiresAt  DATETIMEOFFSET(3) NULL
--   OwnershipTransferFromUserId      VARCHAR(36) NULL  (the current owner's Id)
--
-- The three transfer columns reuse the same token-lifecycle pattern as
-- ResetToken/ResetTokenExpiresAt but are kept separate so an in-flight
-- ownership transfer doesn't collide with a concurrent password reset
-- on the same nominee's account.

-- 1. Drop the existing Role CHECK constraint
DECLARE @RoleConstraint NVARCHAR(256) = (
    SELECT name FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID('dbo.Users')
      AND name LIKE '%Role%'
);
IF @RoleConstraint IS NOT NULL
    EXEC('ALTER TABLE dbo.Users DROP CONSTRAINT ' + @RoleConstraint);

-- 2. Add the updated CHECK constraint including Customer Owner
ALTER TABLE dbo.Users
    ADD CONSTRAINT CK_Users_Role
    CHECK (Role IN ('Customer Admin', 'Customer Owner', 'Streetleaf Admin', 'User'));

-- 3. Add ownership transfer columns
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Users') AND name = 'OwnershipTransferToken')
    ALTER TABLE dbo.Users ADD OwnershipTransferToken UNIQUEIDENTIFIER NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Users') AND name = 'OwnershipTransferTokenExpiresAt')
    ALTER TABLE dbo.Users ADD OwnershipTransferTokenExpiresAt DATETIMEOFFSET(3) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Users') AND name = 'OwnershipTransferFromUserId')
    ALTER TABLE dbo.Users ADD OwnershipTransferFromUserId VARCHAR(36) NULL;
