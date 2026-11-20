"""Shared fakes and helpers for the Phase E test suite.

HARD RULES for this whole suite: no network, no real YouTube calls, no
real credentials, no live database. All YouTube interactions go through
:class:`FakeYouTubeService` / ``DryRunService``; all queue interaction
goes through ``FakeQueue``.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from youtube_uploader.config import Settings
from youtube_uploader.queue import FakeQueue
from youtube_uploader.quota import InMemoryQuotaStore, QuotaTracker
from youtube_uploader.state import JobStatus


# ---------------------------------------------------------------------------
# HttpError construction (googleapiclient needs a resp with .status/.reason)
# ---------------------------------------------------------------------------


class FakeHttpResponse:
    """Minimal ``resp`` object for ``googleapiclient.errors.HttpError``."""

    def __init__(self, status: int, reason: str = "Error"):
        self.status = status
        self.reason = reason


def make_http_error(
    status: int, reason: str = "Error", content: bytes = b"error"
) -> HttpError:
    return HttpError(FakeHttpResponse(status, reason), content)


def make_quota_error() -> HttpError:
    """A 403 quotaExceeded error, exactly as YouTube returns it."""
    return make_http_error(
        403,
        "Forbidden",
        b'{"error": {"errors": [{"reason": "quotaExceeded", '
        b'"message": "The request cannot be completed because you have '
        b'exceeded your quota."}]}}',
    )


# ---------------------------------------------------------------------------
# Fake resumable insert request (the googleapiclient next_chunk() surface)
# ---------------------------------------------------------------------------


class FakeStatus:
    """Stands in for googleapiclient's MediaUploadProgress."""

    def __init__(self, fraction: float):
        self._fraction = fraction

    def progress(self) -> float:
        return self._fraction


class FakeInsertRequest:
    """Fake ``videos().insert()`` request with a scripted ``next_chunk()``.

    Script actions (one per ``next_chunk()`` call; the last action
    repeats once the script is exhausted, so an "always failing"
    script never silently succeeds):

    - ``("progress", fraction)`` — return ``(FakeStatus(fraction), None)``
    - ``("done", video_id)`` — return ``(None, {"id": video_id})``
    - ``("raise", exc)`` — raise ``exc``
    """

    def __init__(self, script, uri: str = "https://upload.example/session/FAKE"):
        self._script = list(script)
        self._last = self._script[-1] if self._script else ("done", "VID_DEFAULT")
        self.resumable_uri: str | None = uri
        self.resumable_progress: int = 0
        self._in_error_state: bool = False
        self.next_chunk_calls: int = 0
        # State the uploader set before the first chunk (resume path).
        self.in_error_state_at_first_chunk: bool | None = None
        self.progress_at_first_chunk: int | None = None

    def next_chunk(self, num_retries: int = 0):
        if self.next_chunk_calls == 0:
            self.in_error_state_at_first_chunk = self._in_error_state
            self.progress_at_first_chunk = self.resumable_progress
        self.next_chunk_calls += 1
        action = self._script.pop(0) if self._script else self._last
        kind = action[0]
        if kind == "raise":
            raise action[1]
        if kind == "progress":
            return FakeStatus(action[1]), None
        if kind == "done":
            return None, {"id": action[1]}
        raise ValueError(f"unknown script action: {action!r}")


# ---------------------------------------------------------------------------
# Fake YouTube service (records every call; stateful privacy + playlist)
# ---------------------------------------------------------------------------


class _Executable:
    def __init__(self, payload: Any):
        self._payload = payload

    def execute(self):
        return copy.deepcopy(self._payload)


class _VideosResource:
    def __init__(self, service: "FakeYouTubeService"):
        self._service = service

    def insert(self, part=None, body=None, media_body=None):
        self._service._record(
            "videos", "insert", {"part": part, "body": copy.deepcopy(body)}
        )
        script = (
            self._service.insert_scripts.pop(0)
            if self._service.insert_scripts
            else [("done", self._service.default_video_id)]
        )
        req = FakeInsertRequest(script)
        self._service.insert_requests.append(req)
        return req

    def list(self, **kwargs):
        self._service._record("videos", "list", dict(kwargs))
        return _Executable(self._service.videos_list_payload())

    def update(self, part=None, body=None, **kwargs):
        self._service._record(
            "videos", "update", {"part": part, "body": copy.deepcopy(body)}
        )
        status = (body or {}).get("status", {})
        if "privacyStatus" in status:
            # The fake channel's privacy actually flips, so a
            # post-publish verify_video call sees "public".
            self._service.video_privacy = status["privacyStatus"]
        return _Executable({"id": (body or {}).get("id", "")})


