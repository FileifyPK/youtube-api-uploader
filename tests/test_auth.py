"""Tests for the OAuth helper (auth.py).

No network: every Google call is mocked. The one thing these tests
cannot prove is a live token refresh against Google — that needs the
owner's real consent + refresh token (setup day), and is recorded as
such in the acceptance checklist.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from youtube_uploader import auth
from youtube_uploader.config import Settings


# --- scopes ---------------------------------------------------------------


def test_scopes_are_least_privilege_only():
    assert auth.SCOPES == [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]
    # The broad "youtube" scope must never appear as its own scope.
    assert "https://www.googleapis.com/auth/youtube" not in auth.SCOPES


# --- build_auth_url --------------------------------------------------------


def test_build_auth_url_params():
    url = auth.build_auth_url(
        "CID.apps.googleusercontent.com",
        "http://localhost:8080/",
        state="xyz",
    )
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["CID.apps.googleusercontent.com"]
    assert qs["redirect_uri"] == ["http://localhost:8080/"]
    assert qs["scope"] == [" ".join(auth.SCOPES)]
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    assert qs["include_granted_scopes"] == ["false"]
    assert qs["state"] == ["xyz"]


def test_build_auth_url_requires_client_and_redirect():
    with pytest.raises(ValueError):
        auth.build_auth_url("", "http://localhost/")
    with pytest.raises(ValueError):
        auth.build_auth_url("CID", "")


# --- exchange_code (Flow mocked) -------------------------------------------


def _fake_flow(refresh_token="FAKE_REFRESH_TOKEN"):
    flow = MagicMock()
    creds = MagicMock()
    creds.refresh_token = refresh_token
    creds.token = "FAKE_ACCESS_TOKEN"
    creds.expiry = datetime(2026, 11, 21, tzinfo=timezone.utc)
    flow.credentials = creds
    return flow


def test_exchange_code_returns_tokens_and_never_logs_code(caplog):
    flow = _fake_flow()
    with patch(
        "youtube_uploader.auth.Flow.from_client_config",
        return_value=flow,
    ) as from_config:
        with caplog.at_level("INFO", logger="youtube_uploader.auth"):
            result = auth.exchange_code(
                "CID", "FAKE_SECRET", "FAKE_AUTH_CODE", "http://localhost/"
            )
    from_config.assert_called_once()
    # The flow received the client config with the two scopes only.
    _, kwargs = from_config.call_args
    assert kwargs["scopes"] == auth.SCOPES
    flow.fetch_token.assert_called_once_with(code="FAKE_AUTH_CODE")
    assert result["refresh_token"] == "FAKE_REFRESH_TOKEN"
    assert result["access_token"] == "FAKE_ACCESS_TOKEN"
    assert result["expiry"] == "2026-11-21T00:00:00+00:00"
    # The code and secret must not appear in any log record.
    for record in caplog.records:
        assert "FAKE_AUTH_CODE" not in record.getMessage()
        assert "FAKE_SECRET" not in record.getMessage()


def test_exchange_code_no_refresh_token_raises():
    flow = _fake_flow(refresh_token=None)
    with patch(
        "youtube_uploader.auth.Flow.from_client_config",
        return_value=flow,
    ):
        with pytest.raises(ValueError, match="did not return a refresh"):
            auth.exchange_code(
                "CID", "SECRET", "CODE", "http://localhost/"
            )


def test_exchange_code_validates_inputs():
    with pytest.raises(ValueError):
        auth.exchange_code("", "S", "C", "http://localhost/")
    with pytest.raises(ValueError):
        auth.exchange_code("CID", "S", "", "http://localhost/")
    with pytest.raises(ValueError):
        auth.exchange_code("CID", "S", "C", "")


# --- refresh_access_token (Credentials.refresh mocked) ---------------------


def _settings():
    return Settings(
        google_client_id="FAKE_CLIENT_ID",
        google_client_secret="FAKE_CLIENT_SECRET",
        youtube_refresh_token="FAKE_REFRESH_TOKEN",
    )


def test_refresh_access_token_builds_and_refreshes():
    settings = _settings()
    with patch(
        "youtube_uploader.auth.Credentials.refresh",
        autospec=True,
    ) as mock_refresh:
        creds = auth.refresh_access_token(settings)
    mock_refresh.assert_called_once()
    assert creds.refresh_token == "FAKE_REFRESH_TOKEN"
    assert creds.client_id == "FAKE_CLIENT_ID"
    assert list(creds.scopes) == auth.SCOPES


def test_refresh_access_token_missing_credentials_names_field_not_value():
    settings = Settings(
        google_client_id="",
        google_client_secret="FAKE_CLIENT_SECRET",
        youtube_refresh_token="FAKE_REFRESH_TOKEN",
    )
    with pytest.raises(ValueError) as excinfo:
        auth.refresh_access_token(settings)
    assert "GOOGLE_CLIENT_ID" in str(excinfo.value)
    # The secret value must never appear in the error.
    assert "FAKE_CLIENT_SECRET" not in str(excinfo.value)
    assert "FAKE_REFRESH_TOKEN" not in str(excinfo.value)


# --- build_youtube_service -------------------------------------------------


def test_build_youtube_service_uses_discovery_build(monkeypatch):
    # No proxy configured -> plain discovery build, no http kwarg.
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    fake_creds = MagicMock()
    with patch(
        "youtube_uploader.auth._discovery_build",
        return_value="SERVICE",
    ) as mock_build:
        assert auth.build_youtube_service(fake_creds) == "SERVICE"
    mock_build.assert_called_once_with(
        "youtube", "v3", credentials=fake_creds
    )


def test_build_youtube_service_uses_proxy_when_configured(monkeypatch, tmp_path):
    # httplib2 does not read proxy env vars on its own; the service
    # must be built over a proxy-aware transport (else SSL
    # WRONG_VERSION_NUMBER behind this VM's proxy). googleapiclient
    # treats http and credentials as mutually exclusive, so the
    # proxy path wraps the transport in AuthorizedHttp and passes
    # ONLY http. The transport must also trust the egress CA bundle
    # (else CERTIFICATE_VERIFY_FAILED).
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    for var in ("https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    ca_file = tmp_path / "ca-bundle.pem"
    ca_file.write_text("fake-ca")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(var, raising=False)
    fake_creds = MagicMock()
    with patch(
        "youtube_uploader.auth._discovery_build",
        return_value="SERVICE",
    ) as mock_build:
        assert auth.build_youtube_service(fake_creds) == "SERVICE"
    mock_build.assert_called_once()
    _, kwargs = mock_build.call_args
    assert "credentials" not in kwargs
    http = kwargs["http"]
    assert isinstance(http, AuthorizedHttp)
    assert isinstance(http.http, httplib2.Http)
    assert http.http.proxy_info.proxy_host == "proxy.example"
    assert http.http.proxy_info.proxy_port == 8080
    assert http.http.ca_certs == str(ca_file)


# --- verify_channel --------------------------------------------------------


def _channel_service(channel_id="UC_SACRED", title="Sacred Knowledge"):
    service = MagicMock()
    service.channels.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": channel_id,
                "snippet": {"title": title},
                "statistics": {"subscriberCount": "1234"},
            }
        ]
    }
    return service


def test_verify_channel_ok():
    result = auth.verify_channel(_channel_service(), "UC_SACRED")
    assert result == {
        "channel_id": "UC_SACRED",
        "title": "Sacred Knowledge",
        "subscriber_count": "1234",
    }


def test_verify_channel_no_expected_id_skips_check():
    result = auth.verify_channel(_channel_service())
    assert result["channel_id"] == "UC_SACRED"


def test_verify_channel_mismatch_raises():
    with pytest.raises(ValueError, match="does not match"):
        auth.verify_channel(_channel_service(), "UC_OTHER")


def test_verify_channel_no_channel_raises():
    service = MagicMock()
    service.channels.return_value.list.return_value.execute.return_value = {
        "items": []
    }
    with pytest.raises(ValueError, match="No YouTube channel"):
        auth.verify_channel(service)
