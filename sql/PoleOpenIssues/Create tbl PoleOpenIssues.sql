-- PoleOpenIssues -- loaded from a specific Airtable view in a SEPARATE
-- Airtable base from the one Customers/Projects/Poles come from (see
-- shared/pole_open_issues_loader.py's own AIRTABLE_POLE_ISSUES_BASE_ID
-- notes). Only ever holds issues where Status = 'Open' and PoleStatus is
-- one of the two values below -- the loader itself both filters incoming
-- records to this set AND removes any existing row that no longer
-- matches (e.g. an issue that gets resolved in Airtable), so this table
-- should always reflect exactly what's currently open, not a
-- never-pruned history of everything that was ever open.

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleOpenIssues')
BEGIN
    CREATE TABLE PoleOpenIssues (
        Id         VARCHAR(50)   NOT NULL PRIMARY KEY,  -- Airtable's own record id
        IssueId    NVARCHAR(100) NULL,                   -- Airtable's own "IssueID" field
        PoleId     VARCHAR(50)   NULL,                   -- linked-record field -- should line up with Poles.Id
        Status     NVARCHAR(50)  NULL,
        PoleStatus NVARCHAR(50)  NULL,

        CONSTRAINT CK_PoleOpenIssues_PoleStatus
            CHECK (PoleStatus IN ('Electrical Issue', 'Structural Issue')),

        SP_ExecId  INT           NULL
    );

    -- Likely access pattern: "what open issues does this pole have" --
    -- e.g. a future getPoleOpenIssues?poleId=X endpoint, matching the
    -- pattern every other read endpoint in this project already follows.
    CREATE NONCLUSTERED INDEX IX_PoleOpenIssues_PoleId
        ON PoleOpenIssues (PoleId);
END