class _CaptionsResource:
    def __init__(self, service: "FakeYouTubeService"):
        self._service = service

    def insert(self, part=None, body=None, media_body=None):
        self._service._record(
            "captions", "insert", {"part": part, "body": copy.deepcopy(body)}
        )
        return _Executable({"id": self._service.caption_id})

    def list(self, **kwargs):
        self._service._record("captions", "list", dict(kwargs))
        return _Executable({"items": copy.deepcopy(self._service.caption_items)})


class _ThumbnailsResource:
    def __init__(self, service: "FakeYouTubeService"):
        self._service = service

    def set(self, videoId=None, media_body=None):
        self._service._record("thumbnails", "set", {"videoId": videoId})
        self._service.thumbnail_set = True
        return _Executable({})


class _PlaylistItemsResource:
    def __init__(self, service: "FakeYouTubeService"):
        self._service = service

    def insert(self, part=None, body=None):
        self._service._record(
            "playlistItems",
            "insert",
            {"part": part, "body": copy.deepcopy(body)},
        )
        vid = (body or {}).get("snippet", {}).get("resourceId", {}).get("videoId")
        if vid:
            self._service.playlist_video_ids.add(vid)
        return _Executable({"id": self._service.playlist_item_id})

    def list(self, **kwargs):
        self._service._record("playlistItems", "list", dict(kwargs))
        items = [
            {
                "snippet": {
                    "resourceId": {"kind": "youtube#video", "videoId": vid}
                }
            }
            for vid in sorted(self._service.playlist_video_ids)
        ]
        return _Executable({"items": items})


class _SearchResource:
    def __init__(self, service: "FakeYouTubeService"):
        self._service = service

    def list(self, **kwargs):
        self._service._record("search", "list", dict(kwargs))
        return _Executable({"items": copy.deepcopy(self._service.search_items)})


class _ChannelsResource:
    def __init__(self, service: "FakeYouTubeService"):
        self._service = service

    def list(self, **kwargs):
        self._service._record("channels", "list", dict(kwargs))
        return _Executable({"items": copy.deepcopy(self._service.channel_items)})


