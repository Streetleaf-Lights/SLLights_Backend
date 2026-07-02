
-- DROP TABLE IF EXISTS Customers;

CREATE TABLE Customers (
    Id                      VARCHAR(50)         NOT NULL PRIMARY KEY,
    Name                    NVARCHAR(255)       NOT NULL,
    ProjectNames            NVARCHAR(MAX)       NULL,
    ProjectIds              NVARCHAR(MAX)       NULL,
    BatchId                 INT                 NULL,
    Address                 NVARCHAR(255)       NULL,
    City                    NVARCHAR(100)       NULL,
    State                   NVARCHAR(50)        NULL,
    Zip                     NVARCHAR(20)        NULL,
    Phone                   NVARCHAR(20)        NULL,
    AirTableCreatedDateTime DATETIMEOFFSET(3)   NULL

    -- CONSTRAINT FK_Customers_SP_Execution FOREIGN KEY (BatchId)
    --     REFERENCES SP_Execution (Id)
);

CREATE NONCLUSTERED INDEX IX_Customers_BatchId
    ON Customers (BatchId);

CREATE NONCLUSTERED INDEX IX_Customers_Name_City_State
    ON Customers (Name, City, State);
