-- Stores the synced record ID of this pole as it exists in the Airtable
-- Issues base (the mirror/synced copy of the Poles table). Required when
-- creating PoleIssues via the Airtable API, since the PoleId linked field
-- in the Issues table points to the synced copy, not the original.
-- Populated by loadLinkedPoles after loadPoles runs.

IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.Poles') AND name = 'LinkedPoleId')
    ALTER TABLE dbo.Poles ADD LinkedPoleId VARCHAR(50) NULL;
