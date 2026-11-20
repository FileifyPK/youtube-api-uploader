"""DryRunService, CLI entry points, and the full mock pipeline.

No real YouTube calls anywhere: the CLI dry-run path uses
DryRunService (videos.insert always blocked), quota-status --local
uses in-memory usage, and the full pipeline runs against
FakeYouTubeService.
"""

import json
from pathlib import Path

import pytest

from youtube_uploader import cli
from youtube_uploader.config import Settings
from youtube_uploader.dryrun import DryRunService, assert_not_dry_run
from youtube_uploader.errors import DryRunBlockedError
from youtube_uploader.queue import FakeQueue
from youtube_uploader.quota import QuotaTracker, pacific_today
from youtube_uploader.state import JobStatus

from conftest import (
    FakeYouTubeService,
    make_approved_job,
    make_settings,
    sha256_of,
)


# ---------------------------------------------------------------------------
# DryRunService
# ---------------------------------------------------------------------------


def test_dry_run_service_insert_is_blocked():
    service = DryRunService()
    with pytest.raises(DryRunBlockedError, match="videos.insert is blocked"):
        service.videos().insert(part="snippet,status", body={}, media_body=None)
    # Even the blocked attempt is recorded.
    assert ("videos", "insert") in [(r, m) for r, m, _ in service.calls]


def test_dry_run_service_other_calls_recorded_and_canned():
    service = DryRunService()
    channel = service.channels().list(part="snippet", mine=True).execute()
    assert channel["items"][0]["id"] == "DRY_RUN_CHANNEL_ID"
    caption = service.captions().insert(part="snippet", body={}).execute()
    assert caption["id"] == "DRY_RUN_CAPTION_ID"
    item = service.playlistItems().insert(part="snippet", body={}).execute()
    assert item["id"] == "DRY_RUN_PLAYLIST_ITEM_ID"
    assert service.videos().list(part="snippet", id="x").execute() == {"items": []}
    assert service.search().list(q="x").execute() == {"items": []}
    methods = [(r, m) for r, m, _ in service.calls]
    assert ("channels", "list") in methods
    assert ("captions", "insert") in methods
    assert ("playlistItems", "insert") in methods
    assert ("videos", "list") in methods
    assert ("search", "list") in methods


def test_assert_not_dry_run():
    with pytest.raises(DryRunBlockedError):
        assert_not_dry_run(Settings(dry_run=True))
    assert_not_dry_run(Settings(dry_run=False))  # must not raise


# ---------------------------------------------------------------------------
# CLI: dry-run-upload
# ---------------------------------------------------------------------------


