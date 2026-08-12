-- Users: application login accounts (auth + password-reset support) --
-- NOT synced from Airtable or Leadsun, so unlike the ETL-pipeline tables
-- in this project it has no Source/SP_ExecId columns and no associated
-- loader/Azure Function -- same reasoning as Workweek being the other
-- non-ETL table here.
--
-- Decisions:
--   * PK column named "Id" (matching Customers.Id/Projects.Id/Poles.Id),
--     not "ID".
--   * Email has a UNIQUE constraint -- two accounts can't share an email.
--     SQL Server's UNIQUE constraint still allows multiple NULLs, so this
--     doesn't force every user to have one.
--   * CustomerId is a real FOREIGN KEY to Customers.Id, per an explicit
--     ask -- unlike every ETL table here, which skips FKs for load-order
--     practicality reasons that don't apply to this table. Changed to
--     VARCHAR(50) (from the originally-given NVARCHAR(50)) to exactly
--     match Customers.Id's type, since SQL Server wants matching types
--     for a clean FK. Stays NULLable: a "Streetleaf Admin" isn't scoped
--     to one customer, so NULL here means "not customer-scoped" -- a
--     NULL FK value is exempt from the constraint automatically.
--   * ResetTokenExpiresAt is DATETIMEOFFSET(3), matching every other
--     datetime column's precision in this project.
--   * CustomerName removed -- redundant now that CustomerId is a real FK;
--     join to Customers for the name instead of storing a copy that can
--     go stale. (Happy to add a scratch SELECT that joins them for
--     convenience, similar to PoleTelemetry/PoleModels, if useful.)
--   * Role and Status each get a CHECK constraint restricting them to the
--     currently-known values. Both stay NULLable -- a CHECK like
--     `IN (...)` doesn't reject NULL, only a non-NULL value outside the
--     list. If the value sets grow later, this constraint needs an
--     ALTER (drop and recreate) to add the new value.
--   * Status defaults to 'Pending' -- not explicitly requested, but
--     "Pending and Active" strongly implies new accounts start Pending
--     until approved/verified. Remove the DEFAULT if that's not the
--     intended flow.

-- DROP TABLE IF EXISTS Users;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'Users')
BEGIN
    CREATE TABLE Users (
        Id                  UNIQUEIDENTIFIER  NOT NULL DEFAULT NEWID(),
        Name                NVARCHAR(255)     NULL,
        Email               NVARCHAR(255)     NULL,
        PasswordHash        NVARCHAR(255)     NULL,
        Role                NVARCHAR(50)      NULL,
        Status              NVARCHAR(50)      NULL DEFAULT 'Pending',
        CustomerId          VARCHAR(50)       NULL,
        ResetToken          UNIQUEIDENTIFIER  NULL,
        ResetTokenExpiresAt DATETIMEOFFSET(3) NULL,
        CONSTRAINT PK_Users PRIMARY KEY (Id),
        CONSTRAINT UQ_Users_Email UNIQUE (Email),
        CONSTRAINT FK_Users_CustomerId FOREIGN KEY (CustomerId) REFERENCES Customers (Id),
        CONSTRAINT CK_Users_Role CHECK (Role IN ('Customer Admin', 'Streetleaf Admin')),
        CONSTRAINT CK_Users_Status CHECK (Status IN ('Pending', 'Active'))
    );

    CREATE NONCLUSTERED INDEX IX_Users_CustomerId
        ON Users (CustomerId);

    CREATE NONCLUSTERED INDEX IX_Users_ResetToken
        ON Users (ResetToken);
END
