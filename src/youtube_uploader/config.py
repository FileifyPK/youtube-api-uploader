"""Configuration — environment variables only, secrets never logged.

Real credentials do not exist yet: the user performs Google consent in
his own browser and the resulting refresh token goes straight into the
Secure Vault. This module only reads values already present in the
environment; it never prompts for, prints, or persists secrets.

Security rules enforced here:
- ``repr``/``str`` of Settings never includes secret values.
- ``UPLOADS_FORCE_OFF`` can only ever turn uploads OFF, never ON.
- DRY_RUN defaults to True.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Keys whose values must never appear in logs or error messages.
SECRET_KEYS = frozenset(
    {
        "GOOGLE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "DATABASE_URL",
    }
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def redact(mapping: dict) -> dict:
    """Return a copy of ``mapping`` with secret values masked."""
    return {
        k: ("***" if k in SECRET_KEYS else v) for k, v in mapping.items()
    }


@dataclass(frozen=True)
class Settings:
    agent_id: str = "muse"
    google_client_id: str = ""
    google_client_secret: str = ""
    youtube_refresh_token: str = ""
    database_url: str = ""
    dry_run: bool = True
    uploads_force_off: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            agent_id=os.environ.get("AGENT_ID", "muse"),
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            youtube_refresh_token=os.environ.get("YOUTUBE_REFRESH_TOKEN", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            dry_run=_env_bool("DRY_RUN", True),
            uploads_force_off=_env_bool("UPLOADS_FORCE_OFF", True),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )

    def __repr__(self) -> str:
        # Never leak secrets via repr/str.
        return (
            f"Settings(agent_id={self.agent_id!r}, "
            f"google_client_id={'<set>' if self.google_client_id else '<unset>'}, "
            f"google_client_secret={'<set>' if self.google_client_secret else '<unset>'}, "
            f"youtube_refresh_token={'<set>' if self.youtube_refresh_token else '<unset>'}, "
            f"database_url={'<set>' if self.database_url else '<unset>'}, "
            f"dry_run={self.dry_run!r}, uploads_force_off={self.uploads_force_off!r}, "
            f"log_level={self.log_level!r})"
        )

    def has_google_credentials(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.youtube_refresh_token
        )

    def has_database(self) -> bool:
        return bool(self.database_url)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
