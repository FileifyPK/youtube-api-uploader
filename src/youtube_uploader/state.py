"""Job state machine (Plan v2, Phase C).

Upload and publishing are separate STEPS. Every upload starts
private (``privacyStatus: private`` on every upload request) as a
safety measure. Once assets verify, the video publishes PUBLIC
automatically per the owner's standing rule (owner order 20 Nov
2026 ~22:04): every video is public, there is NO per-video
publishing approval and NO scheduled publishing. The gate that
matters is at upload time: the kill switch + the owner's stop
order. There is no path from any pre-upload state straight to
``published``.

``upload_outcome_unknown`` exists because YouTube provides no client
idempotency key for videos.insert: if the response (video ID) is lost
after YouTube may have accepted the upload, the job must NOT be
blindly re-uploaded. It goes to ``upload_outcome_unknown`` and waits
for reconciliation (see reconcile.py) and, if the outcome stays
uncertain, human review.
"""

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    APPROVED_FOR_UPLOAD = "approved_for_upload"
    UPLOADING = "uploading"
    UPLOAD_OUTCOME_UNKNOWN = "upload_outcome_unknown"
    UPLOADED_PRIVATE = "uploaded_private"
    ASSETS_VERIFIED = "assets_verified"
    READY_FOR_REVIEW = "ready_for_review"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


# States from which an upload (videos.insert) may legally start.
UPLOADABLE_STATES = frozenset({JobStatus.APPROVED_FOR_UPLOAD})

# Terminal states — no further transitions.
TERMINAL_STATES = frozenset(
    {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED}
)

# States that mean "a video may already exist on YouTube for this job".
# Re-upload from any of these states is forbidden without reconciliation.
VIDEO_MAY_EXIST_STATES = frozenset(
    {
        JobStatus.UPLOADING,
        JobStatus.UPLOAD_OUTCOME_UNKNOWN,
        JobStatus.UPLOADED_PRIVATE,
        JobStatus.ASSETS_VERIFIED,
        JobStatus.READY_FOR_REVIEW,
        JobStatus.PUBLISHED,
    }
)

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.APPROVED_FOR_UPLOAD, JobStatus.CANCELLED}
    ),
    JobStatus.APPROVED_FOR_UPLOAD: frozenset(
        {JobStatus.UPLOADING, JobStatus.CANCELLED}
    ),
    JobStatus.UPLOADING: frozenset(
        {
            JobStatus.UPLOADED_PRIVATE,
            JobStatus.UPLOAD_OUTCOME_UNKNOWN,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.UPLOAD_OUTCOME_UNKNOWN: frozenset(
        {
            # Reconciliation found the video: adopt it, continue as private.
            JobStatus.UPLOADED_PRIVATE,
            # Reconciliation proved nothing was uploaded: safe to retry.
            JobStatus.APPROVED_FOR_UPLOAD,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.UPLOADED_PRIVATE: frozenset(
        {JobStatus.ASSETS_VERIFIED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.ASSETS_VERIFIED: frozenset(
        {JobStatus.READY_FOR_REVIEW, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.READY_FOR_REVIEW: frozenset(
        # Publish to Public is automatic per the owner's standing rule
        # (owner order 20 Nov 2026): no per-video approval, no scheduling.
        {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.PUBLISHED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


def can_transition(from_state: JobStatus, to_state: JobStatus) -> bool:
    """True if ``from_state -> to_state`` is a legal transition."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def validate_transition(from_state: JobStatus, to_state: JobStatus) -> None:
    """Raise ValueError on an illegal transition."""
    if not can_transition(from_state, to_state):
        raise ValueError(
            f"Illegal job transition: {from_state.value} -> {to_state.value}"
        )
