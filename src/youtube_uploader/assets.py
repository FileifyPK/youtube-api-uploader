"""Post-upload assets (Plan v2, Phase B): captions, thumbnail, playlist,
and publishing.

Each function reserves quota BEFORE the API call and records usage
afterwards. A quota rejection (403 ``quotaExceeded``) raises
:class:`QuotaExceeded` and is NEVER retried — the caller pauses the
pipeline, it does not hammer the API.

Publishing (:func:`publish_video`) sets ``privacyStatus: "public"``
via ``videos.update``. Per the owner's standing rule (owner order
20 Nov 2026 ~22:04), EVERY video publishes public automatically once
assets verify — no per-video publishing approval. Uploads still
start private as a safety measure.

All YouTube calls go through the injected ``service`` object.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from youtube_uploader.quota import QuotaExceeded, QuotaTracker

log = logging.getLogger(__name__)

#: YouTube's thumbnail size limit.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024

_ALLOWED_THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png"}
_THUMBNAIL_MIMETYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


def _is_quota_error(exc: HttpError) -> bool:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    if status is None:
        return False
    try:
        status = int(status)
    except (TypeError, ValueError):
        return False
    if status != 403:
        return False
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    return b"quotaExceeded" in content or b"quota" in content.lower()


def _raise_quota_or_pass(exc: HttpError, endpoint: str) -> None:
    if _is_quota_error(exc):
        raise QuotaExceeded(
            f"{endpoint} rejected by YouTube quota (quotaExceeded) — "
            "not retried."
        ) from exc
    raise exc


def upload_caption(
    service: Any,
    quota: QuotaTracker,
    video_id: str,
    srt_path: str | Path,
    language: str,
    name: str | None = None,
) -> str:
    """Upload an SRT caption track; return the caption ID.

    Costs 400 quota units. The .srt file must exist and be non-empty.
    """
    path = Path(srt_path)
    if path.suffix.lower() != ".srt":
        raise ValueError(f"Caption file must be .srt, got: {path.name}")
    if not path.is_file():
        raise FileNotFoundError(f"Caption file not found: {path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Caption file is empty: {path}")
    if not language:
        raise ValueError("Caption language is required (e.g. 'ur')")

    quota.reserve("captions.insert")
    media = MediaFileUpload(str(path), mimetype="text/plain")
    body: dict[str, Any] = {
        "snippet": {
            "videoId": video_id,
            "language": language,
            "name": name or f"Captions ({language})",
            "isDraft": False,
        }
    }
    try:
        response = (
            service.captions()
            .insert(part="snippet", body=body, media_body=media)
            .execute()
        )
    except HttpError as exc:
        _raise_quota_or_pass(exc, "captions.insert")
    quota.record("captions.insert")
    caption_id = response.get("id", "")
    log.info("Video %s: caption uploaded (%s)", video_id, caption_id)
    return caption_id


def set_thumbnail(
    service: Any,
    quota: QuotaTracker,
    video_id: str,
    image_path: str | Path,
) -> bool:
    """Set a custom thumbnail; return True on success.

    Costs 50 quota units. The image must exist, be a .jpg/.jpeg/.png,
    and be at most 2 MB (YouTube's limit).
    """
    path = Path(image_path)
    ext = path.suffix.lower()
    if ext not in _ALLOWED_THUMBNAIL_EXTENSIONS:
        raise ValueError(
            f"Thumbnail must be .jpg/.jpeg/.png, got: {path.name}"
        )
    if not path.is_file():
        raise FileNotFoundError(f"Thumbnail not found: {path}")
    size = path.stat().st_size
    if size > MAX_THUMBNAIL_BYTES:
        raise ValueError(
            f"Thumbnail is {size} bytes — YouTube's limit is "
            f"{MAX_THUMBNAIL_BYTES} bytes (2 MB)"
        )
    if size == 0:
        raise ValueError(f"Thumbnail file is empty: {path}")

    quota.reserve("thumbnails.set")
    media = MediaFileUpload(str(path), mimetype=_THUMBNAIL_MIMETYPES[ext])
    try:
        service.thumbnails().set(
            videoId=video_id, media_body=media
        ).execute()
    except HttpError as exc:
        _raise_quota_or_pass(exc, "thumbnails.set")
    quota.record("thumbnails.set")
    log.info("Video %s: thumbnail set", video_id)
    return True


def add_to_playlist(
    service: Any,
    quota: QuotaTracker,
    video_id: str,
    playlist_id: str,
) -> str:
    """Add a video to a playlist; return the playlist-item ID.

    Costs 50 quota units.
    """
    if not playlist_id:
        raise ValueError("playlist_id is required")
    quota.reserve("playlistItems.insert")
    body: dict[str, Any] = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }
    }
    try:
        response = (
            service.playlistItems()
            .insert(part="snippet", body=body)
            .execute()
        )
    except HttpError as exc:
        _raise_quota_or_pass(exc, "playlistItems.insert")
    quota.record("playlistItems.insert")
    item_id = response.get("id", "")
    log.info(
        "Video %s: added to playlist %s (item %s)",
        video_id,
        playlist_id,
        item_id,
    )
    return item_id


def publish_video(service: Any, quota: QuotaTracker, video_id: str) -> bool:
    """Publish a video PUBLIC via ``videos.update``; return True.

    Costs 50 quota units. Sets ONLY ``status.privacyStatus`` to
    ``"public"`` (part="status") — title, description, tags, and the
    per-video assessment fields are untouched.

    Called automatically by the pipeline once assets verify, per the
    owner's standing rule: every video is public, no per-video
    publishing approval.
    """
    quota.reserve("videos.update")
    body: dict[str, Any] = {
        "id": video_id,
        "status": {"privacyStatus": "public"},
    }
    try:
        service.videos().update(part="status", body=body).execute()
    except HttpError as exc:
        _raise_quota_or_pass(exc, "videos.update")
    quota.record("videos.update")
    log.info("Video %s: published public", video_id)
    return True
