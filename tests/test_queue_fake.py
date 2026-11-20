"""FakeQueue tests: lifecycle, kill switch, leases, quota store,
concurrency, and the live client's fail-closed kill switch.

No live database anywhere: QueueClient is only ever constructed
against an unreachable DSN, and only ``uploads_enabled()`` (which
must fail closed) is called on it.
"""

import threading
from datetime import date, datetime, timezone

import pytest

from youtube_uploader.queue import FakeQueue, QueueClient
from youtube_uploader.state import JobStatus

DAY = date(2026, 11, 20)


def _create(queue, job_id, sha=None, **overrides):
    kwargs = dict(
        job_id=job_id,
        topic="Test topic",
        source_file=f"/tmp/{job_id}.mp4",
        file_sha256=sha or ("sha-" + job_id),
        title="Test title",
        description="Test description",
        metadata={},
    )
    kwargs.update(overrides)
    return queue.create_job(**kwargs)


def _approve(queue, job_id):
    return queue.transition(job_id, JobStatus.APPROVED_FOR_UPLOAD)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_create_approve_claim_heartbeat_session_transitions():
    queue = FakeQueue(agent_id="muse")
    job = _create(queue, "job-q-001")
    assert job["status"] == JobStatus.QUEUED.value
    assert job["attempt_count"] == 0

    _approve(queue, "job-q-001")
    claimed = queue.claim_job(lease_seconds=60)
    assert claimed is not None
    assert claimed["job_id"] == "job-q-001"
    assert claimed["status"] == JobStatus.UPLOADING.value
    assert claimed["claimed_by"] == "muse"
    assert claimed["attempt_count"] == 1
    assert claimed["lease_expires_at"] is not None
    assert claimed["lease_expires_at"] > datetime.now(timezone.utc)

    old_lease = claimed["lease_expires_at"]
    queue.heartbeat("job-q-001", lease_seconds=3600)
    assert queue.get_job("job-q-001")["lease_expires_at"] > old_lease

    queue.save_session(
        "job-q-001", "https://upload.example/session/1", 12345
    )
    row = queue.get_job("job-q-001")
    assert row["upload_session_uri"] == "https://upload.example/session/1"
    assert row["bytes_uploaded"] == 12345

    queue.transition("job-q-001", JobStatus.UPLOADED_PRIVATE, video_id="VID1")
    row = queue.get_job("job-q-001")
    assert row["status"] == JobStatus.UPLOADED_PRIVATE.value
    assert row["video_id"] == "VID1"
    # Leaving 'uploading' clears the lease.
    assert row["claimed_by"] is None
    assert row["lease_expires_at"] is None

    queue.transition("job-q-001", JobStatus.ASSETS_VERIFIED)
    queue.transition("job-q-001", JobStatus.READY_FOR_REVIEW)
    queue.transition("job-q-001", JobStatus.PUBLISHED)
    assert queue.get_job("job-q-001")["status"] == JobStatus.PUBLISHED.value


def test_claim_returns_none_when_nothing_approved():
    queue = FakeQueue()
    _create(queue, "job-q-none")  # stays queued
    assert queue.claim_job() is None


def test_claim_picks_oldest_approved_job():
    queue = FakeQueue()
    _create(queue, "job-q-old")
    _create(queue, "job-q-new")
    _approve(queue, "job-q-old")
    _approve(queue, "job-q-new")
    assert queue.claim_job()["job_id"] == "job-q-old"
    assert queue.claim_job()["job_id"] == "job-q-new"
    assert queue.claim_job() is None


def test_duplicate_job_id_rejected():
    queue = FakeQueue()
    _create(queue, "job-q-dup")
    with pytest.raises(ValueError, match="Duplicate job_id"):
        _create(queue, "job-q-dup", sha="different-sha")


def test_live_duplicate_file_sha256_rejected():
    queue = FakeQueue()
    _create(queue, "job-q-a", sha="same-sha")
    with pytest.raises(ValueError, match="file_sha256"):
        _create(queue, "job-q-b", sha="same-sha")


def test_duplicate_sha_allowed_after_terminal_state():
    queue = FakeQueue()
    _create(queue, "job-q-a", sha="same-sha")
    queue.transition("job-q-a", JobStatus.CANCELLED)
    # A cancelled job no longer blocks a fresh job for the same file.
    row = _create(queue, "job-q-b", sha="same-sha")
    assert row["job_id"] == "job-q-b"


