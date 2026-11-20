"""Resumable upload engine (Plan v2, Phase B).

:class:`ResumableUploader` performs the ``videos.insert`` step of the
pipeline against an injected YouTube service object (a real
``googleapiclient`` resource or a fake). It never creates its own
service and never makes a network call except through that object.

Safety gates — checked IN ORDER at the start of
:meth:`ResumableUploader.upload`, before ANY API call:

1. ``settings.dry_run`` → :class:`DryRunBlockedError`
2. ``settings.uploads_force_off`` → :class:`UploadBlockedError`
3. ``queue.uploads_enabled()`` False → :class:`KillSwitchOffError`
   (a dead database reads as OFF — fail closed)
4. job status must be ``APPROVED_FOR_UPLOAD`` — or ``UPLOADING``
   with the job claimed by this agent (``claim_job()`` moves the job
   to ``uploading``, so the live worker path hands us a claimed job;
   a job uploading under another agent's claim is still refused) —
   else :class:`UploadBlockedError`
5. ``quota.reserve("videos.insert")`` → :class:`QuotaExceeded`
   propagates, never blind-retried

Privacy: ``status.privacyStatus`` is ALWAYS the literal string
``"private"``, hardcoded at the single place the request body is
built (:data:`PRIVACY_STATUS`) and guarded — if it is ever anything
else, the upload aborts. Upload and publish are separate STEPS:
every upload starts private as a safety measure, and once assets
verify the pipeline publishes the video PUBLIC automatically per
the owner's standing rule (owner order 20 Nov 2026 ~22:04 — every
video is public, no per-video publishing approval,
publishing). The gate that matters is at upload time: the kill
switch + the owner's stop order.

Assessment: ``madeForKids`` and ``containsSyntheticMedia`` come ONLY
from ``job["metadata"]["assessment"]``. If the assessment dict or
either key is missing, upload raises ValueError — they are NEVER
defaulted (Plan v2: a wrong synthetic-media disclosure violates
YouTube policy).

Resumable protocol (google-api-python-client behaviour, verified
against the installed library):

* The session URI appears on ``request.resumable_uri`` after the
  first ``next_chunk()`` call; ``request.resumable_progress`` tracks
  uploaded bytes. Both are persisted via
  ``queue.save_session(job_id, session_uri, bytes_uploaded)`` after
  EVERY chunk, so a crash resumes instead of restarting.
* To resume, the saved URI and byte count are set back onto a fresh
  request and ``request._in_error_state`` is set so the library first
  queries the server for the true upload state (empty PUT with
  ``Content-Range: bytes */size``) before sending more data.
* An expired session (HTTP 404/410 on resume) means NO video was
  created, so a FRESH session is started; restarts are capped at
  :data:`MAX_SESSION_RESTARTS` per job attempt and each restart is
  recorded in the audit log (NOT the attempt log: ``upload_attempts``
  has ``UNIQUE(job_id, attempt_no)``, so a restart — same attempt —
  must not consume the attempt's one row).
* If the connection drops AFTER the final chunk was sent but before
  the video-ID response arrives (or retries are exhausted on the
  final chunk), the outcome is genuinely unknown: YouTube may have
  the video. :class:`UploadOutcomeUnknownError` is raised and
  ``videos.insert`` is NEVER retried for that job — the job moves to
  ``upload_outcome_unknown`` for reconciliation (see
  :mod:`youtube_uploader.reconcile`).

Retries: chunk uploads use exponential backoff with full jitter for
retryable 5xx errors, max :data:`MAX_CHUNK_RETRIES` retries. Quota
errors (403 ``quotaExceeded``) are NEVER retried. Retrying
``next_chunk()`` on the SAME resumable session is safe — the danger
is only re-issuing ``videos.insert``, which this module never does
after an ambiguous final chunk.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from youtube_uploader.config import Settings
from youtube_uploader.dryrun import assert_not_dry_run
from youtube_uploader.errors import (
    DryRunBlockedError,  # noqa: F401  (re-exported for callers)
    KillSwitchOffError,
    SessionExpiredError,
    UploadBlockedError,
    UploadOutcomeUnknownError,
)
from youtube_uploader.quota import QuotaExceeded, QuotaTracker
from youtube_uploader.state import JobStatus

try:  # httplib2 ships with google-api-python-client; be defensive.
    import httplib2

    _TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
        httplib2.HttpLib2Error,
        ConnectionError,
        TimeoutError,
        OSError,
    )
except ImportError:  # pragma: no cover
    _TRANSPORT_ERRORS = (ConnectionError, TimeoutError, OSError)

if TYPE_CHECKING:  # pragma: no cover
    from youtube_uploader.quota import QuotaStore

log = logging.getLogger(__name__)

# --- Tunables -------------------------------------------------------------

#: Upload chunk size (256 KiB, per the YouTube resumable-upload guide:
#: chunks must be multiples of 256 KiB, except the final chunk).
CHUNK_SIZE = 256 * 1024

#: Max backoff retries per chunk for retryable errors.
MAX_CHUNK_RETRIES = 5

#: Max fresh-session restarts per job attempt (expired sessions).
MAX_SESSION_RESTARTS = 2

#: HTTP statuses worth retrying with backoff (transient server errors).
RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

#: HTTP statuses that mean the resumable session is gone (expired
#: after ~1 week of inactivity, or otherwise invalid).
SESSION_EXPIRED_STATUSES = frozenset({404, 410})

# --- The single privacy constant ------------------------------------------

#: THE privacy status for every upload. Hardcoded here and nowhere
#: else; :func:`build_insert_body` guards it with a hard check.
PRIVACY_STATUS = "private"


# --- Queue protocol (mirrors the Phase C QueueClient interface) -----------

class QueueClientProtocol(Protocol):
    """Structural type for the Phase C QueueClient.

    Only the methods this engine uses are listed; the real client has
    the full interface (see the module docstring of ``queue.py``).
    """

    def uploads_enabled(self) -> bool: ...

    def transition(
        self,
        job_id: str,
        to_state: JobStatus,
        video_id: str | None = ...,
        error: str | None = ...,
    ) -> dict: ...

    def save_session(
        self, job_id: str, session_uri: str, bytes_uploaded: int
    ) -> None: ...

    def record_attempt(
        self,
        job_id: str,
        attempt_no: int,
        outcome: str,
        error: str | None = ...,
        video_id: str | None = ...,
    ) -> None: ...

    def audit(
        self,
        job_id: str | None,
        agent_id: str,
        action: str,
        detail: str = ...,
    ) -> None: ...


# --- File helpers ---------------------------------------------------------


def compute_file_sha256(path: str | Path) -> str:
    """SHA-256 hex digest of a file, streamed in 1 MiB blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_source_file(job: dict[str, Any]) -> Path:
    """Check the job's source file exists and matches its fingerprint.

    Returns the resolved :class:`Path`. Raises FileNotFoundError if
    the file is missing, ValueError if the SHA-256 does not match the
    fingerprint recorded when the job was created (a changed file
    must never be uploaded under an old job's metadata).
    """
    source = job.get("source_file")
    if not source:
        raise ValueError(f"Job {job.get('job_id')}: no source_file set")
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(
            f"Job {job.get('job_id')}: source file not found: {path}"
        )
    expected = job.get("file_sha256")
    if not expected:
        raise ValueError(
            f"Job {job.get('job_id')}: no file_sha256 fingerprint recorded"
        )
    actual = compute_file_sha256(path)
    if actual != expected:
        raise ValueError(
            f"Job {job.get('job_id')}: source file SHA-256 mismatch — "
            "the file changed since the job was created; refusing upload"
        )
    return path


