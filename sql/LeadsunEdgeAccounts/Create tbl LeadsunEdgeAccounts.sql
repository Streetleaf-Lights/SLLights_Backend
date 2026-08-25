-- LeadsunEdgeAccounts: credentials for logging into the Leadsun Edge
-- system itself (a separate system from this project's own Users
-- table, which is for THIS application's own login). Loaded from a
-- one-off spreadsheet via scripts/load_leadsun_edge_accounts.py, NOT
-- synced from Airtable or the Leadsun telemetry API -- same reasoning
-- as Workweek being a non-ETL, no-Source/SP_ExecId table (see the now-
-- removed sql/Workweek/ for that precedent, or README.md's own note).
--
-- EncryptedPassword is genuinely encrypted (Fernet, via
-- shared/encryption_utils.py), NOT hashed -- these credentials need to
-- be RECOVERABLE later (an operator or automated process actually
-- logging into Leadsun Edge with them), unlike Users.PasswordHash
-- (bcrypt, one-way, only ever verified, never decrypted). VARCHAR(500)
-- comfortably fits a Fernet token for a password-length plaintext (a
-- token is roughly plaintext-length plus a fixed ~100-byte overhead for
-- the IV/timestamp/HMAC/base64 encoding), with headroom to spare.
--
-- Username as the PRIMARY KEY (a natural key, not a surrogate
-- Id/uniqueidentifier) -- confirmed unique in the source spreadsheet
-- (41 rows, 0 duplicates) and there's no other table in this project
-- with a foreign key pointing at a LeadsunEdgeAccounts row, so a
-- surrogate key would add complexity without buying anything here.
--
-- LoadedAt/UpdatedAt: LoadedAt is set once, at initial INSERT, and
-- never changes again -- UpdatedAt refreshes every time the loader
-- script's own MERGE updates an existing row (e.g. the spreadsheet is
-- re-run later with a changed password for an existing Username).
-- Together they answer "when did this account first appear" and "when
-- did its password last change" as two separate questions, without
-- needing a full audit-history table for a credential set this small.

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'LeadsunEdgeAccounts')
BEGIN
    CREATE TABLE LeadsunEdgeAccounts (
        Username          NVARCHAR(100)  NOT NULL,
        EncryptedPassword VARCHAR(500)   NOT NULL,
        LoadedAt          DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
        UpdatedAt         DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
        CONSTRAINT PK_LeadsunEdgeAccounts PRIMARY KEY (Username)
    );
END
