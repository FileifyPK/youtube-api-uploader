"""OAuth helper (Plan v2, Phase A).

Least-privilege scopes ONLY — never the broad ``youtube`` scope::

    https://www.googleapis.com/auth/youtube.upload
    https://www.googleapis.com/auth/youtube.force-ssl

Security rules (Plan v2 §3, step 4):

* The user performs Google consent in HIS OWN browser. This module
  only builds the consent URL; it never opens a browser itself.
* The authorization code, refresh token, and client secret go
  straight into the Secure Vault via the secure flow — NEVER through
  chat, logs, files, or GitHub.
* No function here prints or logs a code, token, or secret. Error
  messages name what is missing, never what its value is.

Token lifecycle: the consent screen must be published in Production
(Testing-mode tokens expire after 7 days). Each agent holds its own
refresh token in its own Secure Vault; one revocation must not stop
the other agents.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build as _discovery_build

from youtube_uploader.config import Settings

log = logging.getLogger(__name__)

# Least-privilege scopes. youtube.upload covers videos.insert and
# thumbnails.set; youtube.force-ssl covers captions and playlists.
# The broad "youtube" scope (full account management) is NOT needed
# and must never be added.
SCOPES: list[str] = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_auth_url(
    client_id: str, redirect_uri: str, state: str | None = None
) -> str:
    """Build the Google OAuth consent URL for the user to open.

    The user opens this URL in HIS OWN browser and clicks Allow. The
    resulting authorization code goes straight to the Secure Vault —
    never through chat.

    ``access_type=offline`` is what makes Google return a refresh
    token; ``prompt=consent`` forces the consent screen every time so
    a refresh token is issued even if the user approved before;
    ``include_granted_scopes=false`` keeps the grant to exactly the
    scopes listed here.
    """
    if not client_id:
        raise ValueError("client_id is required to build the auth URL")
    if not redirect_uri:
        raise ValueError("redirect_uri is required to build the auth URL")
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    if state:
        params["state"] = state
    return f"{GOOGLE_AUTH_URI}?{urlencode(params)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens.

    Returns ``{"refresh_token": ..., "access_token": ...,
    "expiry": ...}`` (expiry as an ISO-8601 string, or None).

    The caller MUST move the refresh token straight into the Secure
    Vault and must never log, print, or paste the code or any token.
    This function logs nothing about the code or the tokens — on
    failure it raises the library error without the code attached.
    """
    if not client_id or not client_secret:
        raise ValueError(
            "client_id and client_secret are required to exchange a code"
        )
    if not code:
        raise ValueError("authorization code is required")
    if not redirect_uri:
        raise ValueError("redirect_uri is required to exchange a code")

    # Desktop-app OAuth client (Plan v2 §3, step 3).
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    # The code is passed to the library only — never logged.
    flow.fetch_token(code=code)
    creds = flow.credentials
    if creds.refresh_token is None:
        raise ValueError(
            "Google did not return a refresh token. The consent screen "
            "must be re-run with prompt=consent (see build_auth_url) "
            "and the app must be published in Production — Testing-mode "
            "grants and repeat consents without prompt=consent may not "
            "issue a refresh token."
        )
    return {
        "refresh_token": creds.refresh_token,
        "access_token": creds.token,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def refresh_access_token(settings: Settings) -> Credentials:
    """Build credentials from Settings and refresh the access token.

    Raises ValueError if any credential is missing (naming only which
    field is missing, never any value). The refresh itself is a
    network call to Google's token endpoint — callers must only call
    this when they genuinely need a live service object.
    """
    missing = [
        name
        for name, value in (
            ("GOOGLE_CLIENT_ID", settings.google_client_id),
            ("GOOGLE_CLIENT_SECRET", settings.google_client_secret),
            ("YOUTUBE_REFRESH_TOKEN", settings.youtube_refresh_token),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing Google OAuth credentials: "
            + ", ".join(missing)
            + ". These come from the owner's consent step (Plan v2 §3) "
            "and live in the Secure Vault — never in chat or files."
        )
    creds = Credentials(
        token=None,
        refresh_token=settings.youtube_refresh_token,
        token_uri=GOOGLE_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    log.info("YouTube access token refreshed successfully")
    return creds


def build_youtube_service(credentials: Any) -> Any:
    """Build the YouTube Data API v3 service from credentials.

    ``credentials`` is injected so tests can pass fakes; production
    passes the result of :func:`refresh_access_token`.
    """
    return _discovery_build("youtube", "v3", credentials=credentials)


def verify_channel(
    service: Any, expected_channel_id: str | None = None
) -> dict[str, Any]:
    """Confirm which channel the credentials authenticate as.

    Calls ``channels.list(mine=True)``. Returns a small dict with the
    channel id, title, and subscriber count. If
    ``expected_channel_id`` is given and the authenticated channel
    differs, raises ValueError — an upload must never run against the
    wrong channel.
    """
    response = service.channels().list(
        part="snippet,statistics", mine=True
    ).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError(
            "No YouTube channel found for the authenticated account. "
            "The OAuth token may belong to an account without a channel."
        )
    channel = items[0]
    channel_id = channel.get("id", "")
    if expected_channel_id and channel_id != expected_channel_id:
        raise ValueError(
            "Authenticated channel does not match the expected channel "
            f"(got {channel_id!r}, expected {expected_channel_id!r}). "
            "Refusing to proceed — uploads must target Sacred Knowledge."
        )
    return {
        "channel_id": channel_id,
        "title": channel.get("snippet", {}).get("title", ""),
        "subscriber_count": channel.get("statistics", {}).get(
            "subscriberCount"
        ),
    }
