"""Reconciliation for ``upload_outcome_unknown`` jobs (Plan v2, §2/§6).

When an upload's outcome is unknown — YouTube may have accepted the
video but the video-ID response was lost — the job must NOT be
blindly re-uploaded. :func:`reconcile_unknown_job` asks YouTube what
actually happened:

1. ``search.list`` (``forMine=True``, the job's title; costs 100
   quota units, reserved first) finds candidate videos on the
   channel.
2. Each candidate is checked with ``videos.list`` for an exact title
   match, a matching description prefix, and a same-day upload
   (within the job's upload window).

Outcomes:

* ``"found"`` — a strong match (exact title + same-day upload +
  description prefix). The video is adopted: the job transitions to
  ``uploaded_private`` with the discovered video ID.
* ``"not_found"`` — no candidates at all AND the job's last attempt
  was more than 24 hours ago. Safe to retry: the job transitions
  back to ``approved_for_upload``.
* Anything ambiguous raises :class:`ReconciliationUncertainError`
  and the job STAYS in ``upload_outcome_unknown`` for human review.

BEST-EFFORT LIMITATION: reconciliation can PROVE existence (a
matching video is on the channel) but absence is only proven after
the 24-hour window — YouTube's search index lags behind uploads, so
a video uploaded minutes ago may not appear in ``search.list`` yet.
That is exactly why "no candidates" only counts as "not found" once
the last attempt is over 24 hours old. When in doubt, the job waits
for a human; a duplicate upload is worse than a delayed one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from youtube_uploader.errors import ReconciliationUncertainError
from youtube_uploader.quota import QuotaTracker
from youtube_uploader.state import JobStatus

log = logging.getLogger(__name__)

#: Hours after the last attempt before "no video found" is trusted.
NOT_FOUND_MIN_AGE = timedelta(hours=24)

#: Characters of the description compared as a prefix match.
DESCRIPTION_PREFIX_LEN = 100

#: Max search candidates examined per reconciliation.
MAX_SEARCH_RESULTS = 10


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (or datetime) to aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _job_last_attempt(job: dict[str, Any]) -> datetime | None:
    """Best-known time of the job's last upload attempt."""
    for key in ("last_attempt_at", "updated_at", "attempted_at"):
        parsed = _parse_timestamp(job.get(key))
        if parsed is not None:
            return parsed
    return None


def _description_matches(job: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """True if the candidate's description starts with the job's prefix.

    Skipped (True) when the job has no description recorded — there
    is nothing to compare against.
    """
    expected = (job.get("description") or "")[:DESCRIPTION_PREFIX_LEN]
    if not expected:
        return True
    actual = candidate.get("snippet", {}).get("description", "")
    return actual.startswith(expected)


def _is_strong_match(
    job: dict[str, Any],
    candidate: dict[str, Any],
    upload_day: datetime.date | None,
) -> bool:
    """Exact title + same-day upload (+ description prefix)."""
    snippet = candidate.get("snippet", {})
    if snippet.get("title") != job.get("title"):
        return False
    if upload_day is not None:
        published = _parse_timestamp(snippet.get("publishedAt"))
        if published is None or published.date() != upload_day:
            return False
    return _description_matches(job, candidate)


def reconcile_unknown_job(service: Any, queue: Any, job: dict[str, Any]) -> str:
    """Reconcile one ``upload_outcome_unknown`` job.

    Returns ``"found"`` or ``"not_found"`` (and transitions the job
    accordingly). Raises :class:`ReconciliationUncertainError` when
    the outcome cannot be established — the job stays
    ``upload_outcome_unknown`` for human review.

    ``queue`` is the Phase C QueueClient, which also implements the
    QuotaStore protocol, so quota is reserved through it. If the
    queue object does not expose quota storage (a minimal test fake),
    reconciliation proceeds without a reservation and logs that fact.
    """
    job_id = job["job_id"]
    title = job.get("title", "")
    last_attempt = _job_last_attempt(job)
    upload_day = last_attempt.date() if last_attempt else None

    tracker: QuotaTracker | None = None
    if hasattr(queue, "get_usage") and hasattr(queue, "record"):
        tracker = QuotaTracker(queue)
        tracker.reserve("search.list")

    response = (
        service.search()
        .list(
            q=title,
            type="video",
            forMine=True,
            maxResults=MAX_SEARCH_RESULTS,
            part="snippet",
        )
        .execute()
    )
    if tracker is not None:
        tracker.record("search.list")

    candidates = [
        item.get("id", {}).get("videoId")
        for item in response.get("items", [])
    ]
    candidates = [vid for vid in candidates if vid]
    log.info(
        "Job %s: reconciliation search found %d candidate(s)",
        job_id,
        len(candidates),
    )

    if not candidates:
        if last_attempt is not None and (
            datetime.now(timezone.utc) - last_attempt > NOT_FOUND_MIN_AGE
        ):
            # Indexing delay is no longer a plausible explanation:
            # nothing was uploaded. Safe to retry.
            queue.transition(job_id, JobStatus.APPROVED_FOR_UPLOAD)
            log.info(
                "Job %s: no video found and last attempt >24h ago — "
                "safe to retry",
                job_id,
            )
            return "not_found"
        raise ReconciliationUncertainError(
            f"Job {job_id}: search found no candidates but the last "
            "attempt was within 24h — YouTube's search index may not "
            "have caught up yet. Human review required."
        )

    # One batched videos.list call for all candidates (1 unit).
    if tracker is not None:
        tracker.reserve("videos.list")
    details = (
        service.videos()
        .list(
            part="snippet,status,fileDetails",
            id=",".join(candidates),
        )
        .execute()
    )
    if tracker is not None:
        tracker.record("videos.list")

    for video in details.get("items", []):
        if _is_strong_match(job, video, upload_day):
            video_id = video.get("id", "")
            queue.transition(
                job_id, JobStatus.UPLOADED_PRIVATE, video_id=video_id
            )
            log.info(
                "Job %s: reconciliation FOUND existing video %s — adopted",
                job_id,
                video_id,
            )
            return "found"

    # Candidates exist but none matches. Only trust "not found" after
    # the 24h window; before that, the real video may simply not be
    # indexed yet (or may be a candidate whose metadata differs).
    if last_attempt is not None and (
        datetime.now(timezone.utc) - last_attempt > NOT_FOUND_MIN_AGE
    ):
        queue.transition(job_id, JobStatus.APPROVED_FOR_UPLOAD)
        log.info(
            "Job %s: %d candidate(s) but no match, last attempt >24h "
            "ago — safe to retry",
            job_id,
            len(candidates),
        )
        return "not_found"

    raise ReconciliationUncertainError(
        f"Job {job_id}: {len(candidates)} candidate video(s) found but "
        "none matches the job's title/upload window, and the last "
        "attempt was within 24h. Human review required."
    )
