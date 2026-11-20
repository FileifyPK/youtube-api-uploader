"""ResumableUploader safety gates (uploader.upload, in order):

1. dry_run -> DryRunBlockedError
2. uploads_force_off -> UploadBlockedError
3. kill switch OFF -> KillSwitchOffError
4. job not approved_for_upload -> UploadBlockedError
5. quota reservation -> QuotaExceeded
then: source validation, assessment validation.

Every gate test uses DryRunService + FakeQueue and asserts NO calls
were recorded on the service when a gate refuses.
"""

import pytest

from youtube_uploader.dryrun import DryRunService
from youtube_uploader.errors import (
    DryRunBlockedError,
    KillSwitchOffError,
    UploadBlockedError,
)
from youtube_uploader.queue import FakeQueue
from youtube_uploader.quota import QuotaExceeded, pacific_today
from youtube_uploader.state import JobStatus
from youtube_uploader.uploader import ResumableUploader

from conftest import make_approved_job, make_settings


def _uploader(service, queue, tracker, settings):
    return ResumableUploader(service, queue, tracker, settings)


def test_dry_run_blocks_before_anything(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, _ = make_approved_job(fake_queue, tmp_path, "job-gate-dryrun")
    uploader = _uploader(
        service, fake_queue, quota_tracker, make_settings(dry_run=True)
    )
    with pytest.raises(DryRunBlockedError):
        uploader.upload(job)
    assert service.calls == []


def test_force_off_blocks(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, _ = make_approved_job(fake_queue, tmp_path, "job-gate-forceoff")
    uploader = _uploader(
        service,
        fake_queue,
        quota_tracker,
        make_settings(uploads_force_off=True),
    )
    with pytest.raises(UploadBlockedError, match="force-disabled"):
        uploader.upload(job)
    assert service.calls == []


def test_kill_switch_off_blocks(quota_tracker, tmp_path):
    # FakeQueue defaults to uploads OFF (fail closed).
    queue = FakeQueue(agent_id="muse")
    assert queue.uploads_enabled() is False
    service = DryRunService()
    job, _ = make_approved_job(queue, tmp_path, "job-gate-killswitch")
    uploader = _uploader(service, queue, quota_tracker, make_settings())
    with pytest.raises(KillSwitchOffError):
        uploader.upload(job)
    assert service.calls == []


def test_job_not_approved_blocks(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, _ = make_approved_job(fake_queue, tmp_path, "job-gate-queued")
    # Move the job dict back to 'queued' (the queue row stays approved;
    # the gate reads the job dict it is handed).
    job = dict(job, status=JobStatus.QUEUED.value)
    uploader = _uploader(service, fake_queue, quota_tracker, make_settings())
    with pytest.raises(UploadBlockedError, match="not 'approved_for_upload'"):
        uploader.upload(job)
    assert service.calls == []


def test_quota_exhausted_blocks(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, _ = make_approved_job(fake_queue, tmp_path, "job-gate-quota")
    # Fill the videos.insert bucket completely (100/100).
    quota_tracker.store.record(pacific_today(), "videos.insert", 100)
    uploader = _uploader(service, fake_queue, quota_tracker, make_settings())
    with pytest.raises(QuotaExceeded):
        uploader.upload(job)
    assert service.calls == []


def test_missing_assessment_blocks(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, _ = make_approved_job(
        fake_queue, tmp_path, "job-gate-noassess", assessment=None
    )
    # make_approved_job merges overrides into metadata; drop the key.
    job = dict(job)
    job["metadata"] = {"language": "ur"}
    uploader = _uploader(service, fake_queue, quota_tracker, make_settings())
    with pytest.raises(ValueError, match="missing metadata.assessment"):
        uploader.upload(job)
    assert service.calls == []


@pytest.mark.parametrize("missing_key", ["made_for_kids", "contains_synthetic_media"])
def test_assessment_missing_key_blocks(
    fake_queue, quota_tracker, tmp_path, missing_key
):
    service = DryRunService()
    assessment = {"made_for_kids": False, "contains_synthetic_media": False}
    del assessment[missing_key]
    job, _ = make_approved_job(
        fake_queue, tmp_path, f"job-gate-{missing_key}", assessment=assessment
    )
    uploader = _uploader(service, fake_queue, quota_tracker, make_settings())
    with pytest.raises(ValueError, match="missing key"):
        uploader.upload(job)
    assert service.calls == []


def test_sha256_mismatch_blocks(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, video = make_approved_job(fake_queue, tmp_path, "job-gate-sha")
    # Change the file after the job was created: fingerprint no longer matches.
    video.write_bytes(b"CHANGED CONTENT - different bytes entirely")
    uploader = _uploader(service, fake_queue, quota_tracker, make_settings())
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        uploader.upload(job)
    assert service.calls == []


def test_missing_source_file_blocks(fake_queue, quota_tracker, tmp_path):
    service = DryRunService()
    job, video = make_approved_job(fake_queue, tmp_path, "job-gate-missing")
    video.unlink()
    uploader = _uploader(service, fake_queue, quota_tracker, make_settings())
    with pytest.raises(FileNotFoundError, match="source file not found"):
        uploader.upload(job)
    assert service.calls == []


def test_gate_order_dry_run_checked_first(quota_tracker, tmp_path):
    # Dry-run ON *and* kill switch OFF: the dry-run error must win,
    # because it is checked first (before touching queue or quota).
    queue = FakeQueue(agent_id="muse")  # kill switch OFF
    service = DryRunService()
    job, _ = make_approved_job(queue, tmp_path, "job-gate-order")
    uploader = _uploader(
        service, queue, quota_tracker, make_settings(dry_run=True)
    )
    with pytest.raises(DryRunBlockedError):
        uploader.upload(job)
    assert service.calls == []
