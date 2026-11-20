"""Shared exception types for the uploader."""


class UploadBlockedError(Exception):
    """An upload was refused by a safety gate (kill switch, stop order,
    dry-run, quota, or an illegal state transition)."""


class KillSwitchOffError(UploadBlockedError):
    """The global kill switch is OFF (or the database is unreachable,
    which is treated as OFF — fail closed)."""


class DryRunBlockedError(UploadBlockedError):
    """A real upload was attempted while dry-run mode is on."""


class UploadOutcomeUnknownError(Exception):
    """YouTube may have accepted an upload but the response was lost.

    The job must move to ``upload_outcome_unknown`` and be reconciled
    before any retry. Never re-upload from this error.
    """


class SessionExpiredError(Exception):
    """A resumable upload session URI has expired (~1 week of
    inactivity). The caller must start a fresh session."""


class VerificationError(Exception):
    """A post-upload verification step failed (caption, thumbnail,
    playlist, or metadata does not match what was requested)."""


class ReconciliationUncertainError(Exception):
    """Reconciliation could neither prove nor disprove that a video
    exists on YouTube for this job. Human review is required."""