class FakeYouTubeService:
    """Recording fake for the YouTube Data API v3 surface the code uses.

    Every call is recorded in ``.calls`` as ``(resource, method,
    kwargs)``. ``videos().insert()`` returns a scripted
    :class:`FakeInsertRequest` (one script per insert call, popped
    from ``insert_scripts``; a default success script is used when
    the list is empty). ``videos().list()`` returns a payload built
    from the service's current attributes, so tests can flip privacy
    or metadata between calls.
    """

    def __init__(self, event_sink: list | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.event_sink = event_sink
        self.insert_requests: list[FakeInsertRequest] = []
        self.insert_scripts: list[list] = []
        self.default_video_id = "VID_FAKE123"
        self.caption_id = "CAP_FAKE123"
        self.playlist_item_id = "PLI_FAKE123"
        # videos.list payload state.
        self.video_privacy = "private"
        self.video_title = ""
        self.video_description = ""
        self.video_published_at: str | None = None
        self.made_for_kids = False
        self.contains_synthetic_media = False
        self.custom_thumbnail_url = "https://example.com/custom-thumb.jpg"
        self.caption_items: list[dict] = [
            {"id": "CAP_FAKE123", "snippet": {"language": "ur"}}
        ]
        self.playlist_video_ids: set[str] = set()
        self.search_items: list[dict] = []
        self.channel_items: list[dict] = []
        self.thumbnail_set = False

    def _record(self, resource: str, method: str, kwargs: dict) -> None:
        self.calls.append((resource, method, kwargs))
        if self.event_sink is not None:
            self.event_sink.append(("call", f"{resource}.{method}"))

    def videos_list_payload(self) -> dict:
        snippet: dict[str, Any] = {
            "title": self.video_title,
            "description": self.video_description,
            "thumbnails": {
                "default": {
                    "url": (
                        "https://i.ytimg.com/vi/"
                        f"{self.default_video_id}/default.jpg"
                    )
                },
                "high": {"url": self.custom_thumbnail_url},
            },
        }
        if self.video_published_at is not None:
            snippet["publishedAt"] = self.video_published_at
        return {
            "items": [
                {
                    "id": self.default_video_id,
                    "snippet": snippet,
                    "status": {
                        "privacyStatus": self.video_privacy,
                        "madeForKids": self.made_for_kids,
                        "containsSyntheticMedia": self.contains_synthetic_media,
                    },
                }
            ]
        }

    def videos(self) -> _VideosResource:
        return _VideosResource(self)

    def captions(self) -> _CaptionsResource:
        return _CaptionsResource(self)

    def thumbnails(self) -> _ThumbnailsResource:
        return _ThumbnailsResource(self)

    def playlistItems(self) -> _PlaylistItemsResource:
        return _PlaylistItemsResource(self)

    def search(self) -> _SearchResource:
        return _SearchResource(self)

    def channels(self) -> _ChannelsResource:
        return _ChannelsResource(self)


# ---------------------------------------------------------------------------
# Job / file / settings helpers
# ---------------------------------------------------------------------------

ASSESSMENT = {"made_for_kids": False, "contains_synthetic_media": False}


def make_settings(**overrides) -> Settings:
    """Settings with the engine gates OPEN (dry-run off, force-off off)."""
    base: dict[str, Any] = {"dry_run": False, "uploads_force_off": False}
    base.update(overrides)
    return Settings(**base)


def write_temp_video(tmp_path, name: str = "video.mp4", content: bytes | None = None) -> Path:
    if content is None:
        content = b"FAKE VIDEO BYTES " * 64
    path = Path(tmp_path) / name
    path.write_bytes(content)
    return path


def sha256_of(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_job_dict(
    job_id: str = "job-test-001",
    source_file: str = "",
    file_sha256: str = "0" * 64,
    status: str = "approved_for_upload",
    **overrides,
) -> dict:
    """A bare job dict (not in any queue) for unit-level tests."""
    job: dict[str, Any] = {
        "job_id": job_id,
        "topic": "Test topic",
        "title": "Test video title",
        "description": "Test description",
        "source_file": str(source_file),
        "file_sha256": file_sha256,
        "status": status,
        "metadata": {"assessment": dict(ASSESSMENT), "language": "ur"},
        "attempt_count": 0,
        "bytes_uploaded": 0,
        "session_uri": None,
    }
    job.update(overrides)
    return job


def make_approved_job(queue: FakeQueue, tmp_path, job_id: str = "job-test-001", **metadata_overrides):
    """Create a real temp video file + a FakeQueue job in
    ``approved_for_upload``. Returns ``(job_dict, video_path)``.

    File content is derived from ``job_id`` so every job has a
    distinct SHA-256 (the queue rejects live duplicate fingerprints).
    """
    content = f"FAKE VIDEO BYTES for {job_id} ".encode() * 32
    video = write_temp_video(tmp_path, f"{job_id}.mp4", content)
    metadata: dict[str, Any] = {"assessment": dict(ASSESSMENT), "language": "ur"}
    metadata.update(metadata_overrides)
    queue.create_job(
        job_id=job_id,
        topic="Test topic",
        source_file=str(video),
        file_sha256=sha256_of(video),
        title="Test video title",
        description="Test description",
        metadata=metadata,
    )
    queue.transition(job_id, JobStatus.APPROVED_FOR_UPLOAD)
    return queue.get_job(job_id), video


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_queue() -> FakeQueue:
    """FakeQueue with the kill switch ON (uploads enabled)."""
    return FakeQueue(agent_id="muse", uploads_enabled=True)


@pytest.fixture
def quota_tracker() -> QuotaTracker:
    return QuotaTracker(InMemoryQuotaStore())


@pytest.fixture
def no_backoff(monkeypatch):
    """Patch the uploader's retry backoff to a no-op (tests stay fast).

    Real backoff sleeps up to ~30s per retry; retry tests only need
    the retry COUNT and outcome, not the delay.
    """
    monkeypatch.setattr(
        "youtube_uploader.uploader._sleep_backoff", lambda retry_no: None
    )
