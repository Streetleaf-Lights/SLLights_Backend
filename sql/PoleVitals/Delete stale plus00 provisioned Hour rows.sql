-- Deletes provisioned Hour rows whose PeriodStart has +00:00 offset,
-- which means they were written before the AT TIME ZONE fix was applied
-- to _PROVISIONED_HOUR_MERGE_SQL. The next loadProvisionedPoleVitals run
-- (or backfill) will rewrite them correctly with the local Eastern offset.
--
-- Safe to run at any time -- the MERGE will recreate all rows that have
-- real telemetry behind them.

DELETE FROM PoleVitals
WHERE Source = 'Provisioned'
  AND PeriodType = 'Hour'
  AND PeriodStart LIKE '% +00:00';
