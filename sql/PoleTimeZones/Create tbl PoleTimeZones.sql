-- PoleTimeZones: caches each pole's resolved timezone (from its
-- PoleTelemetry Longitude/Latitude), so pole_vitals_loader.py can bucket
-- Hour/Day/Month/Week vitals in each pole's OWN local time instead of
-- hardcoding Eastern for every pole regardless of where it actually is.
--
-- NOT synced from Airtable or Leadsun directly -- computed FROM
-- PoleTelemetry's own coordinates via shared/timezone_utils.py
-- (timezonefinder, a point-in-polygon lookup library -- there's no way
-- to run that computation inside a T-SQL query, which is why this is a
-- separate cached table/loader rather than folded into PoleVitals'
-- aggregation SQL directly).
--
-- WindowsTimeZone, not IanaTimeZone, is what pole_vitals_loader.py's
-- AT TIME ZONE clauses actually use -- SQL Server expects Windows
-- timezone names ("Eastern Standard Time"), not IANA/Olson names
-- ("America/New_York"), which is what timezonefinder returns natively.
-- IanaTimeZone is kept alongside it purely for human readability/
-- debugging -- it's not used in any SQL bucketing logic.
--
-- Both timezone columns are nullable: a coordinate that doesn't resolve
-- to any IANA zone (e.g. bad/placeholder data), or resolves to an IANA
-- zone outside shared/timezone_utils.py's deliberately US-scoped mapping,
-- still gets a row here (so it isn't re-attempted every cycle), just with
-- WindowsTimeZone left NULL -- pole_vitals_loader.py falls back to
-- Eastern time for any VendorPoleId in that state (see its own comments).
--
-- No FK anywhere -- same reasoning as PoleTelemetry/PoleModels: this
-- project doesn't enforce FKs where load/compute order makes it
-- impractical.

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'PoleTimeZones')
BEGIN
    CREATE TABLE PoleTimeZones (
        Id              INT           IDENTITY(1,1) NOT NULL PRIMARY KEY,
        VendorPoleId      NVARCHAR(100) NULL,           -- NULL for provisioned poles
        ProvisionedPoleId NVARCHAR(64) NULL,          -- NULL for Leadsun poles
        Longitude       FLOAT         NULL,
        Latitude        FLOAT         NULL,
        IanaTimeZone    VARCHAR(50)   NULL,  -- human-readable only, not used in SQL
        WindowsTimeZone VARCHAR(50)   NULL,  -- what AT TIME ZONE actually uses
        Source          VARCHAR(50)   NOT NULL,
        SP_ExecId       INT           NULL
    );

    -- One row per Leadsun pole (VendorPoleId is its identifier)
    CREATE UNIQUE NONCLUSTERED INDEX UX_PoleTimeZones_VendorPoleId
        ON PoleTimeZones (VendorPoleId)
        WHERE VendorPoleId IS NOT NULL;

    -- One row per provisioned pole (ProvisionedPoleId is its identifier)
    CREATE UNIQUE NONCLUSTERED INDEX UX_PoleTimeZones_ProvisionedPoleId
        ON PoleTimeZones (ProvisionedPoleId)
        WHERE ProvisionedPoleId IS NOT NULL;

    CREATE NONCLUSTERED INDEX IX_PoleTimeZones_SP_ExecId
        ON PoleTimeZones (SP_ExecId);
END;
