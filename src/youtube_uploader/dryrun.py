"""Dry-run service and fake queue (Plan v2, Phase B/E).

:class:`DryRunService` mimics the ``googleapiclient`` resource surface
used by the upload engine (``videos()``, ``captions()``,
``thumbnails()``, ``playlistItems()``, ``channels()``, ``search()``)
and records every call in :attr:`DryRunService.calls` so tests can
assert on exactly what the engine tried to do.

The one hard rule: :meth:`videos().insert()
< DryRunService.videos>` raises :class:`DryRunBlockedError` — a dry run
can NEVER call the YouTube upload endpoint, even if the caller
forgot to check ``settings.dry_run`` first. Every other method returns
a canned empty/success response.

The in-memory queue stand-in :class:`FakeQueue` lives in
:mod:`youtube_uploader.queue` (the single canonical implementation,
thread-safe, enforcing the state machine) and is re-exported here so
dry-run callers can import both fakes from one module. It doubles as
the :class:`~youtube_uploader.quota.QuotaStore` (``get_usage`` /
``record``) so the whole pipeline can run in tests and in
``cli.py dry-run-upload`` without a database.

Both classes make NO network calls.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from googleapiclient.http import MediaUpload

from youtube_uploader.config import Settings
from youtube_uploader.errors import DryRunBlockedError
from youtube_uploader.queue import FakeQueue  # noqa: F401  (re-export)

log = logging.getLogger(__name__)


def assert_not_dry_run(settings: Settings) -> None:
    """Raise :class:`DryRunBlockedError` if dry-run mode is on.

    The engine's :meth:`ResumableUploader.upload` calls this FIRST,
    before touching the queue or the quota tracker.
    """
    if settings.dry_run:
        raise DryRunBlockedError(
            "Dry-run mode is ON (DRY_RUN=true): refusing to perform a "
            "real YouTube upload. Set DRY_RUN=false only with the "
            "owner's explicit approval for a real upload."
        )


# ---------------------------------------------------------------------------
# Dry-run YouTube service
# ---------------------------------------------------------------------------

# Canned responses keyed by (resource, method). Anything not listed here
# returns an empty dict — safe for read-only verification calls.
_CANNED: dict[tuple[str, str], dict[str, Any]] = {
    ("channels", "list"): {
        "items": [
            {
                "id": "DRY_RUN_CHANNEL_ID",
                "snippet": {"title": "Dry Run Channel"},
                "statistics": {"subscriberCount": "0"},
            }
        ]
    },
    ("videos", "list"): {"items": []},
    ("captions", "list"): {"items": []},
    ("captions", "insert"): {"id": "DRY_RUN_CAPTION_ID"},
    ("thumbnails", "set"): {"items": [{"id": "DRY_RUN_VIDEO_ID"}]},
    ("playlistItems", "list"): {"items": [], "pageInfo": {"totalResults": 0}},
    ("playlistItems", "insert"): {"id": "DRY_RUN_PLAYLIST_ITEM_ID"},
    ("search", "list"): {"items": []},
}


class _FakeRequest:
    """Stands in for a googleapiclient HttpRequest (``.execute()``)."""

    def __init__(
        self, service: "DryRunService", resource: str, method: str
    ) -> None:
        self._service = service
        self._resource = resource
        self._method = method
        # Attributes the engine reads on a real HttpRequest. They stay
        # empty here because a dry run never uploads any bytes.
        self.resumable_uri: str | None = None
        self.resumable_progress: int = 0

    def execute(self) -> dict[str, Any]:
        return copy.deepcopy(
            _CANNED.get((self._resource, self._method), {})
        )

    def next_chunk(self, num_retries: int = 0) -> Any:
        # Any resumable upload attempt through the dry-run service is
        # blocked, even if it somehow got past videos().insert().
        raise DryRunBlockedError(
            "Dry-run service: resumable upload chunk blocked — "
            "dry runs never call videos.insert."
        )


class _FakeResource:
    """One resource of the fake service (e.g. ``videos``)."""

    def __init__(self, service: "DryRunService", name: str) -> None:
        self._service = service
        self._name = name

    @staticmethod
    def _safe_record(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Copy kwargs for the call log, tolerating un-copyable values.

        ``media_body`` is a MediaFileUpload holding an open file
        handle — it cannot be deep-copied. Such values are recorded
        as a short ``<TypeName>`` placeholder instead of crashing.
        """
        recorded: dict[str, Any] = {}
        for key, value in kwargs.items():
            if isinstance(value, MediaUpload):
                # Never deepcopy a media upload: it holds an open file
                # handle, and even a failed deepcopy attempt leaves a
                # noisy __del__ warning behind at GC time.
                recorded[key] = f"<{type(value).__name__}>"
                continue
            try:
                recorded[key] = copy.deepcopy(value)
            except Exception:  # noqa: BLE001 — any un-copyable value
                recorded[key] = f"<{type(value).__name__}>"
        return recorded

    def __getattr__(self, method: str) -> Any:
        def _call(**kwargs: Any) -> _FakeRequest:
            # Record the call BEFORE doing anything else so tests can
            # see even blocked attempts.
            self._service.calls.append(
                (self._name, method, self._safe_record(kwargs))
            )
            if self._name == "videos" and method == "insert":
                raise DryRunBlockedError(
                    "Dry-run service: videos.insert is blocked — "
                    "a dry run must never upload to YouTube."
                )
            return _FakeRequest(self._service, self._name, method)

        return _call


class DryRunService:
    """Fake YouTube Data API v3 service for dry runs and tests.

    Every call is recorded in :attr:`calls` as
    ``(resource, method, kwargs)`` tuples. ``videos().insert()`` always
    raises :class:`DryRunBlockedError`; all other methods return canned
    empty/success responses via ``.execute()``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def videos(self) -> _FakeResource:
        return _FakeResource(self, "videos")

    def captions(self) -> _FakeResource:
        return _FakeResource(self, "captions")

    def thumbnails(self) -> _FakeResource:
        return _FakeResource(self, "thumbnails")

    def playlistItems(self) -> _FakeResource:
        return _FakeResource(self, "playlistItems")

    def channels(self) -> _FakeResource:
        return _FakeResource(self, "channels")

    def search(self) -> _FakeResource:
        return _FakeResource(self, "search")

# ---------------------------------------------------------------------------
# Fake queue (re-exported from queue.py — single canonical implementation)
# ---------------------------------------------------------------------------

__all__ = ["DryRunService", "FakeQueue", "assert_not_dry_run"]
