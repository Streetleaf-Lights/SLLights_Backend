-- Clears Projects.LeadsunProject for ONE project, setting it back to NULL
-- (blank). That is all it does: it does NOT delete the Projects row, drop the
-- column, or touch Airtable or Leadsun.
--
-- NULL counts as "blank" for populateLeadsunProject (which accepts NULL, ''
-- and '{}'), so the project can be populated again afterwards -- but see
-- the two checks below first, or it will not stay cleared / will not work:
--
--   1. AIRTABLE. Airtable's "Leadsun ProjectID" is the source of truth.
--      loadProjects rewrites LeadsunProject's "ProjectId" from it on every
--      run, so if that field still holds the id, this value is simply put
--      back at the next run -- and populateLeadsunProject refuses (409)
--      while Airtable's field is non-empty. Clear it on the project's
--      Airtable record ("Project Tracking" table) too.
--
--   2. LEADSUN. The project that was created there still exists (there is no
--      delete call), so populateLeadsunProject will refuse (409) to create
--      another with the same name. Use a different ProjectName, or delete
--      the old one in Leadsun's own UI.
--
-- Run the whole script: step 1 shows what is there now, step 2 clears it
-- inside a transaction that only commits if exactly one row matched, and
-- step 3 shows the result.

DECLARE @ProjectId VARCHAR(50) = 'recagXKKY76we25Zw';   -- Projects.Id (= its Airtable record id)

-- 1. Before
SELECT Id, Name, LeadsunProject
FROM Projects
WHERE Id = @ProjectId;

-- 2. Clear it -- all or nothing
BEGIN TRANSACTION;

UPDATE Projects
SET LeadsunProject = NULL
WHERE Id = @ProjectId;

IF @@ROWCOUNT = 1
BEGIN
    COMMIT TRANSACTION;
    PRINT 'Cleared Projects.LeadsunProject for ' + @ProjectId + '.';
END
ELSE
BEGIN
    ROLLBACK TRANSACTION;
    PRINT 'No single Project found with Id ' + @ProjectId + ' -- nothing was changed.';
END

-- 3. After
SELECT Id, Name, LeadsunProject
FROM Projects
WHERE Id = @ProjectId;
