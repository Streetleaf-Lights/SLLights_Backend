SELECT TOP 100
    Id,
    Name,
    Email,
    PasswordHash,
    Role,
    Status,
    CustomerId,
    ResetToken,
    ResetTokenExpiresAt AT TIME ZONE 'Eastern Standard Time' AS ResetTokenExpiresAt
FROM Users
WHERE 1 = 1
-- AND Email = 'someone@example.com'
-- AND CustomerId = 'recwx649JfiRmWqxF'
-- AND Status = 'Pending'
-- AND Id = '899ac3bf-f0bc-418c-aebf-696aeca773ad'
ORDER BY Name;

-- SELECT COLUMN_NAME, DATA_TYPE
-- FROM INFORMATION_SCHEMA.COLUMNS
-- WHERE TABLE_NAME = 'Users'
-- ORDER BY ORDINAL_POSITION;
