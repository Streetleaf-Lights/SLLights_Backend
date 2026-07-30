-- UserSessions: backs real, immediate sign-out for the JWT-based auth
-- system (shared/auth_utils.py). A signed JWT access token alone can't be
-- "un-issued" once handed to a client -- it stays cryptographically valid
-- until it naturally expires. This table is what makes Sign Out actually
-- do something: each session gets a row here (keyed by the token's own
-- "jti" claim), and every authenticated request checks this table
-- (RevokedAt IS NULL, not expired) in addition to the JWT's own
-- signature/expiry check. Sign Out sets RevokedAt; nothing deletes rows
-- outright, so there's a natural audit trail of session history.
--
-- One row per (device/browser) session, not per user -- a user signed in
-- on both their laptop and phone gets two independent rows, and signing
-- out of one doesn't affect the other.
--
-- No FK to Users -- same reasoning as the rest of this schema: this
-- project doesn't enforce FKs where they'd complicate load order, and
-- UserId here is only ever written by sign_in() right after verifying
-- that user exists, so it's not at real risk of pointing at nothing.

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'UserSessions')
BEGIN
    CREATE TABLE UserSessions (
        Id         VARCHAR(36)       NOT NULL PRIMARY KEY,  -- the JWT's own "jti" claim (a UUID)
        UserId     VARCHAR(50)       NOT NULL,
        CreatedAt  DATETIMEOFFSET(3) NOT NULL,
        ExpiresAt  DATETIMEOFFSET(3) NOT NULL,
        RevokedAt  DATETIMEOFFSET(3) NULL  -- NULL = still active; set by sign_out()
    );

    CREATE NONCLUSTERED INDEX IX_UserSessions_UserId
        ON UserSessions (UserId);  -- for "sign out of all my other sessions"-style queries later

    CREATE NONCLUSTERED INDEX IX_UserSessions_ExpiresAt
        ON UserSessions (ExpiresAt);  -- for a future cleanup job purging long-expired rows
END
