-- PoleIssues -- loaded from a specific Airtable view in a SEPARATE
-- Airtable base from the one Customers/Projects/Poles come from (see
-- shared/pole_issues_loader.py's own AIRTABLE_POLE_ISSUES_BASE_ID notes).
-- Holds all issues with a valid PoleStatus (both Open and Closed) --
-- the loader upserts all valid records and removes rows no longer in
-- the Airtable view. IsOpenIssueFault on PoleTelemetry is driven by a
-- live WHERE Status = 'Open' filter at ingestion time, not by this
-- table only holding open records.

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleIssues')
BEGIN
    CREATE TABLE PoleIssues (
        Id         VARCHAR(50)   NOT NULL PRIMARY KEY,  -- Airtable's own record id
        IssueId    NVARCHAR(100) NULL,                   -- Airtable's own "IssueID" field
        PoleId     VARCHAR(50)   NULL,                   -- linked-record field -- lines up with Poles.Id
        Status     NVARCHAR(50)  NULL,                   -- 'Open' or 'Closed'
        PoleStatus NVARCHAR(50)  NULL,

        CONSTRAINT CK_PoleIssues_PoleStatus
            CHECK (PoleStatus IN ('Electrical Issue', 'Structural Issue')),

        SP_ExecId  INT           NULL
    );

    CREATE NONCLUSTERED INDEX IX_PoleIssues_PoleId
        ON PoleIssues (PoleId);

    -- Status index makes the WHERE Status = 'Open' filter in
    -- _fetch_pole_ids_with_open_issues() and the API EXISTS subqueries efficient.
    CREATE NONCLUSTERED INDEX IX_PoleIssues_Status
        ON PoleIssues (Status);
END;
