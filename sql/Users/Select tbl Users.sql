SELECT TOP 100
    Id,
    Name,
    Email,
    PasswordHash,
    Role,
    Status,
    CustomerId,
    ResetToken,
    ResetTokenExpiresAt
FROM Users
WHERE 1 = 1
-- AND Email = 'someone@example.com'
-- AND CustomerId = 'recwx649JfiRmWqxF'
-- AND Status = 'Pending'
ORDER BY Name;
