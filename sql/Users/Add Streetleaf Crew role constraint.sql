-- Updates the Users.Role CHECK constraint to include 'Streetleaf Crew'.
-- Run BEFORE deploying the code change and before running
-- 'Add Streetleaf Crew user.sql'.

DECLARE @ck NVARCHAR(256) = (
    SELECT name FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID('dbo.Users')
      AND name LIKE '%Role%'
);
IF @ck IS NOT NULL
    EXEC('ALTER TABLE dbo.Users DROP CONSTRAINT ' + @ck);

ALTER TABLE dbo.Users
    ADD CONSTRAINT CK_Users_Role
    CHECK (Role IN ('Customer Admin', 'Customer Owner', 'Streetleaf Admin', 'Streetleaf Crew', 'User'));
