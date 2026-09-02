-- Poles table: mirrors Customers/Projects' shape and conventions.
--
-- Notes / assumptions (confirm before running against a real environment):
--   * No FK from Poles.ProjectId to Projects.Id, nor from Poles.CustomerId
--     to Customers.Id -- intentional. function_app.py runs load_poles()
--     BEFORE load_projects() and load_customers(), so at insert time
--     neither the referenced Project nor Customer row exists yet. An FK
--     here would make the insert fail.
--   * Lat/Long are FLOAT. If you need fixed precision instead (e.g. to
--     avoid floating-point drift across repeated writes), swap to
--     DECIMAL(9,6), which comfortably covers GPS coordinate precision.
--   * InstallDate here is a plain DATE (singular) -- distinct from
--     Projects.InstallDates, which is plural/JSON-encoded. Confirm
--     Airtable's "Field Installed" is really a single date and not
--     something else (e.g. a checkbox).
--   * PoleNumber/LocationId are plain scalar columns; ProjectId and
--     CustomerId are both linked-record references (list of ids, first one
--     taken) -- see projects_loader.py's comments on "Contracting Entity"
--     for background on that field-naming quirk.
--   * CountyFips is VARCHAR(5), not INT -- a FIPS code can have a leading
--     zero (e.g. "01001" for Autauga County, AL), which an INT column
--     would silently strip on storage. This is what
--     pole_timezones_loader.py joins against CountyTimeZones.FIPS with.
--   * ControllerId matches up with PoleTelemetry.ProductId -- Leadsun's
--     own name for the exact same underlying identifier, sourced here
--     from Airtable's own "Controller ID" field instead. Two separate,
--     independently-sourced columns rather than one shared column --
--     see shared/poles_loader.py's own comment on _map_record_to_pole()'s
--     ControllerId mapping for the full reasoning. NVARCHAR(50), matching
--     PoleTelemetry.ProductId's own type/width.
--   * Active -- per explicit request, flags whether a Pole's own Id is
--     still present in loadPoles' own Airtable fetch, without deleting
--     the row when it isn't (PoleTelemetry/PoleVitals/PoleTimeZones all
--     reference this same LocationId, and deleting outright would lose
--     that history). Named generically (not e.g. "IsRemovedFromAirtable")
--     so it reads sensibly if a future source OTHER than Airtable ever
--     needs to reconcile against this same table. Set by shared/
--     airtable_removal_utils.flag_records_removed_from_airtable(),
--     called once per run from load_poles(), separately from this
--     table's own MERGE -- see that function's own docstring for why.
--     BIT NOT NULL DEFAULT 1: every row starts as "active", correct as
--     a starting assumption since the very next loadPoles run
--     re-evaluates every row anyway.

-- DROP TABLE IF EXISTS Poles;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'Poles')
BEGIN

    CREATE TABLE Poles (
        Id                      VARCHAR(50)         NOT NULL PRIMARY KEY,
        PoleNumber              NVARCHAR(100)       NULL,
        LocationId              VARCHAR(50)          NULL,
        CountyFips              VARCHAR(5)           NULL,
        ProjectId               VARCHAR(50)          NULL,
        CustomerId              VARCHAR(50)          NULL,
        InstallDate             DATE                 NULL,
        Lat                     FLOAT                NULL,
        Long                    FLOAT                NULL,
        ControllerId            NVARCHAR(50)         NULL,
        SP_ExecId               INT                  NULL,
        Active                  BIT                  NOT NULL DEFAULT 1,
        AirTableCreatedDateTime DATETIMEOFFSET(3)    NULL
    );

    CREATE NONCLUSTERED INDEX IX_Poles_SP_ExecId
        ON Poles (SP_ExecId);

    CREATE NONCLUSTERED INDEX IX_Poles_ProjectId
        ON Poles (ProjectId);

    CREATE NONCLUSTERED INDEX IX_Poles_CustomerId
        ON Poles (CustomerId);

    CREATE NONCLUSTERED INDEX IX_Poles_LocationId
        ON Poles (LocationId);

    CREATE NONCLUSTERED INDEX IX_Poles_CountyFips
        ON Poles (CountyFips);
END
