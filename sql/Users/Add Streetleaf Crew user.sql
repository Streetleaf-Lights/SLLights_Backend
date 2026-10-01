-- Adds a Streetleaf Crew user. Run AFTER deploying the code that adds
-- 'Streetleaf Crew' to the Role CHECK constraint.
--
-- Replace the placeholder values before running:
--   @Name        -- display name, e.g. 'Streetleaf Crew'
--   @Email       -- login email, e.g. 'crew@streetleaf.com'
--   @Password    -- plain-text password to hash (use bcrypt externally
--                   and paste the hash, or set a ResetToken and use the
--                   forgot-password flow to let the user set their own)
--
-- Streetleaf Crew: same read access as User but scoped to ALL customers
-- with CrewAssigned = 1, regardless of CustomerId. No CustomerId is set
-- on this role (it's Streetleaf-wide, not customer-scoped).

DECLARE @Name     NVARCHAR(100) = 'Streetleaf Crew';
DECLARE @Email    NVARCHAR(200) = 'crew@streetleaf.com';
DECLARE @NewId    UNIQUEIDENTIFIER = NEWID();

-- Insert as Pending so the user must set their password via the invite flow.
-- A ResetToken is generated here; send the registration link to the user.
DECLARE @Token UNIQUEIDENTIFIER = NEWID();
DECLARE @Expires DATETIMEOFFSET(3) = DATEADD(DAY, 7, SYSDATETIMEOFFSET());

INSERT INTO Users (Id, Name, Email, Role, Status, CustomerId, PasswordHash, ResetToken, ResetTokenExpiresAt)
VALUES (
    @NewId,
    @Name,
    @Email,
    'Streetleaf Crew',
    'Pending',
    NULL,           -- no CustomerId; Streetleaf Crew is not customer-scoped
    NULL,
    @Token,
    @Expires
);

-- Also update the Role CHECK constraint to include Streetleaf Crew
-- (run this first if you haven't deployed the code change yet):
--
-- DECLARE @ck NVARCHAR(256) = (
--     SELECT name FROM sys.check_constraints
--     WHERE parent_object_id = OBJECT_ID('dbo.Users')
--       AND name LIKE '%Role%'
-- );
-- IF @ck IS NOT NULL EXEC('ALTER TABLE dbo.Users DROP CONSTRAINT ' + @ck);
-- ALTER TABLE dbo.Users ADD CONSTRAINT CK_Users_Role
--     CHECK (Role IN ('Customer Admin', 'Customer Owner', 'Streetleaf Admin', 'Streetleaf Crew', 'User'));

SELECT
    @NewId      AS NewUserId,
    @Email      AS Email,
    @Token      AS RegistrationToken,
    @Expires    AS TokenExpiresAt;
