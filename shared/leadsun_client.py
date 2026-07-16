import logging
import os
import ssl
import tempfile

import requests
from requests.adapters import HTTPAdapter

LEADSUN_API_URL = os.environ.get("LEADSUN_API_URL", "https://leadsunedge-us.com:8550/lamps")

# Kept as a separate setting from LEADSUN_API_URL (not renamed to something
# like LEADSUN_LAMPS_URL) so an already-configured LEADSUN_API_URL setting
# keeps working unchanged for existing deployments.
LEADSUN_MODELS_URL = os.environ.get(
    "LEADSUN_MODELS_URL", "https://leadsunedge-us.com:8550/models"
)

# Combined certificate + unencrypted private key, PEM format (both blocks
# in one file -- confirmed against the cert provided while building this:
# one "BEGIN CERTIFICATE" block, one unencrypted "BEGIN PRIVATE KEY" block).
# Stored as an app setting rather than a file in the repo, same reasoning
# as AIRTABLE_API_KEY/SQL_CONNECTION_STRING: it's a credential, and this
# project already keeps local.settings.json (which holds the real value)
# out of source control.
LEADSUN_CLIENT_CERT_PEM = os.environ["LEADSUN_CLIENT_CERT_PEM"]

# Separate concern from the client cert above: this is for verifying the
# SERVER's certificate. leadsunedge-us.com presents a self-signed cert,
# which the public CA bundle requests/certifi ships with doesn't trust --
# that's what SSLCertVerificationError("self-signed certificate") means.
# Set this to the PEM text of that server cert (or its issuing CA) to pin
# trust to it specifically, the same way LEADSUN_CLIENT_CERT_PEM is
# stored. Leave unset to fall back to the default public CA bundle
# (verify=True), which is what fails against a self-signed server cert.
LEADSUN_SERVER_CA_CERT = os.environ.get("LEADSUN_SERVER_CA_CERT")

# A second, separate problem that can surface even once the cert above is
# trusted: its Common Name/SAN may not match "leadsunedge-us.com" at all
# (common for lightweight self-signed certs on IoT gateways reused across
# deployments) -- SSLCertVerificationError("Hostname mismatch..."). This
# skips ONLY the hostname check while still requiring the certificate
# chain to validate against LEADSUN_SERVER_CA_CERT (or the system default
# trust store if that isn't set, though that combination won't help with a
# self-signed cert on its own -- see the warning in fetch_lamps()).
LEADSUN_SKIP_HOSTNAME_CHECK = (
    os.environ.get("LEADSUN_SKIP_HOSTNAME_CHECK", "").strip().lower() == "true"
)

# Escape hatch ONLY -- disables server certificate verification entirely,
# which makes the connection vulnerable to man-in-the-middle tampering.
# Prefer LEADSUN_SERVER_CA_CERT (+ LEADSUN_SKIP_HOSTNAME_CHECK if needed)
# above; only reach for this if the real server cert/CA genuinely isn't
# obtainable and the risk is accepted.
LEADSUN_SKIP_TLS_VERIFY = os.environ.get("LEADSUN_SKIP_TLS_VERIFY", "").strip().lower() == "true"


class _NoHostnameCheckAdapter(HTTPAdapter):
    """
    A requests HTTPAdapter that still validates the server's certificate
    chain via the given SSLContext, but with hostname verification turned
    off. This needs turning off in TWO separate places: the SSLContext's
    own check_hostname (Python's ssl module), AND urllib3's independent
    assert_hostname pool-level check, which runs underneath requests'
    verify= handling regardless of the SSLContext's setting. Missing
    either one still raises a hostname-mismatch error.
    """

    def __init__(self, ssl_context, *args, **kwargs):
        self._ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        kwargs["assert_hostname"] = False
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        kwargs["assert_hostname"] = False
        return super().proxy_manager_for(*args, **kwargs)


def _build_no_hostname_check_ssl_context(ca_cert_path):
    """
    SSLContext that validates the certificate chain (against ca_cert_path
    if given, otherwise the system's default trust store) but skips
    hostname verification.
    """
    context = (
        ssl.create_default_context(cafile=ca_cert_path)
        if ca_cert_path
        else ssl.create_default_context()
    )
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


_PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)


def _validate_pem_has_certificate(pem_content: str, setting_name: str) -> None:
    """
    Fails fast with a clear, actionable message if pem_content is missing a
    certificate block, instead of letting a mangled value fail deep inside
    urllib3/OpenSSL with a cryptic "[SSL] PEM lib" error that gives no clue
    which setting or what's actually wrong. This exact failure mode has
    already bitten this setup once (JSON-escaping in local.settings.json)
    and once more on the Azure side (the app setting's value likely got
    truncated or had its newlines flattened when set via the Portal UI) --
    worth catching early rather than debugging via stack trace each time.
    """
    if "-----BEGIN CERTIFICATE-----" not in pem_content:
        raise ValueError(
            f"{setting_name} doesn't contain a '-----BEGIN CERTIFICATE-----' "
            f"block. This usually means the app setting's value got "
            f"truncated, had its newlines flattened, or still has 'Bag "
            f"Attributes' metadata lines left in from an openssl pkcs12 "
            f"export. Re-check the value against the original .pem file --"
            f"setting it via `az functionapp config appsettings set "
            f"--settings \"{setting_name}=$(cat file.pem)\"` avoids the "
            f"Portal text box mangling multi-line values."
        )


