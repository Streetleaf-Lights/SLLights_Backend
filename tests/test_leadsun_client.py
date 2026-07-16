"""Tests for shared/leadsun_client.py"""

import os
import ssl

import pytest

from shared import leadsun_client


class TestValidatePemHasCertificate:
    def test_valid_pem_passes(self):
        leadsun_client._validate_pem_has_certificate(
            "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----", "SOME_SETTING"
        )  # must not raise

    def test_missing_certificate_marker_raises_with_setting_name_in_message(self):
        with pytest.raises(ValueError, match="SOME_SETTING"):
            leadsun_client._validate_pem_has_certificate("not a real cert", "SOME_SETTING")

    def test_missing_certificate_marker_message_mentions_the_likely_causes(self):
        with pytest.raises(ValueError, match="truncated"):
            leadsun_client._validate_pem_has_certificate("garbage", "SOME_SETTING")


class TestWriteClientCertToTempFileValidation:
    def test_raises_clear_error_when_certificate_block_missing(self, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_CLIENT_CERT_PEM", "not a real pem at all")
        with pytest.raises(ValueError, match="LEADSUN_CLIENT_CERT_PEM"):
            leadsun_client._write_client_cert_to_temp_file()

    def test_raises_clear_error_when_private_key_block_missing(self, monkeypatch):
        cert_only = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
        monkeypatch.setattr(leadsun_client, "LEADSUN_CLIENT_CERT_PEM", cert_only)
        with pytest.raises(ValueError, match="private key"):
            leadsun_client._write_client_cert_to_temp_file()

    def test_accepts_rsa_private_key_marker_variant(self, monkeypatch):
        pem = (
            "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
            "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"
        )
        monkeypatch.setattr(leadsun_client, "LEADSUN_CLIENT_CERT_PEM", pem)
        path = leadsun_client._write_client_cert_to_temp_file()
        os.unlink(path)  # must not raise getting here

    def test_valid_combined_pem_writes_successfully(self, monkeypatch):
        monkeypatch.setattr(
            leadsun_client,
            "LEADSUN_CLIENT_CERT_PEM",
            "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        )
        path = leadsun_client._write_client_cert_to_temp_file()
        try:
            assert os.path.exists(path)
        finally:
            os.unlink(path)


def test_write_client_cert_to_temp_file_creates_file_with_pem_content():
    path = leadsun_client._write_client_cert_to_temp_file()
    try:
        with open(path) as f:
            content = f.read()
        assert content == leadsun_client.LEADSUN_CLIENT_CERT_PEM
    finally:
        os.unlink(path)


def test_fetch_lamps_calls_correct_url_with_cert_and_timeout(mock_requests_get_leadsun):
    mock_requests_get_leadsun.return_value.json.return_value = [{"productName": "P1"}]
    mock_requests_get_leadsun.return_value.raise_for_status.return_value = None

    result = leadsun_client.fetch_lamps()

    assert result == [{"productName": "P1"}]
    call = mock_requests_get_leadsun.call_args
    assert call.args[0] == leadsun_client.LEADSUN_API_URL
    assert call.kwargs["timeout"] == 30
    assert "cert" in call.kwargs
    assert os.path.exists(call.kwargs["cert"]) is False  # cleaned up after the call


def test_fetch_lamps_cert_file_contains_pem_during_the_request(mocker):
    """Confirms the temp file passed as cert= actually has the PEM content
    at call time (checked via a side_effect that reads it mid-call)."""
    captured = {}

    def fake_get(url, cert, verify, timeout):
        with open(cert) as f:
            captured["cert_content"] = f.read()
        response = mocker.MagicMock()
        response.json.return_value = []
        response.raise_for_status.return_value = None
        return response

    mocker.patch("shared.leadsun_client.requests.get", side_effect=fake_get)

    leadsun_client.fetch_lamps()

    assert captured["cert_content"] == leadsun_client.LEADSUN_CLIENT_CERT_PEM


def test_fetch_lamps_propagates_http_errors(mock_requests_get_leadsun):
    mock_requests_get_leadsun.return_value.raise_for_status.side_effect = RuntimeError("HTTP 500")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        leadsun_client.fetch_lamps()


def test_fetch_lamps_cleans_up_temp_file_even_on_request_failure(mocker):
    mocker.patch("shared.leadsun_client.requests.get", side_effect=RuntimeError("network down"))
    mock_unlink = mocker.patch("shared.leadsun_client.os.unlink")

    with pytest.raises(RuntimeError, match="network down"):
        leadsun_client.fetch_lamps()

    mock_unlink.assert_called_once()


def test_fetch_lamps_cleans_up_temp_file_on_success(mocker):
    mock_get = mocker.patch("shared.leadsun_client.requests.get")
    mock_get.return_value.json.return_value = []
    mock_get.return_value.raise_for_status.return_value = None
    mock_unlink = mocker.patch("shared.leadsun_client.os.unlink")

    leadsun_client.fetch_lamps()

    mock_unlink.assert_called_once()


def test_default_api_url_is_the_lamps_endpoint():
    assert leadsun_client.LEADSUN_API_URL == "https://leadsunedge-us.com:8550/lamps"


def test_default_models_url_is_the_models_endpoint():
    assert leadsun_client.LEADSUN_MODELS_URL == "https://leadsunedge-us.com:8550/models"


class TestFetchModels:
    """
    fetch_models() shares _get() with fetch_lamps() -- all the cert/verify/
    hostname-bypass behavior is already covered via fetch_lamps()'s tests
    above (same underlying code path). These just confirm fetch_models()
    hits the right URL and returns the right data.
    """

    def test_calls_models_url_not_lamps_url(self, mock_requests_get_leadsun):
        mock_requests_get_leadsun.return_value.json.return_value = [{"modelId": 82}]
        mock_requests_get_leadsun.return_value.raise_for_status.return_value = None

        result = leadsun_client.fetch_models()

        assert result == [{"modelId": 82}]
        called_url = mock_requests_get_leadsun.call_args.args[0]
        assert called_url == leadsun_client.LEADSUN_MODELS_URL
        assert called_url != leadsun_client.LEADSUN_API_URL

    def test_cleans_up_cert_temp_file(self, mocker):
        mock_get = mocker.patch("shared.leadsun_client.requests.get")
        mock_get.return_value.json.return_value = []
        mock_get.return_value.raise_for_status.return_value = None
        mock_unlink = mocker.patch("shared.leadsun_client.os.unlink")

        leadsun_client.fetch_models()

        mock_unlink.assert_called_once()

    def test_propagates_http_errors(self, mock_requests_get_leadsun):
        mock_requests_get_leadsun.return_value.raise_for_status.side_effect = RuntimeError(
            "HTTP 500"
        )

        with pytest.raises(RuntimeError, match="HTTP 500"):
            leadsun_client.fetch_models()


# --------------------------------------------------------------------------
# Server certificate verification (_resolve_verify_option / fetch_lamps)
# --------------------------------------------------------------------------


class TestResolveVerifyOption:
    def test_defaults_to_true_when_nothing_configured(self, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", None)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)
        assert leadsun_client._resolve_verify_option() is True

    def test_returns_temp_file_path_when_server_ca_cert_configured(self, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----")
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)

        path = leadsun_client._resolve_verify_option()
        try:
            assert isinstance(path, str)
            with open(path) as f:
                assert f.read() == "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----"
        finally:
            os.unlink(path)

    def test_returns_false_when_skip_verify_enabled(self, monkeypatch, caplog):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", True)
        with caplog.at_level("WARNING"):
            result = leadsun_client._resolve_verify_option()
        assert result is False
        assert any("insecure" in rec.message.lower() for rec in caplog.records)

    def test_skip_verify_takes_precedence_over_server_ca_cert(self, monkeypatch):
        """If both are set, the more explicit/insecure override wins rather
        than silently picking one -- but this combination is unusual
        enough that it's worth it being deterministic either way."""
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", True)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----")
        assert leadsun_client._resolve_verify_option() is False


class TestFetchLampsVerifyIntegration:
    def test_passes_true_by_default(self, mock_requests_get_leadsun, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", None)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)
        mock_requests_get_leadsun.return_value.json.return_value = []
        mock_requests_get_leadsun.return_value.raise_for_status.return_value = None

        leadsun_client.fetch_lamps()

        assert mock_requests_get_leadsun.call_args.kwargs["verify"] is True

    def test_passes_false_when_skip_verify_enabled(self, mock_requests_get_leadsun, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", True)
        mock_requests_get_leadsun.return_value.json.return_value = []
        mock_requests_get_leadsun.return_value.raise_for_status.return_value = None

        leadsun_client.fetch_lamps()

        assert mock_requests_get_leadsun.call_args.kwargs["verify"] is False

    def test_passes_ca_cert_temp_path_and_cleans_it_up(self, mocker, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----")
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)

        captured = {}

        def fake_get(url, cert, verify, timeout):
            captured["verify_path"] = verify
            with open(verify) as f:
                captured["verify_content"] = f.read()
            response = mocker.MagicMock()
            response.json.return_value = []
            response.raise_for_status.return_value = None
            return response

        mocker.patch("shared.leadsun_client.requests.get", side_effect=fake_get)

        leadsun_client.fetch_lamps()

        assert captured["verify_content"] == "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----"
        assert os.path.exists(captured["verify_path"]) is False  # cleaned up after


class TestNoHostnameCheckAdapter:
    """
    These build a real requests.adapters.HTTPAdapter poolmanager (no
    network I/O involved) to confirm assert_hostname=False actually makes
    it into urllib3's pool config -- not just onto the SSLContext. This is
    the exact thing that was missing before: check_hostname=False on the
    SSLContext alone isn't enough, since urllib3 does its own independent
    hostname check underneath requests' verify= handling.
    """

    def test_assert_hostname_false_reaches_the_pool_manager(self):
        context = leadsun_client._build_no_hostname_check_ssl_context(None)
        adapter = leadsun_client._NoHostnameCheckAdapter(context)

        assert adapter.poolmanager.connection_pool_kw["assert_hostname"] is False

    def test_ssl_context_reaches_the_pool_manager(self):
        context = leadsun_client._build_no_hostname_check_ssl_context(None)
        adapter = leadsun_client._NoHostnameCheckAdapter(context)

        assert adapter.poolmanager.connection_pool_kw["ssl_context"] is context


class TestNoHostnameCheckSslContext:
    def test_check_hostname_is_disabled(self):
        context = leadsun_client._build_no_hostname_check_ssl_context(None)
        assert context.check_hostname is False

    def test_verify_mode_still_requires_a_valid_chain(self):
        """Skipping the hostname check must not also disable chain
        validation -- verify_mode should stay CERT_REQUIRED."""
        context = leadsun_client._build_no_hostname_check_ssl_context(None)
        assert context.verify_mode == ssl.CERT_REQUIRED

    def test_uses_provided_ca_cert_path(self, mocker):
        mock_create_context = mocker.patch("shared.leadsun_client.ssl.create_default_context")
        leadsun_client._build_no_hostname_check_ssl_context("/some/ca.pem")
        mock_create_context.assert_called_once_with(cafile="/some/ca.pem")


class TestFetchLampsHostnameCheckBypass:
    def test_uses_session_with_custom_adapter_when_enabled(self, mocker, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_HOSTNAME_CHECK", True)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----")
        mocker.patch("shared.leadsun_client.ssl.create_default_context")

        mock_session_instance = mocker.MagicMock()
        mock_session_instance.get.return_value.json.return_value = [{"productName": "P1"}]
        mock_session_instance.get.return_value.raise_for_status.return_value = None
        mocker.patch("shared.leadsun_client.requests.Session", return_value=mock_session_instance)
        mocker.patch("shared.leadsun_client.requests.get")  # must NOT be used in this path

        result = leadsun_client.fetch_lamps()

        assert result == [{"productName": "P1"}]
        mock_session_instance.mount.assert_called_once()
        mounted_scheme, mounted_adapter = mock_session_instance.mount.call_args.args
        assert mounted_scheme == "https://"
        assert isinstance(mounted_adapter, leadsun_client._NoHostnameCheckAdapter)
        mock_session_instance.get.assert_called_once()
        assert mock_session_instance.get.call_args.args[0] == leadsun_client.LEADSUN_API_URL
        mock_session_instance.close.assert_called_once()
        leadsun_client.requests.get.assert_not_called()

    def test_plain_requests_get_used_when_disabled(self, mock_requests_get_leadsun, monkeypatch, mocker):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_HOSTNAME_CHECK", False)
        mock_session_class = mocker.patch("shared.leadsun_client.requests.Session")
        mock_requests_get_leadsun.return_value.json.return_value = []
        mock_requests_get_leadsun.return_value.raise_for_status.return_value = None

        leadsun_client.fetch_lamps()

        mock_session_class.assert_not_called()
        mock_requests_get_leadsun.assert_called_once()

    def test_skip_tls_verify_takes_precedence_over_hostname_bypass(
        self, mock_requests_get_leadsun, monkeypatch, mocker
    ):
        """If both LEADSUN_SKIP_TLS_VERIFY and LEADSUN_SKIP_HOSTNAME_CHECK
        are set, the fully-open verify=False path is simpler and already
        covers this -- no need for the custom adapter too."""
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_HOSTNAME_CHECK", True)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", True)
        mock_session_class = mocker.patch("shared.leadsun_client.requests.Session")
        mock_requests_get_leadsun.return_value.json.return_value = []
        mock_requests_get_leadsun.return_value.raise_for_status.return_value = None

        leadsun_client.fetch_lamps()

        mock_session_class.assert_not_called()
        assert mock_requests_get_leadsun.call_args.kwargs["verify"] is False

    def test_warns_when_hostname_check_skipped_without_ca_cert(
        self, mocker, monkeypatch, caplog
    ):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_HOSTNAME_CHECK", True)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", None)

        mock_session_instance = mocker.MagicMock()
        mock_session_instance.get.return_value.json.return_value = []
        mock_session_instance.get.return_value.raise_for_status.return_value = None
        mocker.patch("shared.leadsun_client.requests.Session", return_value=mock_session_instance)

        with caplog.at_level("WARNING"):
            leadsun_client.fetch_lamps()

        assert any(
            "without LEADSUN_SERVER_CA_CERT" in rec.message for rec in caplog.records
        )

    def test_session_and_temp_files_cleaned_up_on_failure(self, mocker, monkeypatch):
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_HOSTNAME_CHECK", True)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SKIP_TLS_VERIFY", False)
        monkeypatch.setattr(leadsun_client, "LEADSUN_SERVER_CA_CERT", "-----BEGIN CERTIFICATE-----\nfake-ca-pem-content\n-----END CERTIFICATE-----")
        mocker.patch("shared.leadsun_client.ssl.create_default_context")

        mock_session_instance = mocker.MagicMock()
        mock_session_instance.get.side_effect = RuntimeError("connection failed")
        mocker.patch("shared.leadsun_client.requests.Session", return_value=mock_session_instance)

        with pytest.raises(RuntimeError, match="connection failed"):
            leadsun_client.fetch_lamps()

        mock_session_instance.close.assert_called_once()
