IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.PoleIssues') AND name = 'IssueSource')
    ALTER TABLE dbo.PoleIssues ADD IssueSource NVARCHAR(100) NULL;
