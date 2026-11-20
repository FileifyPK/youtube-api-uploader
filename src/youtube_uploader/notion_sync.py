"""Notion tracker sync (Plan v2, Phase D).

After a successful upload the trackers must flip in the SAME run:

* the matching Production Queue row flips to the uploaded state, and
* a row is added to the 🎬 Uploaded Videos page ✅ Uploaded table with
  the live link.

The 🎬 Uploaded Videos page is the authority on "never upload twice":
:func:`flip_after_upload` checks ``row_exists(video_url)`` first and
refuses to record the same video twice.

This module never talks to Notion itself. The real client is the
existing Notion API helper (``notion_api.py``, which lives outside this
repository and is NOT a dependency of this package) and is injected at
runtime as any object satisfying the :class:`NotionClient` protocol.
All payload builders are pure functions, unit-testable without a
client. No Notion SDK is imported (none is installed, none is a
dependency).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

# Status value written to the Production Queue row after a successful
# upload. Must match the status vocabulary used on that database.
UPLOADED_STATUS = "uploaded"


class UploadJob(Protocol):
    """Minimal job shape the sync needs (duck-typed).

    Queue jobs are plain dicts (see :func:`adapt_job_dict`, which maps
    a queue job dict onto this shape). Only these attributes are read;
    anything else on the job is ignored.
    """

    topic_title: str  # Topic title as it appears on the Production Queue row.
    title: str  # Video title as it should appear on the 🎬 Uploaded row.
    language: str  # Video language ("English" or "Urdu").
    duration: str  # Human-readable duration, e.g. "18:11".
    playlist: str  # Playlist the video was added to (name or ID).
    agent_id: str  # Agent that performed the upload ("muse", "meem", ...).


class NotionClient(Protocol):
    """Protocol for the injected Notion client.

    The real implementation is the existing Notion API helper
    (``notion_api.py``), which is not part of this repository. It is
    injected at runtime — this module performs no live Notion calls
    and imports no Notion SDK.
    """

    def find_production_queue_row(self, topic_title: str) -> str | None:
        """Return the Production Queue row id for ``topic_title``.

        Returns None when no row matches. Matching is by the topic's
        full title, never by a bare topic number (numbers are reused
        across series).
        """
        ...

    def update_row_status(
        self, row_id: str, status: str, notes: str = ""
    ) -> None:
        """Set a Production Queue row's status (and notes)."""
        ...

    def add_uploaded_row(
        self,
        title: str,
        video_url: str,
        language: str,
        duration: str,
        playlist: str,
    ) -> str:
        """Add a row to the ✅ Uploaded table; return the new row id."""
        ...

    def row_exists(self, video_url: str) -> bool:
        """True if a ✅ Uploaded row already records ``video_url``."""
        ...


def build_video_url(video_id: str) -> str:
    """Canonical watch URL for a YouTube video id."""
    return f"https://www.youtube.com/watch?v={video_id}"


class JobView:
    """Attribute view over a queue job dict, satisfying UploadJob.

    Queue jobs are dicts with keys ``topic``, ``title`` and a
    ``metadata`` mapping; the sync reads attributes. This adapter
    bridges the two shapes without copying data.
    """

    def __init__(self, job: dict) -> None:
        self._job = job

    @property
    def topic_title(self) -> str:
        return str(self._job.get("topic", "") or "")

    @property
    def title(self) -> str:
        return str(self._job.get("title", "") or "")

    @property
    def language(self) -> str:
        meta = self._job.get("metadata") or {}
        return str(meta.get("language", "") or "")

    @property
    def duration(self) -> str:
        meta = self._job.get("metadata") or {}
        return str(meta.get("duration", "") or "")

    @property
    def playlist(self) -> str:
        meta = self._job.get("metadata") or {}
        return str(
            meta.get("playlist_name", meta.get("playlist_id", "")) or ""
        )

    @property
    def agent_id(self) -> str:
        # claimed_by is cleared (None) when the job leaves "uploading",
        # so the fallback must trigger on a None VALUE, not just a
        # missing key — otherwise completed jobs report agent "".
        return str(
            self._job.get("claimed_by")
            or self._job.get("agent_id", "")
            or ""
        )


