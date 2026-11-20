"""Command-line interface (Plan v2, Phase B/D).

Subcommands:

* ``verify-channel`` — confirm the authenticated YouTube channel.
* ``quota-status`` — print today's quota numbers (numbers only).
* ``upload-next`` — claim one approved job and run the full pipeline:
  upload (private) → captions/thumbnail/playlist → verify → publish
  public automatically → ``published``. Publishing is automatic per
  the owner's standing rule (20 Nov 2026): every video is public,
  no per-video publishing approval.
* ``reconcile JOB_ID`` — reconcile an ``upload_outcome_unknown`` job.
* ``dry-run-upload JOB_ID`` — run the pipeline against
  :class:`DryRunService` + :class:`FakeQueue` and print what WOULD
  happen. Never touches the real YouTube API.

Every upload path goes through the same gates as the engine
(dry-run, force-off, kill switch, job status, quota) — the CLI adds
no bypass. The CLI never prints secrets; ``quota-status`` prints
numbers only.

This module performs NO work at import time. Nothing here runs a
real upload unless a human explicitly invokes ``upload-next`` with
the kill switch ON, dry-run OFF, and the owner's stop order lifted.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from youtube_uploader import auth
from youtube_uploader.assets import (
    add_to_playlist,
    publish_video,
    set_thumbnail,
    upload_caption,
)
from youtube_uploader.config import Settings, configure_logging
from youtube_uploader.dryrun import DryRunService, FakeQueue
from youtube_uploader.errors import (
    DryRunBlockedError,
    KillSwitchOffError,
    ReconciliationUncertainError,
    UploadBlockedError,
    UploadOutcomeUnknownError,
    VerificationError,
)
from youtube_uploader.quota import (
    InMemoryQuotaStore,
    QuotaExceeded,
    QuotaTracker,
    pacific_today,
    seconds_until_reset,
)
from youtube_uploader.reconcile import reconcile_unknown_job
from youtube_uploader.state import JobStatus
from youtube_uploader.uploader import ResumableUploader
from youtube_uploader.verify import (
    verify_caption,
    verify_playlist_membership,
    verify_thumbnail,
    verify_video,
)

log = logging.getLogger(__name__)


# --- shared construction --------------------------------------------------


def _load_settings() -> Settings:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    return settings


def _build_service(settings: Settings) -> Any:
    """Refresh the OAuth token and build the live YouTube service."""
    credentials = auth.refresh_access_token(settings)
    return auth.build_youtube_service(credentials)


def _load_queue(settings: Settings) -> Any:
    """Construct the Phase C QueueClient (lazy import).

    Constructor: ``QueueClient(dsn, agent_id)``. Requires
    ``DATABASE_URL`` in the environment (injected from the Secure
    Vault at runtime — never stored in a file).
    """
    if not settings.has_database():
        raise SystemExit(
            "DATABASE_URL is not set — upload-next/reconcile need the "
            "Neon queue. Set it from the Secure Vault (see .env.example)."
        )
    from youtube_uploader.queue import QueueClient

    return QueueClient(settings.database_url, settings.agent_id)


def _quota_tracker(queue: Any | None) -> QuotaTracker:
    """A tracker over the queue's quota store, or in-memory fallback."""
    if queue is not None and hasattr(queue, "get_usage"):
        return QuotaTracker(queue)
    return QuotaTracker(InMemoryQuotaStore())


# --- the full pipeline ----------------------------------------------------


