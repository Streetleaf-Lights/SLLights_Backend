-- Removes CrewAssigned from Customers -- no longer used for crew scoping.
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('dbo.Customers') AND name = 'CrewAssigned')
    ALTER TABLE dbo.Customers DROP COLUMN CrewAssigned;