def _write_pem_to_temp_file(pem_content: str) -> str:
    """Materializes PEM text to a temp file (requests' cert=/verify=
    parameters need actual filesystem paths, not raw PEM text). Caller is
    responsible for deleting the returned path when done."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    try:
        tmp.write(pem_content)
    finally:
        tmp.close()
    return tmp.name


def _write_client_cert_to_temp_file() -> str:
    """
    Materializes LEADSUN_CLIENT_CERT_PEM to a temp file. Written fresh on
    every call (cheap -- a few KB) so a rotated cert setting takes effect
    on the very next run without needing a restart.
    """
    _validate_pem_has_certificate(LEADSUN_CLIENT_CERT_PEM, "LEADSUN_CLIENT_CERT_PEM")
    if not any(marker in LEADSUN_CLIENT_CERT_PEM for marker in _PRIVATE_KEY_MARKERS):
        raise ValueError(
            "LEADSUN_CLIENT_CERT_PEM doesn't contain a private key block "
            "(expected '-----BEGIN ... PRIVATE KEY-----'). Same likely "
            "causes as a missing certificate block -- the setting's value "
            "is probably truncated or mangled. Re-set it via `az functionapp "
            'config appsettings set --settings "LEADSUN_CLIENT_CERT_PEM='
            '$(cat file.pem)"` to avoid Portal text-box mangling.'
        )
    return _write_pem_to_temp_file(LEADSUN_CLIENT_CERT_PEM)


def _resolve_verify_option():
    """
    Returns whatever should be passed as requests' verify= kwarg:
      - False if LEADSUN_SKIP_TLS_VERIFY=true (insecure escape hatch)
      - a temp file path if LEADSUN_SERVER_CA_CERT is set (verify against
        that specific pinned cert/CA instead of the public bundle)
      - True otherwise (default public CA bundle -- fails against a
        self-signed server cert, which is the problem this exists to fix)
    """
    if LEADSUN_SKIP_TLS_VERIFY:
        logging.warning(
            "leadsun_client: TLS server certificate verification is DISABLED "
            "(LEADSUN_SKIP_TLS_VERIFY=true). This is insecure -- only use for "
            "temporary testing. Prefer setting LEADSUN_SERVER_CA_CERT instead."
        )
        return False
    if LEADSUN_SERVER_CA_CERT:
        _validate_pem_has_certificate(LEADSUN_SERVER_CA_CERT, "LEADSUN_SERVER_CA_CERT")
        return _write_pem_to_temp_file(LEADSUN_SERVER_CA_CERT)
    return True


def _get(url: str) -> list:
    """
    Shared GET logic for both Leadsun endpoints (/lamps, /models): mutual
    TLS with the client cert, plus whichever server-certificate
    verification mode is configured (default, pinned CA, hostname-check
    bypass, or fully disabled).

    Returns the parsed JSON response body, in whatever shape/casing the
    Leadsun API sends it (capitalization/renaming happens in the loader
    modules, not here).
    """
    cert_path = _write_client_cert_to_temp_file()
    verify_option = _resolve_verify_option()
    verify_is_temp_file = isinstance(verify_option, str)
    session = None
    try:
        if LEADSUN_SKIP_HOSTNAME_CHECK and not LEADSUN_SKIP_TLS_VERIFY:
            ca_cert_path = verify_option if verify_is_temp_file else None
            if ca_cert_path is None:
                logging.warning(
                    "leadsun_client: LEADSUN_SKIP_HOSTNAME_CHECK is set without "
                    "LEADSUN_SERVER_CA_CERT -- chain validation falls back to the "
                    "system's default trust store, which will still reject a "
                    "self-signed certificate. Set LEADSUN_SERVER_CA_CERT too."
                )
            ssl_context = _build_no_hostname_check_ssl_context(ca_cert_path)
            session = requests.Session()
            session.mount("https://", _NoHostnameCheckAdapter(ssl_context))
            response = session.get(url, cert=cert_path, timeout=30)
        else:
            response = requests.get(url, cert=cert_path, verify=verify_option, timeout=30)
        response.raise_for_status()
        return response.json()
    finally:
        os.unlink(cert_path)
        if verify_is_temp_file:
            os.unlink(verify_option)
        if session is not None:
            session.close()


def fetch_lamps() -> list:
    """
    Fetches every lamp/pole record from the Leadsun /lamps endpoint.

    ASSUMPTIONS -- unverified, since this sandbox has no network path to
    leadsunedge-us.com to confirm against a real response:
      - One GET returns everything; no pagination params are sent. If the
        real API paginates, this needs an offset/cursor loop added, similar
        to shared/airtable_client.py's fetch_all_records().
      - The response body is a JSON array of lamp objects directly, not
        wrapped in an envelope like {"data": [...]} or {"lamps": [...]}.
    """
    return _get(LEADSUN_API_URL)


def fetch_models() -> list:
    """
    Fetches every pole/lamp model definition from the Leadsun /models
    endpoint (confirmed: plain JSON array, no pagination, same as /lamps).
    """
    return _get(LEADSUN_MODELS_URL)
