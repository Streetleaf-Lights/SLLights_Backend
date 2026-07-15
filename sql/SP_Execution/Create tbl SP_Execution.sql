
-- DROP TABLE IF EXISTS SP_Execution;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'SP_Execution')
BEGIN

    CREATE TABLE SP_Execution (
        Id                      INT IDENTITY(1,1) NOT NULL,
        Name                    NVARCHAR(255)       NOT NULL,
        Environment             NVARCHAR(50)        NOT NULL,
        StartDateTime           DATETIMEOFFSET(3)   NOT NULL,
        EndDateTime             DATETIMEOFFSET(3)   NULL,
        TotalSuccessfulRecords  INT                 NOT NULL DEFAULT 0,
        TotalErrorRecords       INT                 NOT NULL DEFAULT 0,
        Source                  NVARCHAR(255)       NULL,
        BatchCount              INT                 NOT NULL DEFAULT 0,
        IsFinalBatch            BIT                 NOT NULL DEFAULT 0,
        ErrorMessage            NVARCHAR(MAX)       NULL,

        CONSTRAINT PK_SP_Execution PRIMARY KEY NONCLUSTERED (Id)
    );

    CREATE CLUSTERED INDEX IX_SP_Execution_StartDateTime
        ON SP_Execution (StartDateTime);

    CREATE NONCLUSTERED INDEX IX_SP_Execution_Environment
        ON SP_Execution (Environment);

    CREATE NONCLUSTERED INDEX IX_SP_Execution_Environment_StartDateTime
    ON SP_Execution (Environment, StartDateTime);
END

-- DROP INDEX IX_SP_Execution_Environment ON SP_Execution;
-- DROP INDEX IX_SP_Execution_Environment_StartDateTime ON SP_Execution;

-- ALTER TABLE SP_Execution DROP COLUMN BatchIds;