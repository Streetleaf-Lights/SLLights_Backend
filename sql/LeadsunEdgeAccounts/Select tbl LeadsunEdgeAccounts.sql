-- EncryptedPassword shows the Fernet ciphertext here, not the real
-- password -- decryption only happens in Python, via
-- shared/encryption_utils.decrypt_secret(), never in SQL. This query is
-- for confirming which accounts exist and when they were loaded/last
-- changed, not for reading actual credentials.
SELECT
    Username,
    EncryptedPassword,
    LoadedAt AT TIME ZONE 'Eastern Standard Time' AS LoadedAt,
    UpdatedAt AT TIME ZONE 'Eastern Standard Time' AS UpdatedAt
FROM LeadsunEdgeAccounts
ORDER BY Username;
