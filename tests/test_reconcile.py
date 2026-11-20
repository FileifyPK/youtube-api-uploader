"""Reconciliation tests: resolve ``upload_outcome_unknown`` jobs
against a mock YouTube service + FakeQueue.

Outcomes: "found" (adopt the video), "not_found" (safe to retry),
or ReconciliationUncertainError (job stays unknown for human review).
Reconciliation NEVER calls videos.insert.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from youtube_uploader.errors import ReconciliationUncertainError
from youtube_uploader.queue import FakeQueue
from youtube_uploader.reconcile import reconcile_unknown_job
from youtube_uploader.state import JobStatus

TITLE = "Reconcile Test Title"
DESCRIPTION = "Reconcile test description."


def _mock_service(search_items, video_details):
    service = MagicMock()
    service.search.return_value.list.return_value.execute.return_value = {
        "items": search_items
    }
    service.videos.return_value.list.return_value.execute.return_value = {
        "items": video_details
    }
    return service


def _unknown_job(queue, job_id="job-rec-001"):
    queue.create_job(
        job_id=job_id,
        topic="Reconcile topic",
        source_file=f"/tmp/{job_id}.mp4",
        file_sha256="sha-" + job_id,
        title=TITLE,
        description=DESCRIPTION,
        metadata={},
    )
    queue.transition(job_id, JobStatus.APPROVED_FOR_UPLOAD)
    queue.transition(job_id, JobStatus.UPLOADING)
    queue.transition(job_id, JobStatus.UPLOAD_OUTCOME_UNKNOWN)
    return queue.get_job(job_id)


def _candidate(video_id, title=TITLE, published_at=None, description=None):
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "publishedAt": published_at
            or datetime.now(timezone.utc).isoformat(),
            "description": description
            if description is not None
            else DESCRIPTION + " (full text continues)",
        },
    }


def test_found_adopts_video_and_transitions_to_uploaded_private():
    queue = FakeQueue()
    job = _unknown_job(queue)
    # Pin the upload day: last attempt at noon UTC today, candidate
    # published the same day with the exact title + description prefix.
    noon = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    job = dict(job, last_attempt_at=noon.isoformat())
    service = _mock_service(
        search_items=[{"id": {"videoId": "VID_FOUND"}}],
        video_details=[_candidate("VID_FOUND", published_at=noon.isoformat())],
    )
    assert reconcile_unknown_job(service, queue, job) == "found"
    row = queue.get_job(job["job_id"])
    assert row["status"] == JobStatus.UPLOADED_PRIVATE.value
    assert row["video_id"] == "VID_FOUND"
    service.videos.return_value.insert.assert_not_called()


def test_not_found_returns_job_to_approved_for_upload():
    queue = FakeQueue()
    job = _unknown_job(queue)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    job = dict(job, last_attempt_at=old.isoformat())
    service = _mock_service(search_items=[], video_details=[])
    assert reconcile_unknown_job(service, queue, job) == "not_found"
    row = queue.get_job(job["job_id"])
    assert row["status"] == JobStatus.APPROVED_FOR_UPLOAD.value
    assert row["video_id"] is None
    service.videos.return_value.insert.assert_not_called()


def test_uncertain_same_title_candidate_uploaded_long_ago():
    queue = FakeQueue()
    job = _unknown_job(queue)
    # Last attempt is recent, but the only same-title candidate was
    # published 30 days ago: not the same-day upload, so no match —
    # and the real video may simply not be indexed yet.
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    service = _mock_service(
        search_items=[{"id": {"videoId": "VID_OLD"}}],
        video_details=[_candidate("VID_OLD", published_at=long_ago)],
    )
    with pytest.raises(ReconciliationUncertainError):
        reconcile_unknown_job(service, queue, job)
    assert queue.get_job(job["job_id"])["status"] == (
        JobStatus.UPLOAD_OUTCOME_UNKNOWN.value
    )
    service.videos.return_value.insert.assert_not_called()


def test_uncertain_no_candidates_but_last_attempt_recent():
    queue = FakeQueue()
    job = _unknown_job(queue)
    # updated_at is "now" (just transitioned): last attempt <24h ago,
    # so an empty search proves nothing (indexing delay).
    service = _mock_service(search_items=[], video_details=[])
    with pytest.raises(ReconciliationUncertainError, match="within 24h"):
        reconcile_unknown_job(service, queue, job)
    assert queue.get_job(job["job_id"])["status"] == (
        JobStatus.UPLOAD_OUTCOME_UNKNOWN.value
    )
    service.videos.return_value.insert.assert_not_called()


def test_uncertain_title_mismatch_with_recent_attempt():
    queue = FakeQueue()
    job = _unknown_job(queue)
    service = _mock_service(
        search_items=[{"id": {"videoId": "VID_OTHER"}}],
        video_details=[_candidate("VID_OTHER", title="A different title")],
    )
    with pytest.raises(ReconciliationUncertainError):
        reconcile_unknown_job(service, queue, job)
    assert queue.get_job(job["job_id"])["status"] == (
        JobStatus.UPLOAD_OUTCOME_UNKNOWN.value
    )


def test_not_found_when_candidates_do_not_match_and_attempt_old():
    queue = FakeQueue()
    job = _unknown_job(queue)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    job = dict(job, last_attempt_at=old.isoformat())
    service = _mock_service(
        search_items=[{"id": {"videoId": "VID_OTHER"}}],
        video_details=[_candidate("VID_OTHER", title="A different title")],
    )
    assert reconcile_unknown_job(service, queue, job) == "not_found"
    assert queue.get_job(job["job_id"])["status"] == (
        JobStatus.APPROVED_FOR_UPLOAD.value
    )
