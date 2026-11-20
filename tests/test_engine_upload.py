"""Upload mechanics: the resumable videos.insert flow against a mock
service that simulates googleapiclient's request/next_chunk surface.

The mock surface matches uploader.py's real usage: the engine builds
a real ``MediaFileUpload`` from the temp file, calls
``service.videos().insert(part, body, media_body)``, then drives the
returned request via ``next_chunk()`` and reads/writes
``resumable_uri`` / ``resumable_progress`` / ``_in_error_state``.
"""

import pytest

from youtube_uploader.errors import (
    UploadBlockedError,
    UploadOutcomeUnknownError,
)
from youtube_uploader.quota import QuotaExceeded, pacific_today
from youtube_uploader.state import JobStatus
from youtube_uploader.uploader import ResumableUploader

from conftest import (
    FakeYouTubeService,
    make_approved_job,
    make_http_error,
    make_quota_error,
    make_settings,
)


def _run(queue, tracker, service, job, settings=None):
    uploader = ResumableUploader(
        service, queue, tracker, settings or make_settings()
    )
    return uploader.upload(job)


def _insert_body(service):
    assert len(service.insert_requests) >= 1
    return service.calls[0][2]["body"]


# ---------------------------------------------------------------------------
# Request body: privacy + per-video assessment
# ---------------------------------------------------------------------------