# --- Request body ---------------------------------------------------------


def build_insert_body(job: dict[str, Any]) -> dict[str, Any]:
    """Build the ``videos.insert`` request body for a job.

    ``privacyStatus`` is ALWAYS the literal ``"private"`` (see
    :data:`PRIVACY_STATUS`); a guard raises if it is anything else.
    ``madeForKids`` / ``containsSyntheticMedia`` come ONLY from the
    per-video assessment in ``job["metadata"]["assessment"]`` — a
    missing assessment or key raises ValueError, never a default.
    """
    metadata = job.get("metadata") or {}
    assessment = metadata.get("assessment")
    if not isinstance(assessment, dict):
        raise ValueError(
            f"Job {job.get('job_id')}: missing metadata.assessment — "
            "madeForKids/containsSyntheticMedia must be assessed "
            "per video and are never defaulted"
        )
    try:
        made_for_kids = assessment["made_for_kids"]
        contains_synthetic = assessment["contains_synthetic_media"]
    except KeyError as exc:
        raise ValueError(
            f"Job {job.get('job_id')}: assessment is missing key "
            f"{exc.args[0]!r} — both made_for_kids and "
            "contains_synthetic_media are required"
        ) from exc
    if not isinstance(made_for_kids, bool) or not isinstance(
        contains_synthetic, bool
    ):
        raise ValueError(
            f"Job {job.get('job_id')}: assessment values must be booleans"
        )

    snippet: dict[str, Any] = {
        "title": job["title"],
        "description": job.get("description", ""),
    }
    if metadata.get("tags"):
        snippet["tags"] = list(metadata["tags"])
    if metadata.get("language"):
        snippet["defaultLanguage"] = metadata["language"]
        snippet["defaultAudioLanguage"] = metadata["language"]
    if metadata.get("category_id"):
        snippet["categoryId"] = str(metadata["category_id"])

    status: dict[str, Any] = {
        # THE privacy status — hardcoded "private", single place.
        "privacyStatus": PRIVACY_STATUS,
        "madeForKids": made_for_kids,
        "containsSyntheticMedia": contains_synthetic,
    }
    # Guard: if this is ever anything but "private", abort loudly.
    # A missing or wrong privacyStatus could default to Public.
    if status["privacyStatus"] != "private":
        raise ValueError(
            "Refusing upload: privacyStatus is not 'private' "
            f"(got {status['privacyStatus']!r})"
        )

    return {"snippet": snippet, "status": status}


