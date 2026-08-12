-- One-time migration for environments where PoleVitals already exists
-- without the IsOnline/LightStatus columns. Safe to re-run -- both
-- additions are guarded, and the CHECK constraint add is guarded too.
--
-- After running this, every existing PoleVitals row will have NULL for
-- both new columns until the next load_pole_vitals() run recomputes
-- them (which happens automatically for the current + previous bucket
-- of each period type on the very next cycle -- no separate backfill
-- script needed here, unlike PoleTimeZones/IsDaylight, since
-- load_pole_vitals() already recomputes recent buckets every run).
--
-- The GO separators below are required, not stylistic: SQL Server
-- compiles a whole batch before executing any of it, so a column just
-- added by ALTER TABLE isn't visible to a later statement in that SAME
-- batch that references it (e.g. the CHECK constraint on LightStatus) --
-- "Invalid column name" at compile time, even though the ALTER TABLE
-- itself would have succeeded. Each GO forces the preceding ALTER TABLE
-- to run as its own batch first.

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'IsOnline'
)
BEGIN
    ALTER TABLE PoleVitals ADD IsOnline BIT NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('PoleVitals') AND name = 'LightStatus'
)
BEGIN
    ALTER TABLE PoleVitals ADD LightStatus VARCHAR(20) NULL;
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.check_constraints
    WHERE object_id = OBJECT_ID('CK_PoleVitals_LightStatus')
)
BEGIN
    ALTER TABLE PoleVitals ADD CONSTRAINT CK_PoleVitals_LightStatus
        CHECK (LightStatus IN ('Working', 'DayLight', 'Not Working'));
END
