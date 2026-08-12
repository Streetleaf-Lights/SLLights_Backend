
-- DROP TABLE IF EXISTS Customers;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'Customers')
BEGIN

    CREATE TABLE Customers (
        Id                      VARCHAR(50)         NOT NULL PRIMARY KEY,
        Name                    NVARCHAR(255)       NOT NULL,
        ProjectNames            NVARCHAR(MAX)       NULL,
        ProjectIds              NVARCHAR(MAX)       NULL,
        SP_ExecId               INT                 NULL,
        Address                 NVARCHAR(255)       NULL,
        City                    NVARCHAR(100)       NULL,
        State                   NVARCHAR(50)        NULL,
        Zip                     NVARCHAR(20)        NULL,
        Phone                   NVARCHAR(20)        NULL,
        AirTableCreatedDateTime DATETIMEOFFSET(3)   NULL

        -- CONSTRAINT FK_Customers_SP_Execution FOREIGN KEY (SP_ExecId)
        --     REFERENCES SP_Execution (Id)
    );

    CREATE NONCLUSTERED INDEX IX_Customers_SP_ExecId
        ON Customers (SP_ExecId);

    CREATE NONCLUSTERED INDEX IX_Customers_Name_City_State
        ON Customers (Name, City, State);
END

-- EXEC sp_rename 'Customers.BatchId', 'SP_ExecId', 'COLUMN';

-- EXEC sp_rename 'Customers.IX_Customers_BatchId', 'IX_Customers_SP_ExecId', 'INDEX';
