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
  ORDER BY [StartDateTime] DESC
  