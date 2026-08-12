-- Adds Poles.CountyFips -- sourced from Airtable's "CountyFips" field
-- (distinct from "Location ID", the pole's own identifier, which
-- already maps to Poles.LocationId). See shared/poles_loader.py's own
-- comments on AIRTABLE_POLES_FIELDS and _clean_county_fips() for the
-- full reasoning.
--
-- This is what pole_timezones_loader.py now joins against
-- CountyTimeZones.FIPS with, replacing the previous Poles.Lat/Poles.Long
-- + timezonefinder-based per-pole computation entirely -- see
-- "sql/CountyTimeZones/Create tbl CountyTimeZones.sql" for that table and
-- the reasoning behind the switch (Lat/Long being frequently missing or
-- incorrect, while CountyFips is a reliably-populated field).
--
-- VARCHAR(5), not INT: a FIPS code can have a leading zero (e.g. "01001"
-- for Autauga County, AL), which an INT column would silently strip on
-- storage -- see shared/poles_loader.py's own _clean_county_fips() for
-- the matching defensive re-padding on the Python side of this same
-- concern.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('Poles') AND name = 'CountyFips'
)
BEGIN
    ALTER TABLE Poles ADD CountyFips VARCHAR(5) NULL;
END
GO

CREATE NONCLUSTERED INDEX IX_Poles_CountyFips
    ON Poles (CountyFips);
