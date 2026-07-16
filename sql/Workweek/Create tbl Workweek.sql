-- Workweek: a static Sunday-Saturday week-numbering calendar reference
-- table, NOT sourced from Airtable or Leadsun -- purely computed date
-- arithmetic, so unlike every other table in this project it has no
-- Source/SP_ExecId columns and no associated loader/Azure Function. See
-- scripts/generate_workweek_sql.py for how it's populated/regenerated.
--
-- Convention (a real interpretation choice, not the only valid one --
-- flagging since 52 weeks x 7 days = 364 days, one or two days short of a
-- full 365/366-day year, so SOME convention has to give):
--   * Week 1 of year Y starts on the Sunday on-or-before January 1 of Y,
--     so January 1st always falls within that year's Week 1. This can
--     dip a few days into December of the PREVIOUS calendar year (e.g.
--     2026's Week 1 starts Sun 2025-12-28, since Jan 1 2026 is a
--     Thursday).
--   * Each year is anchored independently to its own January 1 -- there
--     is no continuous rolling week counter spanning multiple years.
--   * Because weeks are always exactly 7 days, the last 2-8 days of
--     December (depending on what weekday Jan 1 falls on that year)
--     aren't covered by THAT year's 52 weeks -- they become part of the
--     FOLLOWING year's Week 1 instead. No calendar date is ever left
--     completely uncovered across a continuous range of populated years;
--     it just sometimes lands under the "next" year's label rather than
--     the one you might expect at a glance.
--   * If a different convention is wanted later (e.g. ISO 8601 weeks,
--     Monday-Sunday instead of Sunday-Saturday, or allowing 53 weeks in
--     some years), scripts/generate_workweek_sql.py is the one place to
--     change the logic -- regenerate and re-run the resulting MERGE
--     script afterward.

-- DROP TABLE IF EXISTS Workweek;

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'Workweek')
BEGIN
    CREATE TABLE Workweek (
        Year      INT  NOT NULL,
        Week      INT  NOT NULL,
        StartDate DATE NOT NULL,  -- always a Sunday
        EndDate   DATE NOT NULL,  -- always a Saturday, StartDate + 6 days
        CONSTRAINT PK_Workweek PRIMARY KEY (Year, Week),
        CONSTRAINT CK_Workweek_Week_Range CHECK (Week BETWEEN 1 AND 52)
    );

    CREATE NONCLUSTERED INDEX IX_Workweek_StartDate
        ON Workweek (StartDate);  -- for "which week does date X fall in" lookups
END