# --- Error classification -------------------------------------------------


def _http_status(exc: HttpError) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_quota_error(exc: HttpError) -> bool:
    """True if the error is a YouTube quota rejection (never retried)."""
    if _http_status(exc) != 403:
        return False
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, str):
        content = content.encode("utf-8", "replace")
    return b"quotaExceeded" in content or b"quota" in content.lower()


def _safe_error(exc: BaseException) -> str:
    """Error text safe for the attempt log: short, single-line, and
    never containing credentials (this module never puts secrets into
    exceptions in the first place)."""
    return " ".join(str(exc).split())[:500]


def _sleep_backoff(retry_no: int) -> None:
    """Exponential backoff with full jitter: sleep U(0, min(2^n, 30))s."""
    cap = min(2**retry_no, 30)
    delay = random.uniform(0, cap)
    log.info("Backing off %.1fs before retry #%d", delay, retry_no)
    time.sleep(delay)


def _current_progress(request: Any, fallback: int) -> int:
    """Bytes uploaded so far, from the request object if available."""
    value = getattr(request, "resumable_progress", None)
    if isinstance(value, int) and value >= 0:
        return value
    return fallback


def _normalize_status(value: JobStatus | str | None) -> JobStatus | None:
    if value is None:
        return None
    if isinstance(value, JobStatus):
        return value
    try:
        return JobStatus(str(value))
    except ValueError:
        return None


# --- The engine -----------------------------------------------------------


