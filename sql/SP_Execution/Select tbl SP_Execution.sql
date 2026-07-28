SELECT TOP (1000) [Id]
      ,[Name]
      ,[Environment]
      ,[StartDateTime]
      ,[EndDateTime]
      ,[TotalSuccessfulRecords]
      ,[TotalErrorRecords]
      ,[Source]
      ,[BatchCount]
      ,[IsFinalBatch]
      ,[ErrorMessage]
  FROM [dbo].[SP_Execution]
  WHERE 1 = 1
--   AND [Name] = 'loadPoleVitals'
--   and environment = 'Prod'
  ORDER BY [StartDateTime] DESC

--   SELECT
--     r.session_id,
--     r.status,
--     r.command,
--     r.wait_type,
--     r.wait_time / 1000.0 AS wait_seconds,
--     r.blocking_session_id,
--     r.percent_complete,
--     r.total_elapsed_time / 1000.0 AS elapsed_seconds,
--     t.text AS running_sql
-- FROM sys.dm_exec_requests r
-- CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
-- WHERE r.session_id != @@SPID
-- ORDER BY r.total_elapsed_time DESC;

-- SELECT r.session_id, t.text
-- FROM sys.dm_exec_requests r
-- CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
-- WHERE r.session_id = 131;

-- SELECT qp.query_plan
-- FROM sys.dm_exec_requests r
-- CROSS APPLY sys.dm_exec_query_plan(r.plan_handle) qp
-- WHERE r.session_id = 131;

-- SELECT TOP 5 Id, StartDateTime, EndDateTime, TotalSuccessfulRecords, TotalErrorRecords, ErrorMessage
-- FROM SP_Execution
-- WHERE Name = 'loadPoleVitals'
-- ORDER BY StartDateTime DESC;

-- SELECT name, value, value_in_use FROM sys.configurations WHERE name = 'cost threshold for parallelism';

-- -- Instance-level
-- SELECT name, value, value_in_use FROM sys.configurations WHERE name = 'max degree of parallelism';

-- -- Database-scoped (Azure SQL Database often uses this one specifically)
-- SELECT * FROM sys.database_scoped_configurations WHERE name = 'MAXDOP';

-- SELECT
--     qs.execution_count,
--     qs.last_dop,
--     qs.min_dop,
--     qs.max_dop,
--     qs.total_worker_time / qs.execution_count AS avg_worker_time,
--     qs.total_elapsed_time / qs.execution_count AS avg_elapsed_time
-- FROM sys.dm_exec_query_stats qs
-- CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t
-- WHERE t.text LIKE '%''Week'' AS PeriodType%';

-- SELECT
--     session_id, node_id, physical_operator_name,
--     thread_id, row_count--, [rebinds], [rewinds]
-- FROM sys.dm_exec_query_profiles
-- WHERE session_id = 145
-- ORDER BY node_id, thread_id;

-- -- SELECT session_id, login_time, last_request_start_time, status, program_name, host_name
-- --   FROM sys.dm_exec_sessions
-- --   WHERE session_id = 101;

-- SELECT
--     r.session_id, r.status, r.command, r.cpu_time, r.total_elapsed_time,
--     s.program_name, s.login_time
-- FROM sys.dm_exec_sessions s
-- LEFT JOIN sys.dm_exec_requests r ON s.session_id = r.session_id
-- WHERE s.is_user_process = 1
-- ORDER BY r.total_elapsed_time DESC;

-- SELECT session_id, percent_complete, total_elapsed_time / 1000.0 AS elapsed_seconds
-- FROM sys.dm_exec_requests
-- WHERE command = 'CREATE INDEX';
