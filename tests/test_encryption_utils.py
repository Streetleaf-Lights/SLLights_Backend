"""Tests for shared/encryption_utils.py"""

import pytest
from cryptography.fernet import Fernet

from shared import encryption_utils


@pytest.fixture(autouse=True)
def _clean_encryption_key_env(monkeypatch):
    """Every test in this file starts with the key unset, so a test
    exercising the missing-key path never accidentally inherits a real
    key from the environment it happens to run in."""
    monkeypatch.delenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", raising=False)


@pytest.fixture
def real_key(monkeypatch):
    """A genuinely valid Fernet key, set for tests that need one."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", key)
    return key


class TestMissingOrMalformedKey:
    def test_encrypt_raises_when_key_missing(self):
        with pytest.raises(encryption_utils.EncryptionConfigError, match="not set"):
            encryption_utils.encrypt_secret("hunter2")

    def test_decrypt_raises_when_key_missing(self):
        with pytest.raises(encryption_utils.EncryptionConfigError, match="not set"):
            encryption_utils.decrypt_secret("some-ciphertext")

    def test_validate_raises_when_key_missing(self):
        with pytest.raises(encryption_utils.EncryptionConfigError, match="not set"):
            encryption_utils.validate_encryption_key_configured()

    def test_malformed_key_raises_a_clear_config_error(self, monkeypatch):
        monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", "not-a-valid-fernet-key")
        with pytest.raises(encryption_utils.EncryptionConfigError, match="isn't a valid Fernet key"):
            encryption_utils.encrypt_secret("hunter2")

    def test_error_message_includes_how_to_generate_a_key(self):
        """The whole point of a clear error here -- someone hitting this
        for the first time should immediately know what to do next, not
        have to go hunting through source code."""
        with pytest.raises(encryption_utils.EncryptionConfigError, match="Fernet.generate_key"):
            encryption_utils.encrypt_secret("hunter2")


class TestEncryptDecryptRoundTrip:
    def test_round_trips_correctly(self, real_key):
        ciphertext = encryption_utils.encrypt_secret("hunter2")
        assert encryption_utils.decrypt_secret(ciphertext) == "hunter2"

    def test_ciphertext_is_not_the_plaintext(self, real_key):
        ciphertext = encryption_utils.encrypt_secret("hunter2")
        assert ciphertext != "hunter2"
        assert "hunter2" not in ciphertext

    def test_same_plaintext_produces_different_ciphertext_each_time(self, real_key):
        """Fernet embeds a random IV per encryption -- this is expected,
        not a bug. Confirms two accounts sharing a real password won't
        have identical EncryptedPassword values."""
        ciphertext1 = encryption_utils.encrypt_secret("hunter2")
        ciphertext2 = encryption_utils.encrypt_secret("hunter2")
        assert ciphertext1 != ciphertext2
        # Both still decrypt back to the same original plaintext.
        assert encryption_utils.decrypt_secret(ciphertext1) == "hunter2"
        assert encryption_utils.decrypt_secret(ciphertext2) == "hunter2"

    def test_round_trips_unicode_and_special_characters(self, real_key):
        """Real passwords in the source spreadsheet include symbols like
        $, !, @, #, *, =, -, ?, ;, }, [, ,, _ -- confirms these survive
        the encrypt/decrypt round trip intact, not just plain ASCII
        letters and digits."""
        tricky_password = "Fi9OJisW1_*eS3E$!@#;}[,="
        ciphertext = encryption_utils.encrypt_secret(tricky_password)
        assert encryption_utils.decrypt_secret(ciphertext) == tricky_password

    def test_ciphertext_is_a_plain_string_safe_for_a_varchar_column(self, real_key):
        ciphertext = encryption_utils.encrypt_secret("hunter2")
        assert isinstance(ciphertext, str)
        ciphertext.encode("ascii")  # must not raise -- Fernet tokens are url-safe base64

    def test_validate_does_not_raise_when_key_is_valid(self, real_key):
        encryption_utils.validate_encryption_key_configured()  # must not raise


class TestWrongKey:
    def test_decrypting_with_a_different_key_raises_invalid_token(self, monkeypatch):
        monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        ciphertext = encryption_utils.encrypt_secret("hunter2")

        monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        with pytest.raises(encryption_utils.InvalidToken):
            encryption_utils.decrypt_secret(ciphertext)

    def test_invalid_token_is_not_silently_swallowed_into_a_placeholder(self, monkeypatch):
        """A silently-wrong 'password' returned instead of an obvious
        error would be far more dangerous than a clear failure -- this
        confirms decrypt_secret() genuinely propagates InvalidToken
        rather than catching it internally."""
        monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        ciphertext = encryption_utils.encrypt_secret("hunter2")

        monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        with pytest.raises(encryption_utils.InvalidToken):
            result = encryption_utils.decrypt_secret(ciphertext)
            assert result != "hunter2"  # unreachable if the exception is correctly raised


class TestNoneInputRejected:
    def test_encrypt_none_raises_value_error(self, real_key):
        with pytest.raises(ValueError, match="cannot encrypt None"):
            encryption_utils.encrypt_secret(None)

    def test_decrypt_none_raises_value_error(self, real_key):
        with pytest.raises(ValueError, match="cannot decrypt None"):
            encryption_utils.decrypt_secret(None)


class TestNotCached:
    def test_changing_the_key_between_calls_is_picked_up_immediately(self, monkeypatch):
        """Confirms _get_fernet() reads the env var fresh every call --
        no stale cached Fernet instance from an earlier key."""
        monkeypatch.setenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        encryption_utils.encrypt_secret("hunter2")  # establishes a first "cache" if there were one

        monkeypatch.delenv("LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY", raising=False)
        with pytest.raises(encryption_utils.EncryptionConfigError):
            encryption_utils.encrypt_secret("hunter2")