def adapt_job_dict(job: UploadJob | dict) -> UploadJob:
    """Accept a queue job dict or an UploadJob; return an UploadJob."""
    if isinstance(job, dict):
        return JobView(job)
    return job


def build_upload_notes(
    job: UploadJob | dict,
    video_id: str,
    *,
    now: datetime | None = None,
) -> str:
    """Pure helper: notes text for the Production Queue tracker row.

    Records the video ID, the live URL, the upload time (UTC) and the
    uploading agent. ``now`` exists only so tests can pin the time.
    Accepts a queue job dict (adapted via :func:`adapt_job_dict`) or
    an :class:`UploadJob`.
    """
    job = adapt_job_dict(job)
    stamp = (now or datetime.now(timezone.utc)).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    agent = getattr(job, "agent_id", "unknown")
    return (
        f"Uploaded via YouTube API and published Public. "
        f"Video ID: {video_id}. "
        f"URL: {build_video_url(video_id)}. "
        f"Uploaded: {stamp}. Agent: {agent}."
    )


def build_uploaded_row_payload(
    job: UploadJob | dict, video_url: str
) -> dict:
    """Pure helper: payload for ``NotionClient.add_uploaded_row``."""
    job = adapt_job_dict(job)
    return {
        "title": job.title,
        "video_url": video_url,
        "language": job.language,
        "duration": job.duration,
        "playlist": job.playlist,
    }


class NotionSync:
    """Flips Notion trackers after a successful upload.

    Constructed with an injected client satisfying
    :class:`NotionClient`. Every public method is safe to call twice
    for the same upload: the idempotency check in
    :meth:`flip_after_upload` prevents duplicate ✅ Uploaded rows.
    """

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    @staticmethod
    def build_upload_notes(
        job: UploadJob | dict,
        video_id: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """Notes text for the tracker row (pure, no client needed)."""
        return build_upload_notes(job, video_id, now=now)

    def flip_after_upload(
        self, job: UploadJob | dict, video_id: str, video_url: str
    ) -> dict:
        """Flip the trackers for a completed upload.

        Accepts a queue job dict (adapted via :func:`adapt_job_dict`)
        or an :class:`UploadJob`.

        Idempotent: if a ✅ Uploaded row already records ``video_url``,
        nothing is written and ``{"flipped": False, "reason":
        "already recorded"}`` is returned.

        On success returns ``{"flipped": True,
        "production_queue_row_id": ..., "uploaded_row_id": ...}`` plus
        ``video_id`` / ``video_url``. If the Production Queue row
        cannot be found, the ✅ Uploaded row is still recorded (the 🎬
        page is the authority against duplicate uploads) and the
        result carries ``"reason": "production_queue_row_not_found"``
        with ``"flipped": False`` so the missing row can be fixed by
        hand instead of silently dropped.
        """
        job = adapt_job_dict(job)
        base = {"video_id": video_id, "video_url": video_url}
        if self._client.row_exists(video_url):
            return {**base, "flipped": False, "reason": "already recorded"}

        result: dict = {**base, "flipped": False}
        row_id = self._client.find_production_queue_row(job.topic_title)
        if row_id is not None:
            notes = build_upload_notes(job, video_id)
            self._client.update_row_status(row_id, UPLOADED_STATUS, notes)
            result["production_queue_row_id"] = row_id
            result["flipped"] = True
        else:
            result["reason"] = "production_queue_row_not_found"

        payload = build_uploaded_row_payload(job, video_url)
        result["uploaded_row_id"] = self._client.add_uploaded_row(
            payload["title"],
            payload["video_url"],
            payload["language"],
            payload["duration"],
            payload["playlist"],
        )
        return result
