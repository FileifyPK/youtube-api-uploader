"""Configuration tests: defaults, env overrides, secret hygiene."""

import pytest

from youtube_uploader.config import SECRET_KEYS, Settings, redact

ALL_ENV_KEYS = [
    "AGENT_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
    "DATABASE_URL",
    "DRY_RUN",
    "UPLOADS_FORCE_OFF",
    "LOG_LEVEL",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_safe(clean_env):
    settings = Settings.from_env()
    # Dry-run ON and uploads force-OFF by default: nothing can upload
    # unless explicitly enabled.
    assert settings.dry_run is True
    assert settings.uploads_force_off is True
    assert settings.agent_id == "muse"
    assert settings.log_level == "INFO"
    assert settings.google_client_id == ""
    assert settings.google_client_secret == ""
    assert settings.youtube_refresh_token == ""
    assert settings.database_url == ""


def test_env_overrides(clean_env, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "meem")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("UPLOADS_FORCE_OFF", "false")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "FAKE_CLIENT_ID_VALUE")
    settings = Settings.from_env()
    assert settings.agent_id == "meem"
    assert settings.dry_run is False
    assert settings.uploads_force_off is False
    assert settings.log_level == "DEBUG"
    assert settings.google_client_id == "FAKE_CLIENT_ID_VALUE"


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("YES", True), ("on", True),
                                          ("0", False), ("false", False), ("no", False), ("off", False)])
def test_env_bool_parsing(clean_env, monkeypatch, raw, expected):
    monkeypatch.setenv("DRY_RUN", raw)
    assert Settings.from_env().dry_run is expected


def test_repr_never_contains_secrets(clean_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "FAKE_CLIENT_SECRET_VALUE")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "FAKE_REFRESH_TOKEN_VALUE")
    monkeypatch.setenv("DATABASE_URL", "FAKE_DATABASE_URL_VALUE")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "FAKE_CLIENT_ID_VALUE")
    settings = Settings.from_env()
    for text in (repr(settings), str(settings)):
        assert "FAKE_CLIENT_SECRET_VALUE" not in text
        assert "FAKE_REFRESH_TOKEN_VALUE" not in text
        assert "FAKE_DATABASE_URL_VALUE" not in text
        # Client IDs are not secret, but repr shows set/unset only.
        assert "FAKE_CLIENT_ID_VALUE" not in text
    assert "<set>" in repr(settings)


def test_redact_masks_secret_keys():
    mapping = {
        "GOOGLE_CLIENT_SECRET": "FAKE_CLIENT_SECRET_VALUE",
        "YOUTUBE_REFRESH_TOKEN": "FAKE_REFRESH_TOKEN_VALUE",
        "DATABASE_URL": "FAKE_DATABASE_URL_VALUE",
        "AGENT_ID": "muse",
        "DRY_RUN": True,
    }
    redacted = redact(mapping)
    assert redacted["GOOGLE_CLIENT_SECRET"] == "***"
    assert redacted["YOUTUBE_REFRESH_TOKEN"] == "***"
    assert redacted["DATABASE_URL"] == "***"
    assert redacted["AGENT_ID"] == "muse"
    assert redacted["DRY_RUN"] is True
    # The original mapping is untouched.
    assert mapping["GOOGLE_CLIENT_SECRET"] == "FAKE_CLIENT_SECRET_VALUE"
    assert SECRET_KEYS == {
        "GOOGLE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "DATABASE_URL",
    }


def test_has_google_credentials():
    assert Settings().has_google_credentials() is False
    assert (
        Settings(google_client_id="FAKE_ID").has_google_credentials()
        is False
    )
    assert (
        Settings(
            google_client_id="FAKE_ID",
            google_client_secret="FAKE_SECRET",
        ).has_google_credentials()
        is False
    )
    assert (
        Settings(
            google_client_id="FAKE_ID",
            google_client_secret="FAKE_SECRET",
            youtube_refresh_token="FAKE_TOKEN",
        ).has_google_credentials()
        is True
    )


def test_has_database():
    assert Settings().has_database() is False
    assert Settings(database_url="FAKE_DATABASE_URL_VALUE").has_database() is True
