-- Adds three columns to Poles, sourced from Streetleaf's OWN provisioned
-- database's `serials` table (a separate Azure SQL server -- see
-- shared/sql_client.get_provisioned_connection()), matched onto Poles
-- via serials.serial_number = Poles.ControllerId. See
-- shared/provisioned_data_loader.py's own load_provisioned_pole_serials()
-- for the loader that populates these:
--
--   ProvisionedPoleId               <- serials.device_uid
--   PoleModelId                     <- serials.product_id + 1000 (matches
--                                       PoleModels.ModelId -- the same
--                                       +1000 offset
--                                       load_provisioned_pole_models()
--                                       already uses, so a given
--                                       product_id maps to the exact same
--                                       ModelId in both tables)
--   ProvisionedPoleCreatedDateTime  <- serials.created_at, VERBATIM --
--                                       stored as plain DATETIME2(7),
--                                       NOT DATETIMEOFFSET, despite this
--                                       project's own usual strong
--                                       preference for DATETIMEOFFSET
--                                       everywhere else. This is
--                                       deliberate: the provisioned db's
--                                       own created_at column carries NO
--                                       timezone information, and its
--                                       true timezone wasn't known with
--                                       confidence at the time this was
--                                       added -- rather than guess (UTC?
--                                       Eastern?) and risk silently
--                                       baking in a wrong offset forever,
--                                       this stores the source value
--                                       exactly as given, with no
--                                       conversion applied. Revisit this
--                                       column's type if the source
--                                       system's own timezone is
--                                       confirmed later.
--
-- No FK from PoleModelId to PoleModels.ModelId, and no FK from
-- ControllerId's own matching logic here to anything -- same reasoning
-- as this table's own existing ProjectId/CustomerId columns (see this
-- table's own header comment above): Poles and the provisioned-db
-- pipeline are loaded by entirely separate, independently-scheduled
-- loaders (loadAirTableData vs loadDeviceData), so there's no ordering
-- guarantee that PoleModels already has a matching row at the moment
-- this UPDATE runs. An FK here would make an otherwise-harmless,
-- temporarily-unmatched value fail the whole write.
--
-- Only UPDATES existing Poles rows (matched by ControllerId) -- never
-- inserts new ones. A serials row whose own serial_number matches no
-- current Poles.ControllerId is simply not applied to anything; Poles
-- rows themselves still only ever come from Airtable via loadPoles.
--
-- Run this AFTER deploying the updated shared/provisioned_data_loader.py
-- (whose staging-table/UPDATE SQL now references these three columns) --
-- otherwise loadDeviceData will fail every run the moment
-- load_provisioned_pole_serials() tries to write to columns that don't
-- exist yet, the same class of failure this project's own earlier
-- column-addition migrations (e.g. "Add ControllerId column.sql")
-- already document.
--
-- Safe to re-run -- each addition is individually guarded.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Poles') AND name = 'ProvisionedPoleId'
)
BEGIN
    ALTER TABLE Poles ADD ProvisionedPoleId NVARCHAR(64) NULL;
END

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Poles') AND name = 'PoleModelId'
)
BEGIN
    ALTER TABLE Poles ADD PoleModelId INT NULL;
END

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Poles') AND name = 'ProvisionedPoleCreatedDateTime'
)
BEGIN
    ALTER TABLE Poles ADD ProvisionedPoleCreatedDateTime DATETIME2(7) NULL;
END

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE object_id = OBJECT_ID('Poles') AND name = 'IX_Poles_PoleModelId'
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_Poles_PoleModelId ON Poles (PoleModelId);
END;