def run_job_pipeline(
    service: Any,
    queue: Any,
    quota: QuotaTracker,
    settings: Settings,
    job: dict[str, Any],
) -> str:
    """Run one job end to end: upload → assets → verify → publish.

    Used by ``upload-next`` (live) and ``dry-run-upload``
    (simulated). Returns the video ID. Raises the engine's errors
    unchanged — the caller reports them.

    Publishing is automatic: once assets verify, the video goes
    PUBLIC via ``videos.update`` and the job transitions straight to
    ``published`` (owner's standing rule, 20 Nov 2026 — no per-video
    publishing approval).
    """
    job_id = job["job_id"]
    uploader = ResumableUploader(service, queue, quota, settings)
    video_id = uploader.upload(job)  # gates + upload + UPLOADED_PRIVATE

    metadata = job.get("metadata") or {}
    caption_file = metadata.get("caption_file")
    thumbnail_file = metadata.get("thumbnail_file")
    playlist_id = metadata.get("playlist_id")

    caption_id = thumbnail_ok = playlist_item_id = None
    if caption_file:
        caption_id = upload_caption(
            service,
            quota,
            video_id,
            caption_file,
            metadata.get("language") or "ur",
        )
    if thumbnail_file:
        thumbnail_ok = set_thumbnail(service, quota, video_id, thumbnail_file)
    if playlist_id:
        playlist_item_id = add_to_playlist(
            service, quota, video_id, playlist_id
        )

    # Verify EVERY step before calling the job done.
    assessment = metadata.get("assessment") or {}
    expected = {
        "title": job.get("title"),
        "description": job.get("description", ""),
        "privacy_status": "private",
        "made_for_kids": assessment.get("made_for_kids"),
        "contains_synthetic_media": assessment.get(
            "contains_synthetic_media"
        ),
    }
    verify_video(service, video_id, expected)
    if caption_file and not verify_caption(service, video_id):
        raise VerificationError(
            f"Video {video_id}: caption verification failed — "
            "no caption track found after upload"
        )
    if thumbnail_file and not verify_thumbnail(service, video_id):
        raise VerificationError(
            f"Video {video_id}: thumbnail verification failed — "
            "no custom thumbnail detected (needs a human look)"
        )
    if playlist_id and not verify_playlist_membership(
        service, video_id, playlist_id
    ):
        raise VerificationError(
            f"Video {video_id}: playlist verification failed — "
            f"not found in playlist {playlist_id}"
        )

    # Record assets only once verified.
    if caption_id:
        queue.record_asset(job_id, "caption", caption_id, True)
    if thumbnail_ok:
        queue.record_asset(job_id, "thumbnail", video_id, True)
    if playlist_item_id:
        queue.record_asset(job_id, "playlist", playlist_item_id, True)

    queue.transition(job_id, JobStatus.ASSETS_VERIFIED)
    queue.transition(job_id, JobStatus.READY_FOR_REVIEW)

    # Publish PUBLIC automatically — the owner's standing rule is
    # that every video is public once assets verify. No per-video
    # approval. Verify the flip took effect
    # before marking the job published.
    publish_video(service, quota, video_id)
    verify_video(
        service, video_id, {**expected, "privacy_status": "public"}
    )
    queue.transition(job_id, JobStatus.PUBLISHED)
    log.info(
        "Job %s: pipeline complete, video %s published public",
        job_id,
        video_id,
    )
    return video_id


# --- subcommands ----------------------------------------------------------