def test_cli_dry_run_upload(tmp_path, capsys):
    video = tmp_path / "dryrun-video.mp4"
    video.write_bytes(b"FAKE VIDEO BYTES for dry run " * 32)
    job_data = {
        "job_id": "job-dryrun-001",
        "topic": "Dry run topic",
        "title": "Dry run title",
        "description": "Dry run description",
        "source_file": str(video),
        "file_sha256": sha256_of(video),
        "metadata": {
            "assessment": {
                "made_for_kids": False,
                "contains_synthetic_media": False,
            },
            "language": "ur",
        },
    }
    job_file = tmp_path / "job.json"
    job_file.write_text(json.dumps(job_data), encoding="utf-8")

    exit_code = cli.main(
        ["dry-run-upload", "job-dryrun-001", "--job-file", str(job_file)]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "WOULD UPLOAD HERE" in out
    assert "videos.insert()" in out
    # The engine attempted exactly ONE call: the blocked videos.insert.
    assert "Calls the engine attempted: 1" in out


def test_cli_dry_run_upload_rejects_bad_job_file(tmp_path, capsys):
    job_file = tmp_path / "bad-job.json"
    job_file.write_text(json.dumps({"no_job_id": True}), encoding="utf-8")
    with pytest.raises(SystemExit, match="job_id"):
        cli.main(["dry-run-upload", "x", "--job-file", str(job_file)])


# ---------------------------------------------------------------------------
# CLI: quota-status --local
# ---------------------------------------------------------------------------


def test_cli_quota_status_local_prints_numbers_no_secrets(
    capsys, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "FAKE_DATABASE_URL_VALUE_12345")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "FAKE_CLIENT_SECRET_VALUE_67890")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "FAKE_REFRESH_TOKEN_VALUE_abc")
    exit_code = cli.main(["quota-status", "--local"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Quota day (Pacific):" in out
    assert "Shared bucket used: 0 / 10000" in out
    assert "Shared bucket remaining: 10000" in out
    assert "Uploads used: 0 / 100" in out
    assert "Uploads remaining: 100" in out
    assert "Full-pipeline videos fittable: 20" in out
    assert "Seconds until Pacific-midnight reset:" in out
    assert "FAKE_DATABASE_URL_VALUE_12345" not in out
    assert "FAKE_CLIENT_SECRET_VALUE_67890" not in out
    assert "FAKE_REFRESH_TOKEN_VALUE_abc" not in out


# ---------------------------------------------------------------------------
# Full mock pipeline (cli.run_job_pipeline)
# ---------------------------------------------------------------------------


def test_full_mock_pipeline_publishes_public_after_assets_verified(
    tmp_path, no_backoff
):
    events: list = []
    service = FakeYouTubeService(event_sink=events)
    service.default_video_id = "VID_PIPELINE1"
    queue = FakeQueue(agent_id="muse", uploads_enabled=True)

    # Spy on transitions so call order and transition order share one timeline.
    orig_transition = queue.transition

    def spy_transition(job_id, to_state, video_id=None, error=None):
        result = orig_transition(
            job_id, to_state, video_id=video_id, error=error
        )
        events.append(("transition", to_state.value))
        return result

    queue.transition = spy_transition

    caption = tmp_path / "captions.srt"
    caption.write_bytes(b"1\n00:00:00,000 --> 00:00:02,000\nTest\n")
    thumbnail = tmp_path / "thumb.jpg"
    thumbnail.write_bytes(b"\xff" * 512)

    job, _ = make_approved_job(
        queue,
        tmp_path,
        "job-pipeline-001",
        caption_file=str(caption),
        thumbnail_file=str(thumbnail),
        playlist_id="PL_FAKE_TEST",
    )
    # The fake channel serves the job's own metadata back.
    service.video_title = job["title"]
    service.video_description = job["description"]
    service.made_for_kids = False
    service.contains_synthetic_media = False

    tracker = QuotaTracker(queue)
    settings = make_settings(agent_id="muse")
    video_id = cli.run_job_pipeline(
        service, queue, tracker, settings, queue.get_job("job-pipeline-001")
    )

    assert video_id == "VID_PIPELINE1"
    assert queue.get_job("job-pipeline-001")["status"] == JobStatus.PUBLISHED.value
    assert queue.get_job("job-pipeline-001")["video_id"] == "VID_PIPELINE1"
    assert service.video_privacy == "public"

    # Exact quota accounting for one full-pipeline video.
    assert queue.get_usage(pacific_today()) == {
        "videos.insert": 1,
        "captions.insert": 400,
        "thumbnails.set": 50,
        "playlistItems.insert": 50,
        "videos.update": 50,
    }

    # All three assets recorded and verified.
    assets = {
        (a["asset_type"], a["verified"]) for a in queue.assets
    }
    assert assets == {
        ("caption", True),
        ("thumbnail", True),
        ("playlist", True),
    }

    # The privacy flip to public happened ONLY after assets_verified —
    # and after every asset call.
    verified_idx = events.index(("transition", "assets_verified"))
    publish_idx = events.index(("call", "videos.update"))
    assert verified_idx < publish_idx
    for call in (
        ("call", "captions.insert"),
        ("call", "thumbnails.set"),
        ("call", "playlistItems.insert"),
    ):
        assert events.index(call) < publish_idx
    # And the publish happened before the final published transition.
    assert publish_idx < events.index(("transition", "published"))