def test_privacy_status_always_private(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    # Even if the job metadata claims otherwise, the insert body must
    # carry privacyStatus "private" — it is hardcoded, never read
    # from metadata.
    job, _ = make_approved_job(
        fake_queue,
        tmp_path,
        "job-up-priv",
        privacy_status="public",
        privacyStatus="public",
    )
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_FAKE123"
    body = _insert_body(service)
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["title"] == job["title"]


@pytest.mark.parametrize(
    "made_for_kids,contains_synthetic",
    [(True, False), (False, True)],
)
def test_assessment_values_flow_into_body(
    fake_queue,
    quota_tracker,
    tmp_path,
    no_backoff,
    made_for_kids,
    contains_synthetic,
):
    service = FakeYouTubeService()
    job_id = f"job-up-assess-{made_for_kids}-{contains_synthetic}"
    job, _ = make_approved_job(
        fake_queue,
        tmp_path,
        job_id,
        assessment={
            "made_for_kids": made_for_kids,
            "contains_synthetic_media": contains_synthetic,
        },
    )
    _run(fake_queue, quota_tracker, service, job)
    body = _insert_body(service)
    assert body["status"]["madeForKids"] is made_for_kids
    assert body["status"]["containsSyntheticMedia"] is contains_synthetic


# ---------------------------------------------------------------------------
# Session persistence + resume
# ---------------------------------------------------------------------------


def test_session_uri_and_bytes_persisted_after_chunks(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    service.insert_scripts = [[("progress", 0.5), ("done", "VID_SESS")]]
    job, video = make_approved_job(fake_queue, tmp_path, "job-up-sess")
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_SESS"
    row = fake_queue.get_job("job-up-sess")
    assert row["upload_session_uri"] == "https://upload.example/session/FAKE"
    assert row["bytes_uploaded"] == video.stat().st_size
    assert service.insert_requests[0].next_chunk_calls == 2


def test_resume_from_session_uri_in_job_dict(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    service.insert_scripts = [[("progress", 0.9), ("done", "VID_RESUMED")]]
    job, _ = make_approved_job(fake_queue, tmp_path, "job-up-resume-dict")
    job = dict(
        job,
        session_uri="https://upload.example/session/RESUME-1",
        bytes_uploaded=512,
    )
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_RESUMED"
    request = service.insert_requests[0]
    # The engine pointed the fresh request at the saved session and
    # told the library to reconcile progress with the server.
    assert request.in_error_state_at_first_chunk is True
    assert request.progress_at_first_chunk == 512
    assert request.resumable_uri == "https://upload.example/session/RESUME-1"
    assert len(service.insert_requests) == 1  # resume, not a new upload


def test_resume_from_queue_row_session(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    # Queue rows store the session under "upload_session_uri"
    # (save_session / the jobs table column) — a resumed job handed
    # over as a queue row must resume from it, not start fresh.
    service = FakeYouTubeService()
    service.insert_scripts = [[("done", "VID_RESUMED_ROW")]]
    job, _ = make_approved_job(fake_queue, tmp_path, "job-up-resume-row")
    fake_queue.save_session(
        "job-up-resume-row",
        "https://upload.example/session/RESUME-ROW",
        256,
    )
    job = fake_queue.get_job("job-up-resume-row")
    assert job["upload_session_uri"] == "https://upload.example/session/RESUME-ROW"
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_RESUMED_ROW"
    request = service.insert_requests[0]
    assert request.in_error_state_at_first_chunk is True
    assert request.progress_at_first_chunk == 256
    assert request.resumable_uri == "https://upload.example/session/RESUME-ROW"


# ---------------------------------------------------------------------------
# Session expiry, ambiguity, retries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expired_status", [404, 410])
def test_session_expiry_starts_fresh_session_and_succeeds(
    fake_queue, quota_tracker, tmp_path, no_backoff, expired_status
):
    service = FakeYouTubeService()
    service.insert_scripts = [
        [("raise", make_http_error(expired_status, "Gone", b"session expired"))],
        [("done", "VID_FRESH")],
    ]
    job, _ = make_approved_job(
        fake_queue, tmp_path, f"job-up-expire-{expired_status}"
    )
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_FRESH"
    # Two videos.insert calls: the dead session, then the fresh one.
    assert len(service.insert_requests) == 2
    assert fake_queue.get_job(job["job_id"])["status"] == (
        JobStatus.UPLOADED_PRIVATE.value
    )
    # Exactly one attempt row (the success): the restart is recorded
    # in the audit log, not as a second attempt row — upload_attempts
    # has UNIQUE(job_id, attempt_no).
    attempts = fake_queue.attempts
    assert [a["outcome"] for a in attempts] == ["success"]
    assert any(
        entry["action"] == "session_restart" for entry in fake_queue.audit_log
    )


def test_final_chunk_ambiguity_never_reinserts(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    service.insert_scripts = [[("raise", TimeoutError("connection lost"))]]
    job, _ = make_approved_job(fake_queue, tmp_path, "job-up-ambiguous")
    with pytest.raises(UploadOutcomeUnknownError):
        _run(fake_queue, quota_tracker, service, job)
    # videos.insert was issued EXACTLY once: no blind re-upload.
    assert len(service.insert_requests) == 1
    row = fake_queue.get_job("job-up-ambiguous")
    assert row["status"] == JobStatus.UPLOAD_OUTCOME_UNKNOWN.value
    assert any(
        a["outcome"] == "upload_outcome_unknown" for a in fake_queue.attempts
    )


def test_transient_5xx_retried_then_succeeds(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    service.insert_scripts = [
        [
            ("raise", make_http_error(500, "Server Error", b"boom")),
            ("raise", make_http_error(503, "Unavailable", b"boom")),
            ("done", "VID_RETRIED"),
        ]
    ]
    job, _ = make_approved_job(fake_queue, tmp_path, "job-up-retry")
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_RETRIED"
    assert len(service.insert_requests) == 1
    assert service.insert_requests[0].next_chunk_calls == 3
    assert fake_queue.get_job("job-up-retry")["status"] == (
        JobStatus.UPLOADED_PRIVATE.value
    )


def test_quota_403_not_retried(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    service.insert_scripts = [[("raise", make_quota_error())]]
    job, _ = make_approved_job(fake_queue, tmp_path, "job-up-quota403")
    with pytest.raises(QuotaExceeded):
        _run(fake_queue, quota_tracker, service, job)
    # A quota rejection is never retried: one insert, one chunk call.
    assert len(service.insert_requests) == 1
    assert service.insert_requests[0].next_chunk_calls == 1


# ---------------------------------------------------------------------------
# Success path + claimed-job path
# ---------------------------------------------------------------------------


def test_success_path_records_quota_attempt_and_state(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    job, _ = make_approved_job(fake_queue, tmp_path, "job-up-success")
    video_id = _run(fake_queue, quota_tracker, service, job)
    assert video_id == "VID_FAKE123"
    assert quota_tracker.uploads_used(pacific_today()) == 1
    assert quota_tracker.store.get_usage(pacific_today()) == {"videos.insert": 1}
    attempts = fake_queue.attempts
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "success"
    assert attempts[0]["video_id"] == "VID_FAKE123"
    row = fake_queue.get_job("job-up-success")
    assert row["status"] == JobStatus.UPLOADED_PRIVATE.value
    assert row["video_id"] == "VID_FAKE123"


def test_claimed_job_uploads_successfully(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    # The live worker path claims first (claim_job moves the job to
    # "uploading"), then uploads the claimed job dict. The engine
    # must accept its own claim.
    service = FakeYouTubeService()
    make_approved_job(fake_queue, tmp_path, "job-up-claimed")
    claimed = fake_queue.claim_job()
    assert claimed is not None
    assert claimed["status"] == JobStatus.UPLOADING.value
    assert claimed["claimed_by"] == "muse"
    video_id = _run(fake_queue, quota_tracker, service, claimed)
    assert video_id == "VID_FAKE123"
    assert fake_queue.get_job("job-up-claimed")["status"] == (
        JobStatus.UPLOADED_PRIVATE.value
    )


def test_job_uploading_under_another_agent_is_refused(
    fake_queue, quota_tracker, tmp_path, no_backoff
):
    service = FakeYouTubeService()
    make_approved_job(fake_queue, tmp_path, "job-up-other-agent")
    claimed = fake_queue.claim_job()  # claimed by "muse"
    assert claimed is not None
    # A different agent's worker must not upload someone else's claim.
    other_settings = make_settings(agent_id="meem")
    with pytest.raises(UploadBlockedError, match="not 'approved_for_upload'"):
        _run(fake_queue, quota_tracker, service, claimed, settings=other_settings)
    assert service.calls == []
