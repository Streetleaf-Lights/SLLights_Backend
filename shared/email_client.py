"""
Sends email through Microsoft 365 / Exchange Online via the Microsoft
Graph API, using the OAuth2 client-credentials flow (an app-only,
service-account-style token -- no interactive user sign-in involved).

Deliberately NOT using SMTP directly: Microsoft has been disabling Basic
Auth for SMTP AUTH across tenants by default for security reasons, so a
plain smtplib-based sender is liable to simply stop working depending on
tenant configuration, or need it re-enabled specifically (a security
regression) to function at all. Graph's application-permission Mail.Send
is the currently-supported, documented way to send mail from a backend
service without a signed-in user.

Deliberately NOT using the `msal` library either -- this project already
depends on `requests` for the Airtable/Leadsun HTTP clients, and the
client-credentials token request is a single, simple POST with no need
for MSAL's broader interactive/caching machinery, so this avoids adding a
second HTTP/auth library for what's a one-shot token fetch.

Required app settings (set these up as an Azure AD App Registration with
Microsoft Graph's Mail.Send APPLICATION permission, admin-consented --
not Mail.Send as a DELEGATED permission, which requires a signed-in user
this backend doesn't have):
  MS365_TENANT_ID      -- the Azure AD tenant id (a GUID, or the tenant's
                           verified domain, e.g. "streetleaf.com")
  MS365_CLIENT_ID      -- the App Registration's Application (client) id
  MS365_CLIENT_SECRET  -- a client secret created for that App Registration
  MS365_SENDER_EMAIL   -- the mailbox to send from, e.g. "noreply@streetleaf.com"
                           (must be a real, licensed mailbox in the tenant)
"""

import logging
import os

import requests

_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_GRAPH_SEND_MAIL_URL_TEMPLATE = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"


class EmailSendError(Exception):
    """Raised when either the Graph token request or the send-mail
    request itself fails -- callers decide how to surface this (e.g.
    invite_user() still succeeds in creating the Pending user row even
    if the invite email fails to send, logging the failure rather than
    losing the whole operation over a transient mail-sending issue)."""


def _get_graph_access_token() -> str:
    tenant = os.environ["MS365_TENANT_ID"]
    client_id = os.environ["MS365_CLIENT_ID"]
    client_secret = os.environ["MS365_CLIENT_SECRET"]

    response = requests.post(
        _TOKEN_URL_TEMPLATE.format(tenant=tenant),
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": _GRAPH_SCOPE,
        },
        timeout=15,
    )
    if response.status_code != 200:
        raise EmailSendError(
            f"failed to acquire Microsoft Graph access token: "
            f"{response.status_code} {response.text}"
        )
    return response.json()["access_token"]


def send_email(to_address: str, subject: str, body_html: str) -> None:
    """
    Sends a single HTML email via Microsoft Graph, from the mailbox
    configured in MS365_SENDER_EMAIL. Raises EmailSendError on any
    failure (token acquisition or the send itself) -- does not retry;
    callers that want the surrounding operation (e.g. invite_user()) to
    succeed even if the email doesn't should catch this specifically,
    not let it propagate as an unhandled 500.
    """
    sender = os.environ["MS365_SENDER_EMAIL"]
    access_token = _get_graph_access_token()

    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
        },
        "saveToSentItems": "false",
    }

    response = requests.post(
        _GRAPH_SEND_MAIL_URL_TEMPLATE.format(sender=sender),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=message,
        timeout=15,
    )
    # sendMail returns 202 Accepted with an empty body on success -- not
    # 200/201, which would be the more typical REST convention.
    if response.status_code != 202:
        raise EmailSendError(
            f"Microsoft Graph sendMail failed: {response.status_code} {response.text}"
        )
    logging.info("email_client: sent email to %s (subject: %s)", to_address, subject)
