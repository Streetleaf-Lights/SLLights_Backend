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

-- DROP TABLE IF EXISTS Poles;

CREATE TABLE Poles (
        Id                      VARCHAR(50)         NOT NULL PRIMARY KEY,
        PoleNumber              NVARCHAR(100)       NULL,
        LocationId              VARCHAR(50)          NULL,
        ProjectId               VARCHAR(50)          NULL,
        CustomerId              VARCHAR(50)          NULL,
        InstallDate             DATE                 NULL,
        Lat                     FLOAT                NULL,
        Long                    FLOAT                NULL,
        SP_ExecId               INT                  NULL,
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
