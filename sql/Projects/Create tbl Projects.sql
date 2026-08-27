-- Projects table: mirrors the Customers table's shape and conventions.
--
-- Notes / assumptions (confirm before running against a real environment):
--   * No FK from Projects.CustomerId to Customers.Id -- intentional, same
--     reasoning as dropping the FK on Customers/SP_Execution: load_projects()
--     runs BEFORE load_customers() in function_app.py, so at insert time the
--     referenced Customer row may not exist yet. An FK here would make that
--     insert fail.
--   * EffectiveDate is a plain DATE (no time component assumed). If Airtable
--     actually sends a full timestamp, switch to DATETIMEOFFSET(3) and
--     update projects_loader.py's diff-check columns accordingly
--     (INTERSECT-based comparison already handles either type safely).
--   * InstallDates is plural/multi-valued (a project can have more than one
--     install date), so it's stored the same way as PoleNumbers/PoleIds --
--     JSON-encoded text in NVARCHAR(MAX), not a native DATE/date-list type.
--   * SP_ExecId has no FK either, consistent with Customers.SP_ExecId.
--   * LeadsunProject is a JSON object (NVARCHAR(MAX), not native SQL
--     JSON -- this project stores all its JSON columns as plain text,
--     e.g. PoleNumbers/PoleIds/InstallDates above, and reads them via
--     JSON_VALUE/JSON_QUERY/JSON_MODIFY rather than a dedicated JSON
--     type, since SQL Server itself has none). Always has at least a
--     "ProjectId" key (Airtable's own "Leadsun ProjectID" field, written
--     by projects_loader.py); ProjectName/UserName/groups are filled in
--     SEPARATELY, later, by
--     pole_telemetry_loader.update_leadsun_project_details(), which
--     aggregates fresh from PoleTelemetry after each loadPoleTelemetry
--     run -- see that function's own docstring for the full shape and
--     field-mapping reasoning. No index on this column -- at this
--     table's own modest scale (low hundreds of rows), a full scan to
--     resolve JSON_VALUE(LeadsunProject, '$.ProjectId') = ? is more than
--     fast enough; a computed column + index on that same expression
--     would be the way to add one later if this table ever grows large
--     enough to need it.

-- DROP TABLE IF EXISTS Projects;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'Projects')
BEGIN

    CREATE TABLE Projects (
        Id                      VARCHAR(50)         NOT NULL PRIMARY KEY,
        Name                    NVARCHAR(255)       NOT NULL,
        PoleNumbers             NVARCHAR(MAX)       NULL,
        PoleIds                 NVARCHAR(MAX)       NULL,
        SP_ExecId               INT                  NULL,
        CustomerId              VARCHAR(50)          NULL,
        PolesUnderContract      INT                  NULL,
        EffectiveDate           DATE                 NULL,
        InstallDates            NVARCHAR(MAX)        NULL,
        LeadsunProject          NVARCHAR(MAX)        NULL,
        AirTableCreatedDateTime DATETIMEOFFSET(3)    NULL
    );

    CREATE NONCLUSTERED INDEX IX_Projects_SP_ExecId
        ON Projects (SP_ExecId);

    CREATE NONCLUSTERED INDEX IX_Projects_CustomerId
        ON Projects (CustomerId);

    CREATE NONCLUSTERED INDEX IX_Projects_Name
        ON Projects (Name);
END
