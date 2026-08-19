-- Allows 'User' as a Users.Role value -- a new, third role (see
-- shared/users_management_api.py's own _VALID_ROLES and invite_user()
-- for the full permission model this enables: 'Streetleaf Admin' can
-- invite any of the three roles; 'Customer Admin' can invite 'Customer
-- Admin'/'User' but not 'Streetleaf Admin', and only for their own
-- CustomerId; 'User' cannot invite at all).
--
-- Run this BEFORE deploying the updated users_management_api.py --
-- without it, every invite_user() call for role='User' will fail with a
-- CHECK constraint violation (547) the moment it tries to INSERT that
-- row, the same class of failure "sql/PoleVitals/Allow LastKnown48Hours
-- PeriodType.sql" already documents for an analogous case.
--
-- Unlike that migration's own situation (which had to discover its
-- constraint's name dynamically, since it wasn't known at the time),
-- CK_Users_Role's exact name is already known directly from
-- "Create tbl Users.sql" -- a plain drop-and-recreate by that known
-- name, same convention as the PoleVitals one, but simpler since there's
-- no name-discovery step needed here.
--
-- GO separators are required, not stylistic -- SQL Server compiles a
-- whole batch before executing any of it, so a constraint just dropped
-- by one ALTER TABLE isn't necessarily safe to recreate against in a
-- statement compiled in the same batch.

-- 1. Drop the existing CHECK constraint.
IF EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE object_id = OBJECT_ID('CK_Users_Role')
)
BEGIN
    ALTER TABLE Users DROP CONSTRAINT CK_Users_Role;
END
GO

-- 2. Recreate it, adding 'User' to the allowed set -- otherwise
-- identical to its previous definition.
ALTER TABLE Users ADD CONSTRAINT CK_Users_Role
    CHECK (Role IN ('Customer Admin', 'Streetleaf Admin', 'User'));
