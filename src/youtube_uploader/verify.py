"""Per-step verification (Plan v2, Phase B).

A video ID alone is NOT proof of success. After upload and asset
steps, every step is verified against the live YouTube state before
the job may move to ``assets_verified``:

* video metadata (title, description, privacy, assessments)
* caption track present
* custom thumbnail present (heuristic — see :func:`verify_thumbnail`)
* playlist membership

:func:`verify_video` raises :class:`VerificationError` on any
mismatch. The boolean helpers return False instead of raising, so
the caller (the queue worker / CLI pipeline) can decide how to
report the failure; the CLI raises VerificationError on False.

All YouTube calls go through the injected ``service`` object.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from youtube_uploader.errors import VerificationError

log = logging.getLogger(__name__)

#: Max pages to scan when checking playlist membership (50 items/page
#: => up to 2,500 items — far beyond any Sacred Knowledge playlist).
MAX_PLAYLIST_PAGES = 50

#: Auto-generated thumbnails live at i.ytimg.com/vi/<id>/<size>.jpg.
#: A custom thumbnail is served from a different path.
_AUTO_THUMBNAIL_RE = re.compile(
    r"^https://i\.ytimg\.com/vi/[^/]+/"
    r"(default|mqdefault|hqdefault|sddefault|maxresdefault)\.jpg$"
)


def _expected(expected: dict[str, Any], *keys: str) -> Any:
    """Read an expected value accepting snake_case or camelCase keys."""
    for key in keys:
        if key in expected:
            return expected[key]
    return None


def verify_video(service: Any, video_id: str, expected: dict[str, Any]) -> dict[str, Any]:
    """Verify a video's metadata matches what was uploaded.

    Checks ``videos.list(part="snippet,status")`` against ``expected``
    (keys accepted in snake_case or camelCase): title, description,
    privacyStatus, madeForKids, containsSyntheticMedia. Only keys
    present in ``expected`` are compared — EXCEPT privacyStatus,
    which is always enforced: it must equal
    ``expected["privacy_status"]`` when given (``"private"`` before
    publish, ``"public"`` after), and defaults to ``"private"`` when
    not given. The ``"private"`` default is deliberate: a caller
    that did not explicitly expect a public video must never
    silently pass a video that went public early.

    Returns the live video resource on success; raises
    :class:`VerificationError` on any mismatch or if the video is
    not found.
    """
    response = (
        service.videos()
        .list(part="snippet,status", id=video_id)
        .execute()
    )
    items = response.get("items", [])
    if not items:
        raise VerificationError(
            f"Video {video_id}: not found via videos.list — cannot verify"
        )
    video = items[0]
    snippet = video.get("snippet", {})
    status = video.get("status", {})

    mismatches: list[str] = []

    expected_privacy = _expected(expected, "privacy_status", "privacyStatus")
    if expected_privacy is None:
        # Safe default: private unless the caller explicitly expects
        # otherwise (the post-publish check passes "public").
        expected_privacy = "private"
    actual_privacy = status.get("privacyStatus")
    if actual_privacy != expected_privacy:
        mismatches.append(
            f"privacyStatus is {actual_privacy!r}, "
            f"expected {expected_privacy!r}"
        )

    title = _expected(expected, "title")
    if title is not None and snippet.get("title") != title:
        mismatches.append(
            f"title is {snippet.get('title')!r}, expected {title!r}"
        )

    description = _expected(expected, "description")
    if description is not None and snippet.get("description") != description:
        mismatches.append("description does not match expected text")

    made_for_kids = _expected(expected, "made_for_kids", "madeForKids")
    if made_for_kids is not None and status.get("madeForKids") != made_for_kids:
        mismatches.append(
            f"madeForKids is {status.get('madeForKids')!r}, "
            f"expected {made_for_kids!r}"
        )

    synthetic = _expected(
        expected, "contains_synthetic_media", "containsSyntheticMedia"
    )
    if synthetic is not None and status.get("containsSyntheticMedia") != synthetic:
        mismatches.append(
            f"containsSyntheticMedia is "
            f"{status.get('containsSyntheticMedia')!r}, expected {synthetic!r}"
        )

    if mismatches:
        raise VerificationError(
            f"Video {video_id}: verification failed: "
            + "; ".join(mismatches)
        )
    log.info("Video %s: metadata verified", video_id)
    return video


def verify_caption(service: Any, video_id: str) -> bool:
    """True if the video has at least one caption track."""
    response = (
        service.captions()
        .list(part="snippet", videoId=video_id)
        .execute()
    )
    found = bool(response.get("items"))
    log.info("Video %s: caption present=%s", video_id, found)
    return found


def verify_thumbnail(service: Any, video_id: str) -> bool:
    """True if the video appears to have a CUSTOM thumbnail.

    HONEST LIMITATION: YouTube Data API v3 exposes NO direct "has
    custom thumbnail" flag. This function applies a heuristic: the
    video's ``snippet.thumbnails`` object must be present, and at
    least one thumbnail URL must NOT match the auto-generated pattern
    (``https://i.ytimg.com/vi/<id>/<size>.jpg``). Auto-generated
    thumbnails always follow that pattern; a custom upload is served
    from a different path. The heuristic can produce a false negative
    if YouTube ever serves a custom thumbnail from an auto-looking
    URL, and it cannot prove the thumbnail bytes are the ones that
    were uploaded — only a visual check (browser, Phase D) proves
    that. Callers should treat False here as "needs a human look",
    not as proof the thumbnail is missing.
    """
    response = (
        service.videos()
        .list(part="snippet", id=video_id)
        .execute()
    )
    items = response.get("items", [])
    if not items:
        log.warning("Video %s: not found while verifying thumbnail", video_id)
        return False
    thumbnails = items[0].get("snippet", {}).get("thumbnails", {})
    if not thumbnails:
        return False
    for thumb in thumbnails.values():
        url = thumb.get("url", "") if isinstance(thumb, dict) else ""
        if url and not _AUTO_THUMBNAIL_RE.match(url):
            log.info("Video %s: custom thumbnail detected", video_id)
            return True
    log.info(
        "Video %s: only auto-generated thumbnail URLs found", video_id
    )
    return False


def verify_playlist_membership(
    service: Any, video_id: str, playlist_id: str
) -> bool:
    """True if the playlist contains the video.

    Pages through ``playlistItems.list`` (50 items/page) up to
    :data:`MAX_PLAYLIST_PAGES` pages.
    """
    page_token: str | None = None
    for page in range(MAX_PLAYLIST_PAGES):
        kwargs: dict[str, Any] = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.playlistItems().list(**kwargs).execute()
        for item in response.get("items", []):
            resource = item.get("snippet", {}).get("resourceId", {})
            if resource.get("videoId") == video_id:
                log.info(
                    "Video %s: found in playlist %s (page %d)",
                    video_id,
                    playlist_id,
                    page + 1,
                )
                return True
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    log.info(
        "Video %s: NOT found in playlist %s after scanning",
        video_id,
        playlist_id,
    )
    return False