def cmd_verify_channel(args: argparse.Namespace) -> int:
    settings = _load_settings()
    try:
        service = _build_service(settings)
    except ValueError as exc:
        print(f"Cannot verify channel: {exc}", file=sys.stderr)
        return 2
    try:
        channel = auth.verify_channel(
            service, expected_channel_id=args.expected_channel_id
        )
    except ValueError as exc:
        print(f"Channel verification FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI reports, never tracebacks
        print(f"Channel verification FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Channel ID: {channel['channel_id']}")
    print(f"Title: {channel['title']}")
    print(f"Subscribers: {channel['subscriber_count']}")
    return 0


def cmd_quota_status(args: argparse.Namespace) -> int:
    settings = _load_settings()
    queue = None
    if not args.local:
        try:
            queue = _load_queue(settings)
        except SystemExit:
            print(
                "(Queue unavailable — showing local in-memory usage.)",
                file=sys.stderr,
            )
    tracker = _quota_tracker(queue)
    today = pacific_today()
    print(f"Quota day (Pacific): {today.isoformat()}")
    print(f"Shared bucket used: {tracker.shared_used(today)} / 10000")
    print(f"Shared bucket remaining: {tracker.shared_remaining(today)}")
    print(f"Uploads used: {tracker.uploads_used(today)} / 100")
    print(f"Uploads remaining: {tracker.uploads_remaining(today)}")
    print(
        f"Full-pipeline videos fittable: {tracker.videos_fittable(today)}"
    )
    print(f"Seconds until Pacific-midnight reset: {seconds_until_reset()}")
    if queue is not None:
        queue.close()
    return 0


def cmd_upload_next(args: argparse.Namespace) -> int:
    settings = _load_settings()
    queue = _load_queue(settings)
    try:
        job = queue.claim_job()
    except Exception as exc:  # noqa: BLE001 — queue down = fail closed
        print(
            f"Cannot claim a job (queue unreachable?): {exc}",
            file=sys.stderr,
        )
        return 1
    if job is None:
        print("No approved jobs waiting.")
        queue.close()
        return 0
    print(f"Claimed job {job['job_id']}: {job.get('title', '')}")
    try:
        service = _build_service(settings)
    except ValueError as exc:
        print(f"Cannot build YouTube service: {exc}", file=sys.stderr)
        queue.close()
        return 2
    tracker = _quota_tracker(queue)
    try:
        video_id = run_job_pipeline(service, queue, tracker, settings, job)
    except DryRunBlockedError as exc:
        print(f"BLOCKED (dry-run): {exc}", file=sys.stderr)
        return 1
    except (KillSwitchOffError, UploadBlockedError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    except QuotaExceeded as exc:
        print(f"BLOCKED (quota): {exc}", file=sys.stderr)
        return 1
    except UploadOutcomeUnknownError as exc:
        print(
            f"OUTCOME UNKNOWN: {exc}\n"
            f"Job {job['job_id']} is in upload_outcome_unknown — "
            "reconcile it before any retry.",
            file=sys.stderr,
        )
        return 1
    except (VerificationError, ValueError, FileNotFoundError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI reports, never tracebacks
        print(f"FAILED (unexpected): {exc}", file=sys.stderr)
        return 1
    finally:
        queue.close()
    print(f"Done: job {job['job_id']} → video {video_id} (public)")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    settings = _load_settings()
    queue = _load_queue(settings)
    try:
        job = queue.get_job(args.job_id)
        if job is None:
            print(f"No such job: {args.job_id}", file=sys.stderr)
            return 2
        if job.get("status") not in (
            JobStatus.UPLOAD_OUTCOME_UNKNOWN,
            JobStatus.UPLOAD_OUTCOME_UNKNOWN.value,
        ):
            print(
                f"Job {args.job_id} is in state {job.get('status')!r}, "
                "not upload_outcome_unknown — nothing to reconcile.",
                file=sys.stderr,
            )
            return 2
        try:
            service = _build_service(settings)
        except ValueError as exc:
            print(f"Cannot build YouTube service: {exc}", file=sys.stderr)
            return 2
        try:
            result = reconcile_unknown_job(service, queue, job)
        except ReconciliationUncertainError as exc:
            print(f"UNCERTAIN: {exc}", file=sys.stderr)
            print(
                f"Job {args.job_id} stays in upload_outcome_unknown "
                "for human review.",
                file=sys.stderr,
            )
            return 1
        print(f"Reconciliation result for {args.job_id}: {result}")
        if result == "found":
            print("Video adopted; job is now uploaded_private.")
        else:
            print("No video exists; job is back to approved_for_upload.")
        return 0
    finally:
        queue.close()


def _load_job_for_dry_run(args: argparse.Namespace) -> dict[str, Any]:
    """Load a job dict for dry-run-upload.

    From ``--job-file`` (JSON) if given, else from the real queue.
    """
    if args.job_file:
        data = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "job_id" not in data:
            raise SystemExit(
                "--job-file must contain a job object with a job_id"
            )
        return data
    settings = _load_settings()
    queue = _load_queue(settings)
    try:
        job = queue.get_job(args.job_id)
    finally:
        queue.close()
    if job is None:
        raise SystemExit(f"No such job: {args.job_id}")
    return job


def cmd_dry_run_upload(args: argparse.Namespace) -> int:
    job = _load_job_for_dry_run(args)
    settings = _load_settings()
    # SIMULATION ONLY: the gates are opened so the pipeline can be
    # exercised end to end — but the service is DryRunService, whose
    # videos().insert() always raises DryRunBlockedError, so no real
    # upload can happen on this path regardless of these flags.
    sim_settings = replace(settings, dry_run=False, uploads_force_off=False)
    queue: FakeQueue = FakeQueue(uploads_enabled=True)
    queue.create_job(
        job_id=job["job_id"],
        topic=job.get("topic", ""),
        source_file=job.get("source_file", ""),
        file_sha256=job.get("file_sha256", ""),
        title=job.get("title", ""),
        description=job.get("description", ""),
        metadata=job.get("metadata") or {},
    )
    queue.transition(job["job_id"], JobStatus.APPROVED_FOR_UPLOAD)
    # Carry any saved session into the simulation. Queue rows store
    # it under "upload_session_uri"; external job JSON may use
    # "session_uri" — read both.
    saved_uri = job.get("session_uri") or job.get("upload_session_uri")
    if saved_uri:
        queue.save_session(
            job["job_id"],
            saved_uri,
            int(job.get("bytes_uploaded") or 0),
        )
    sim_job = queue.get_job(job["job_id"])
    assert sim_job is not None
    service = DryRunService()
    tracker = QuotaTracker(queue)
    print(f"DRY RUN for job {job['job_id']}: {job.get('title', '')}")
    print("Service: DryRunService (videos.insert always blocked).")
    try:
        video_id = run_job_pipeline(
            service, queue, tracker, sim_settings, sim_job
        )
    except DryRunBlockedError:
        print(
            "WOULD UPLOAD HERE: the pipeline reached videos.insert, "
            "which DryRunService blocked as designed. Gates passed: "
            "dry-run off (simulated), force-off off (simulated), "
            "kill switch ON (simulated), job approved, quota reserved, "
            "source file validated."
        )
        print(f"Calls the engine attempted: {len(service.calls)}")
        for resource, method, _kwargs in service.calls:
            print(f"  - {resource}.{method}()")
        return 0
    except (UploadBlockedError, QuotaExceeded) as exc:
        print(f"WOULD BE BLOCKED: {exc}")
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"WOULD FAIL VALIDATION: {exc}")
        return 0
    print(f"Unexpected: pipeline returned video {video_id} in dry run.")
    return 1


# --- parser ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-uploader",
        description=(
            "Sacred Knowledge YouTube API uploader — private by "
            "default, kill-switch gated. No real upload runs without "
            "the owner's explicit approval."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_channel = sub.add_parser(
        "verify-channel",
        help="Confirm which YouTube channel the credentials belong to.",
    )
    p_channel.add_argument(
        "--expected-channel-id",
        default=None,
        help="Fail if the authenticated channel differs from this ID.",
    )
    p_channel.set_defaults(func=cmd_verify_channel)

    p_quota = sub.add_parser(
        "quota-status", help="Print today's quota numbers."
    )
    p_quota.add_argument(
        "--local",
        action="store_true",
        help="Use local in-memory usage instead of the queue database.",
    )
    p_quota.set_defaults(func=cmd_quota_status)

    sub.add_parser(
        "upload-next",
        help="Claim one approved job and run the full upload pipeline.",
    ).set_defaults(func=cmd_upload_next)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="Reconcile an upload_outcome_unknown job against YouTube.",
    )
    p_reconcile.add_argument("job_id")
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_dry = sub.add_parser(
        "dry-run-upload",
        help="Simulate one job's pipeline against DryRunService.",
    )
    p_dry.add_argument("job_id")
    p_dry.add_argument(
        "--job-file",
        default=None,
        help="JSON file with the job dict (else read from the queue).",
    )
    p_dry.set_defaults(func=cmd_dry_run_upload)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
