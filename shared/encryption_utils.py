"""
Fernet-based (symmetric, authenticated) encryption for secrets that need
to be stored REVERSIBLY -- genuinely retrieved and used again later, not
just verified like a login password. This is a fundamentally different
need from users_management_api.py's own bcrypt-based Users.PasswordHash:
that one is a ONE-WAY hash, verified via bcrypt's own comparison, and
NEVER decrypted -- appropriate there because this system only ever needs
to confirm a login attempt matches, never to recover the original
password. A LeadsunEdgeAccounts row is different: these are real
credentials for logging into the Leadsun Edge system ITSELF, so the
original plaintext must be recoverable later (by an operator, or an
automated process) -- hashing would make that impossible by design,
which is exactly why hashing is wrong for this specific case even though
it's right for Users.PasswordHash.

Uses a single, shared symmetric key from
LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY (a base64-encoded 32-byte key, the
format Fernet.generate_key() produces) -- same "read from os.environ,
fail clearly if missing" convention as this project's other secrets
(AUTH_JWT_SECRET, LEADSUN_CLIENT_CERT_PEM, etc.), not a hardcoded value
or a key derived from something else.

Generate a new key with:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and add it to local.settings.json's own "Values" (for local runs) and
the Function App's own App Settings (for anything running in Azure) --
NOT committed to source control, same as every other secret in this
project. Losing this key means every already-encrypted
EncryptedPassword value becomes permanently unrecoverable -- there is no
way to decrypt without it, by design (that's what makes it real
encryption rather than a reversible-only-if-you-guess obfuscation). Back
it up somewhere durable and separate from the database itself.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

# Re-exported so callers catching decryption failures don't need their
# own `from cryptography.fernet import InvalidToken` -- one import
# location for anything touching encrypted secrets in this project.
__all__ = [
    "encrypt_secret",
    "decrypt_secret",
    "validate_encryption_key_configured",
    "EncryptionConfigError",
    "InvalidToken",
]


class EncryptionConfigError(Exception):
    """
    Raised when the encryption key itself is missing or malformed --
    a deployment/configuration problem, distinct from InvalidToken
    (which means the KEY is fine but THIS SPECIFIC ciphertext doesn't
    match it -- wrong key, corrupted data, or truncated data instead).
    """


def _get_fernet() -> Fernet:
    """
    Deliberately NOT cached (e.g. via functools.lru_cache) -- reads
    LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY fresh on every call. Caching a
    Fernet instance keyed by a value read once at import/first-call time
    would make tests that need to exercise a missing-or-different key
    across multiple cases fight a stale cache; constructing a Fernet
    from an already-decoded key is cheap, so there's no real performance
    cost to paying for this every call instead.
    """
    key = os.environ.get("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY")
    if not key:
        raise EncryptionConfigError(
            "LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY is not set. Generate one with "
            '`python3 -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and add it to local.settings.json\'s '
            "Values (for local runs) and the Function App's own App Settings (for Azure)."
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except Exception as ex:
        raise EncryptionConfigError(
            f"LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY is set but isn't a valid Fernet key "
            f"(expected a base64-encoded 32-byte value, the format "
            f"Fernet.generate_key() produces): {ex}"
        ) from ex


def validate_encryption_key_configured() -> None:
    """
    Raises EncryptionConfigError immediately if
    LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY is missing or malformed --
    otherwise returns None, doing nothing further. Intended for a
    caller (e.g. a loader script) to call ONCE, up front, before doing
    any real work that would only fail on this same problem much
    later -- e.g. after already reading an entire spreadsheet, only to
    discover on the very first row that there's no usable key to
    encrypt anything with. Deliberately public (unlike _get_fernet()
    itself) since "is this configured at all" is a legitimate,
    reusable question on its own, independent of actually
    encrypting/decrypting anything yet.
    """
    _get_fernet()


def encrypt_secret(plaintext: str) -> str:
    """
    Encrypts plaintext into a Fernet token -- url-safe base64 TEXT (not
    raw bytes), safe to store directly in a VARCHAR/NVARCHAR column with
    no further encoding needed. Returns a genuinely DIFFERENT-looking
    token every single call, even for the exact same plaintext with the
    exact same key -- Fernet embeds a random IV and the current
    timestamp in every token it produces. This is expected, not a bug:
    two accounts that happen to share the same real password will NOT
    have identical EncryptedPassword values, and encrypting the same
    password twice produces two different (but both individually valid,
    both decrypting back to the same plaintext) tokens.
    """
    if plaintext is None:
        raise ValueError(
            "encrypt_secret() cannot encrypt None -- pass an empty string if that's "
            "genuinely the intended value"
        )
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """
    Reverses encrypt_secret(). Raises cryptography.fernet.InvalidToken
    (re-exported from this module -- see its own module docstring) if
    ciphertext wasn't produced by THIS SAME key -- a wrong or rotated
    key, or corrupted/truncated stored data. Deliberately lets this
    propagate rather than catching it and returning some placeholder
    value: a caller silently getting back a wrong, unusable "password"
    instead of an obvious, immediate error is far more dangerous here
    (e.g. it could get bound into an actual login attempt against the
    real Leadsun Edge system) than a clear failure that stops the
    caller from proceeding at all.
    """
    if ciphertext is None:
        raise ValueError("decrypt_secret() cannot decrypt None")
    return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
