IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.PoleIssues') AND name = 'DateReported')
    ALTER TABLE dbo.PoleIssues ADD DateReported NVARCHAR(100) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.PoleIssues') AND name = 'ProblemDetails')
    ALTER TABLE dbo.PoleIssues ADD ProblemDetails NVARCHAR(MAX) NULL;
