"""State machine tests (Plan v2, Phase C).

The 10 states are: queued, approved_for_upload, uploading,
upload_outcome_unknown, uploaded_private, assets_verified,
ready_for_review, published, failed, cancelled. Per the owner order
of 20 Nov 2026 ~22:04 there is NO approved_for_publishing state and
NO scheduled state: every video publishes Public automatically once
assets verify.
"""

import pytest

from youtube_uploader.state import (
    ALLOWED_TRANSITIONS,
    UPLOADABLE_STATES,
    VIDEO_MAY_EXIST_STATES,
    JobStatus,
    can_transition,
    validate_transition,
)

EXPECTED_VALUES = {
    "queued",
    "approved_for_upload",
    "uploading",
    "upload_outcome_unknown",
    "uploaded_private",
    "assets_verified",
    "ready_for_review",
    "published",
    "failed",
    "cancelled",
}

HAPPY_PATH = [
    (JobStatus.QUEUED, JobStatus.APPROVED_FOR_UPLOAD),
    (JobStatus.APPROVED_FOR_UPLOAD, JobStatus.UPLOADING),
    (JobStatus.UPLOADING, JobStatus.UPLOADED_PRIVATE),
    (JobStatus.UPLOADED_PRIVATE, JobStatus.ASSETS_VERIFIED),
    (JobStatus.ASSETS_VERIFIED, JobStatus.READY_FOR_REVIEW),
    (JobStatus.READY_FOR_REVIEW, JobStatus.PUBLISHED),
]

ILLEGAL = [
    (JobStatus.QUEUED, JobStatus.PUBLISHED),
    (JobStatus.QUEUED, JobStatus.UPLOADING),
    (JobStatus.QUEUED, JobStatus.UPLOADED_PRIVATE),
    (JobStatus.APPROVED_FOR_UPLOAD, JobStatus.PUBLISHED),
    (JobStatus.APPROVED_FOR_UPLOAD, JobStatus.UPLOADED_PRIVATE),
    (JobStatus.UPLOADING, JobStatus.ASSETS_VERIFIED),
    (JobStatus.UPLOADING, JobStatus.PUBLISHED),
    (JobStatus.UPLOADED_PRIVATE, JobStatus.PUBLISHED),
    (JobStatus.UPLOADED_PRIVATE, JobStatus.READY_FOR_REVIEW),
    (JobStatus.ASSETS_VERIFIED, JobStatus.PUBLISHED),
    (JobStatus.ASSETS_VERIFIED, JobStatus.UPLOADED_PRIVATE),
    (JobStatus.PUBLISHED, JobStatus.QUEUED),
    (JobStatus.PUBLISHED, JobStatus.FAILED),
    (JobStatus.PUBLISHED, JobStatus.CANCELLED),
    (JobStatus.PUBLISHED, JobStatus.UPLOADED_PRIVATE),
    (JobStatus.FAILED, JobStatus.QUEUED),
    (JobStatus.FAILED, JobStatus.APPROVED_FOR_UPLOAD),
    (JobStatus.CANCELLED, JobStatus.QUEUED),
    (JobStatus.CANCELLED, JobStatus.APPROVED_FOR_UPLOAD),
]


def test_all_ten_states_present():
    assert len(JobStatus) == 10
    assert {s.value for s in JobStatus} == EXPECTED_VALUES
    # Every state has a transition-table entry (possibly empty).
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


def test_no_publishing_approval_or_scheduled_state():
    values = {s.value for s in JobStatus}
    assert "approved_for_publishing" not in values
    assert "scheduled" not in values
    assert not hasattr(JobStatus, "APPROVED_FOR_PUBLISHING")
    assert not hasattr(JobStatus, "SCHEDULED")


@pytest.mark.parametrize("from_state,to_state", HAPPY_PATH)
def test_happy_path_transitions_are_legal(from_state, to_state):
    assert can_transition(from_state, to_state) is True
    validate_transition(from_state, to_state)  # must not raise


@pytest.mark.parametrize("from_state,to_state", ILLEGAL)
def test_illegal_transitions_rejected(from_state, to_state):
    assert can_transition(from_state, to_state) is False
    with pytest.raises(ValueError, match="Illegal job transition"):
        validate_transition(from_state, to_state)


def test_upload_outcome_unknown_transitions():
    # Reconciliation found the video: adopt it, continue as private.
    assert can_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.UPLOADED_PRIVATE
    )
    # Reconciliation proved nothing was uploaded: safe to retry.
    assert can_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.APPROVED_FOR_UPLOAD
    )
    assert can_transition(JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.FAILED)
    assert can_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.CANCELLED
    )
    # But never straight to published, and never back to uploading
    # without going through approval again.
    assert not can_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.PUBLISHED
    )
    assert not can_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.UPLOADING
    )
    validate_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.UPLOADED_PRIVATE
    )
    validate_transition(
        JobStatus.UPLOAD_OUTCOME_UNKNOWN, JobStatus.APPROVED_FOR_UPLOAD
    )


def test_uploadable_states():
    assert UPLOADABLE_STATES == {JobStatus.APPROVED_FOR_UPLOAD}


def test_video_may_exist_states():
    assert JobStatus.UPLOADING in VIDEO_MAY_EXIST_STATES
    assert JobStatus.UPLOAD_OUTCOME_UNKNOWN in VIDEO_MAY_EXIST_STATES
    assert JobStatus.UPLOADED_PRIVATE in VIDEO_MAY_EXIST_STATES
    assert JobStatus.PUBLISHED in VIDEO_MAY_EXIST_STATES
    # Pre-upload states: no video can exist yet.
    assert JobStatus.QUEUED not in VIDEO_MAY_EXIST_STATES
    assert JobStatus.APPROVED_FOR_UPLOAD not in VIDEO_MAY_EXIST_STATES
    # Mid-pipeline states also imply a video exists.
    assert JobStatus.ASSETS_VERIFIED in VIDEO_MAY_EXIST_STATES
    assert JobStatus.READY_FOR_REVIEW in VIDEO_MAY_EXIST_STATES