def test_illegal_transition_raises():
    queue = FakeQueue()
    _create(queue, "job-q-illegal")
    with pytest.raises(ValueError, match="Illegal job transition"):
        queue.transition("job-q-illegal", JobStatus.PUBLISHED)


def test_transition_unknown_job_raises():
    queue = FakeQueue()
    with pytest.raises(ValueError, match="Unknown job_id"):
        queue.transition("job-q-missing", JobStatus.CANCELLED)


def test_record_attempt_duplicate_number_rejected():
    queue = FakeQueue()
    _create(queue, "job-q-att")
    queue.record_attempt("job-q-att", 1, "failed", error="boom")
    with pytest.raises(ValueError, match="Duplicate attempt_no"):
        queue.record_attempt("job-q-att", 1, "success")


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_defaults_off():
    queue = FakeQueue()
    assert queue.uploads_enabled() is False


def test_kill_switch_toggles():
    queue = FakeQueue()
    queue.set_uploads_enabled(True)
    assert queue.uploads_enabled() is True
    queue.set_uploads_enabled(False)
    assert queue.uploads_enabled() is False


def test_kill_switch_on_via_constructor():
    assert FakeQueue(uploads_enabled=True).uploads_enabled() is True


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def test_release_expired_leases_returns_job_to_approved():
    queue = FakeQueue()
    _create(queue, "job-q-lease")
    _approve(queue, "job-q-lease")
    # A negative lease is already expired.
    queue.claim_job(lease_seconds=-60)
    assert queue.get_job("job-q-lease")["status"] == JobStatus.UPLOADING.value
    assert queue.release_expired_leases() == 1
    row = queue.get_job("job-q-lease")
    assert row["status"] == JobStatus.APPROVED_FOR_UPLOAD.value
    assert row["claimed_by"] is None
    assert row["lease_expires_at"] is None
    # Nothing left to release.
    assert queue.release_expired_leases() == 0


def test_live_lease_is_not_released():
    queue = FakeQueue()
    _create(queue, "job-q-live")
    _approve(queue, "job-q-live")
    queue.claim_job(lease_seconds=3600)
    assert queue.release_expired_leases() == 0
    assert queue.get_job("job-q-live")["status"] == JobStatus.UPLOADING.value


def test_heartbeat_noop_for_unclaimed_job(caplog):
    queue = FakeQueue()
    _create(queue, "job-q-beat")
    # Not uploading / not ours: a warning, no exception, no lease.
    queue.heartbeat("job-q-beat")
    assert queue.get_job("job-q-beat")["lease_expires_at"] is None


# ---------------------------------------------------------------------------
# Quota store (FakeQueue implements the QuotaStore protocol)
# ---------------------------------------------------------------------------


def test_quota_store_get_usage_and_record():
    queue = FakeQueue()
    assert queue.get_usage(DAY) == {}
    queue.record(DAY, "captions.insert", 400)
    queue.record(DAY, "captions.insert", 400)
    queue.record(DAY, "videos.insert", 1)
    assert queue.get_usage(DAY) == {"captions.insert": 800, "videos.insert": 1}


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_claim_exactly_one_winner():
    queue = FakeQueue()
    _create(queue, "job-q-race")
    _approve(queue, "job-q-race")
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(timeout=5)
        claimed = queue.claim_job()
        with lock:
            results.append(claimed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 8
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0]["job_id"] == "job-q-race"
    assert winners[0]["attempt_count"] == 1


def test_concurrent_create_distinct_jobs_all_succeed():
    queue = FakeQueue()
    errors: list = []
    barrier = threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait(timeout=5)
            _create(queue, f"job-q-conc-{i}")
        except Exception as exc:  # noqa: BLE001 — collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    for i in range(8):
        assert queue.get_job(f"job-q-conc-{i}") is not None


# ---------------------------------------------------------------------------
# Live client: fail closed, never a real server
# ---------------------------------------------------------------------------


def test_queue_client_fail_closed_on_unreachable_dsn():
    client = QueueClient("postgresql://invalid.invalid:1/db", "muse")
    try:
        # No exception, no hang: an unreachable database reads as OFF.
        assert client.uploads_enabled() is False
    finally:
        client.close()
