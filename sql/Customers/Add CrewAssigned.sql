IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Customers') AND name = 'CrewAssigned')
    ALTER TABLE dbo.Customers ADD CrewAssigned BIT NULL;
