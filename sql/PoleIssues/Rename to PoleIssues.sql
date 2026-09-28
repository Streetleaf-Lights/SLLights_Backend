-- Renames PoleOpenIssues to PoleIssues now that the Airtable view includes
-- both Open and Closed issues. The loader (loadPoleIssues) upserts all
-- valid records; IsOpenIssueFault on PoleTelemetry is driven by a live
-- WHERE Status = 'Open' filter at ingestion time, not by the table
-- only holding open records.

-- Drop the index (must be done before rename)
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_PoleOpenIssues_PoleId'
           AND object_id = OBJECT_ID('dbo.PoleOpenIssues'))
    DROP INDEX IX_PoleOpenIssues_PoleId ON dbo.PoleOpenIssues;

-- Rename the table
EXEC sp_rename 'dbo.PoleOpenIssues', 'PoleIssues';

-- Rename the PK constraint
DECLARE @pk NVARCHAR(256) = (
    SELECT name FROM sys.key_constraints
    WHERE parent_object_id = OBJECT_ID('dbo.PoleIssues') AND type = 'PK'
);
IF @pk IS NOT NULL
    EXEC('ALTER TABLE dbo.PoleIssues DROP CONSTRAINT ' + @pk);

EXEC('ALTER TABLE dbo.PoleIssues ADD CONSTRAINT PK_PoleIssues PRIMARY KEY (Id);');

-- Rename the CHECK constraint
DECLARE @ck NVARCHAR(256) = (
    SELECT name FROM sys.check_constraints
    WHERE parent_object_id = OBJECT_ID('dbo.PoleIssues')
);
IF @ck IS NOT NULL
    EXEC('ALTER TABLE dbo.PoleIssues DROP CONSTRAINT ' + @ck);

ALTER TABLE dbo.PoleIssues
    ADD CONSTRAINT CK_PoleIssues_PoleStatus
    CHECK (PoleStatus IN ('Electrical Issue', 'Structural Issue'));

-- Recreate the index under the new name
CREATE NONCLUSTERED INDEX IX_PoleIssues_PoleId ON dbo.PoleIssues (PoleId);

-- Add an index on Status to make the WHERE Status = 'Open' query
-- in _fetch_pole_ids_with_open_issues efficient
CREATE NONCLUSTERED INDEX IX_PoleIssues_Status ON dbo.PoleIssues (Status);
