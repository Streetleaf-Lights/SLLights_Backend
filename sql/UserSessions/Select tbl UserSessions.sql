SELECT TOP (1000) [Id]
      ,[UserId]
      ,[CreatedAt] AT TIME ZONE 'Eastern Standard Time' AS CreatedAt
      ,[ExpiresAt] AT TIME ZONE 'Eastern Standard Time' AS ExpiresAt
      ,[RevokedAt] AT TIME ZONE 'Eastern Standard Time' AS RevokedAt
  FROM [dbo].[UserSessions]
WHERE 1 = 1
  AND [UserId] = '1644b28b-d652-4594-985a-e208deedbffe'
ORDER BY [CreatedAt] DESC
