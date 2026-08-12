-- Users joined with Customers, so you can see which customer each user
-- belongs to without a separate query -- CustomerName was deliberately
-- removed from Users itself once CustomerId became a real FOREIGN KEY,
-- to avoid storing a copy of the name that could go stale.
--
-- LEFT JOIN (not INNER): CustomerId is nullable -- a "Streetleaf Admin"
-- isn't scoped to one customer, so those rows still show up here, just
-- with all the Customer-side columns NULL.
SELECT TOP 100
    u.Id,
    u.Name,
    u.Email,
    u.PasswordHash,
    u.Role,
    u.Status,
    u.CustomerId,
    c.Name AS CustomerName,
    c.City AS CustomerCity,
    c.State AS CustomerState,
    u.ResetToken,
    u.ResetTokenExpiresAt
FROM Users u
LEFT JOIN Customers c ON u.CustomerId = c.Id
WHERE 1 = 1
-- AND u.Email = 'someone@example.com'
-- AND u.CustomerId = 'recwx649JfiRmWqxF'
-- AND u.Role = 'Streetleaf Admin'
-- AND u.Status = 'Pending'
ORDER BY u.Name;