class ResumableUploader:
    """Resumable ``videos.insert`` engine.

    All YouTube calls go through the injected ``service`` object.
    ``queue`` is the Phase C QueueClient (or a fake implementing the
    same interface); ``quota`` is a :class:`QuotaTracker`.
    """

    def __init__(
        self,
        service: Any,
        queue: QueueClientProtocol,
        quota: QuotaTracker,
        settings: Settings,
    ) -> None:
        self.service = service
        self.queue = queue
        self.quota = quota
        self.settings = settings

    # -- public API -------------------------------------------------------

    def upload(self, job: dict[str, Any]) -> str:
        """Upload one approved job; return the YouTube video ID.

        Gates (in order): dry-run → force-off → kill switch → job
        status → quota reservation. Then the file is validated, the
        job moves to ``uploading``, and chunks flow until YouTube
        returns the video resource.
        """
        # Gate (a): dry-run — FIRST, before touching queue or quota.
        assert_not_dry_run(self.settings)

        job_id = job["job_id"]
        attempt_no = int(job.get("attempt_count", job.get("attempt_no", 1)))

        # Gate (b): local force-off.
        if self.settings.uploads_force_off:
            raise UploadBlockedError(
                f"Job {job_id}: uploads are force-disabled "
                "(UPLOADS_FORCE_OFF=true)."
            )

        # Gate (c): global kill switch (fail closed).
        if not self.queue.uploads_enabled():
            raise KillSwitchOffError(
                f"Job {job_id}: global upload kill switch is OFF "
                "(or the queue database is unreachable — fail closed)."
            )

        # Gate (d): only an approved job may start an upload — or a
        # job this worker has already claimed. claim_job() moves the
        # job to "uploading", so the live worker path (claim, then
        # upload) hands us a job in "uploading" held by this agent;
        # refusing it would make upload-next unable to upload at all.
        # A job uploading under ANOTHER agent's claim is still refused.
        status = _normalize_status(job.get("status"))
        claimed_by_self = (
            status is JobStatus.UPLOADING
            and job.get("claimed_by") == self.settings.agent_id
        )
        if status is not JobStatus.APPROVED_FOR_UPLOAD and not claimed_by_self:
            raise UploadBlockedError(
                f"Job {job_id}: status is "
                f"{status.value if status else job.get('status')!r}, "
                "not 'approved_for_upload' — refusing upload."
            )

        # Gate (e): quota reservation (QuotaExceeded propagates).
        self.quota.reserve("videos.insert")

        source = validate_source_file(job)
        file_size = source.stat().st_size
        body = build_insert_body(job)
        if status is JobStatus.APPROVED_FOR_UPLOAD:
            # A self-claimed job is already "uploading" — no
            # (illegal) uploading -> uploading transition.
            self.queue.transition(job_id, JobStatus.UPLOADING)
        log.info(
            "Job %s: starting upload of %s (%d bytes)",
            job_id,
            source.name,
            file_size,
        )

        # Queue rows store the session under "upload_session_uri"
        # (save_session / the jobs table column); external job JSON
        # may use "session_uri". Read both, or a crashed upload handed
        # over as a queue row would restart instead of resuming.
        session_uri = job.get("session_uri") or job.get("upload_session_uri")
        bytes_uploaded = int(job.get("bytes_uploaded") or 0)
        restarts = 0
        try:
            while True:  # fresh-session restart loop (expired sessions)
                try:
                    video_id = self._run_upload_session(
                        job, body, source, file_size,
                        session_uri, bytes_uploaded,
                    )
                    break
                except SessionExpiredError:
                    restarts += 1
                    log.warning(
                        "Job %s: resumable session expired; restart %d/%d",
                        job_id,
                        restarts,
                        MAX_SESSION_RESTARTS,
                    )
                    # Audit log, NOT record_attempt: upload_attempts
                    # has UNIQUE(job_id, attempt_no), so recording a
                    # restart here would make the final success/failure
                    # record for the same attempt_no fail.
                    self.queue.audit(
                        job_id,
                        self.settings.agent_id,
                        "session_restart",
                        (
                            "Resumable session expired (404/410); "
                            "starting a fresh session — no video was "
                            "created, so restart is safe."
                        ),
                    )
                    if restarts > MAX_SESSION_RESTARTS:
                        raise
                    session_uri = None
                    bytes_uploaded = 0
        except UploadOutcomeUnknownError as exc:
            # YouTube may already have the video: NEVER re-insert.
            self.queue.record_attempt(
                job_id,
                attempt_no,
                "upload_outcome_unknown",
                error=_safe_error(exc),
            )
            self.queue.transition(
                job_id,
                JobStatus.UPLOAD_OUTCOME_UNKNOWN,
                error=_safe_error(exc),
            )
            raise
        except Exception as exc:  # noqa: BLE001 — recorded, then raised
            self.queue.record_attempt(
                job_id, attempt_no, "failed", error=_safe_error(exc)
            )
            try:
                self.queue.transition(
                    job_id, JobStatus.FAILED, error=_safe_error(exc)
                )
            except Exception:  # noqa: BLE001 — original error wins
                log.exception(
                    "Job %s: could not transition to FAILED after error",
                    job_id,
                )
            raise

        self.quota.record("videos.insert")
        self.queue.record_attempt(
            job_id, attempt_no, "success", video_id=video_id
        )
        self.queue.transition(
            job_id, JobStatus.UPLOADED_PRIVATE, video_id=video_id
        )
        log.info("Job %s: uploaded as private video %s", job_id, video_id)
        return video_id

    # -- internals ----------------------------------------------------------

    def _run_upload_session(
        self,
        job: dict[str, Any],
        body: dict[str, Any],
        source: Path,
        file_size: int,
        session_uri: str | None,
        bytes_uploaded: int,
    ) -> str:
        """Run chunks on one resumable session; return the video ID.

        Raises :class:`SessionExpiredError` if the session URI is
        dead (caller restarts fresh), :class:`UploadOutcomeUnknownError`
        if the final chunk's outcome cannot be established, and
        :class:`QuotaExceeded` (unretried) on quota rejection.
        """
        job_id = job["job_id"]
        mimetype = (
            mimetypes.guess_type(str(source))[0] or "video/mp4"
        )
        media = MediaFileUpload(
            str(source),
            mimetype=mimetype,
            chunksize=CHUNK_SIZE,
            resumable=True,
        )
        request = self.service.videos().insert(
            part="snippet,status", body=body, media_body=media
        )

        if session_uri:
            # Resume: point the fresh request at the saved session and
            # let the library reconcile progress with the server on the
            # next next_chunk() call (it issues an empty PUT and reads
            # the server's range header when _in_error_state is set).
            # Attribute guards keep test fakes working.
            if hasattr(request, "resumable_uri"):
                request.resumable_uri = session_uri
            if hasattr(request, "resumable_progress"):
                request.resumable_progress = bytes_uploaded
            if hasattr(request, "_in_error_state"):
                request._in_error_state = True
            log.info(
                "Job %s: resuming session at %d/%d bytes",
                job_id,
                bytes_uploaded,
                file_size,
            )

        response = None
        retries = 0
        while response is None:
            uploaded = _current_progress(request, bytes_uploaded)
            is_final_chunk = uploaded + CHUNK_SIZE >= file_size
            try:
                status, response = request.next_chunk()
            except HttpError as exc:
                code = _http_status(exc)
                if code in SESSION_EXPIRED_STATUSES:
                    raise SessionExpiredError(
                        f"Job {job_id}: resumable session expired "
                        f"(HTTP {code}); no video was created."
                    ) from exc
                if _is_quota_error(exc):
                    raise QuotaExceeded(
                        f"Job {job_id}: YouTube quota rejected the "
                        "upload (quotaExceeded) — not retried."
                    ) from exc
                if code in RETRYABLE_STATUSES:
                    retries += 1
                    if retries > MAX_CHUNK_RETRIES:
                        if is_final_chunk:
                            raise UploadOutcomeUnknownError(
                                f"Job {job_id}: retries exhausted on "
                                "the FINAL chunk (HTTP "
                                f"{code}) — YouTube may have accepted "
                                "the upload but no video ID arrived; "
                                "not retrying videos.insert."
                            ) from exc
                        raise
                    log.warning(
                        "Job %s: chunk failed with HTTP %s; retry %d/%d",
                        job_id,
                        code,
                        retries,
                        MAX_CHUNK_RETRIES,
                    )
                    _sleep_backoff(retries)
                    continue
                # Any other HttpError (4xx etc.) is a definitive
                # rejection: the video was NOT created. Not unknown.
                raise
            except _TRANSPORT_ERRORS as exc:
                # Connection dropped mid-chunk. Retrying next_chunk()
                # on the SAME session is safe; only the final chunk
                # is ambiguous once retries run out.
                retries += 1
                if retries > MAX_CHUNK_RETRIES:
                    if is_final_chunk:
                        raise UploadOutcomeUnknownError(
                            f"Job {job_id}: connection lost on the "
                            "FINAL chunk and retries exhausted — "
                            "YouTube may have accepted the upload but "
                            "no video ID arrived; not retrying "
                            "videos.insert."
                        ) from exc
                    raise
                log.warning(
                    "Job %s: transport error during chunk (%s); "
                    "retry %d/%d",
                    job_id,
                    _safe_error(exc),
                    retries,
                    MAX_CHUNK_RETRIES,
                )
                _sleep_backoff(retries)
                continue

            retries = 0
            if response is not None:
                bytes_uploaded = file_size
            else:
                bytes_uploaded = _current_progress(request, uploaded)
                if status is not None:
                    try:
                        bytes_uploaded = int(
                            status.progress() * file_size
                        )
                    except (AttributeError, TypeError, ValueError):
                        pass
            # Persist the session URI + byte count after EVERY chunk.
            uri = getattr(request, "resumable_uri", None) or session_uri or ""
            self.queue.save_session(job_id, uri, bytes_uploaded)
            session_uri = uri

        video_id = response.get("id") if isinstance(response, dict) else None
        if not video_id:
            # The upload "finished" but YouTube gave no video ID —
            # the video may exist. Treat as unknown, never re-insert.
            raise UploadOutcomeUnknownError(
                f"Job {job_id}: upload completed but no video ID was "
                "returned; the video may already exist on YouTube."
            )
        return video_id
