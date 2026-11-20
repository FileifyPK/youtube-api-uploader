"""NotionSync tests with a fake client implementing the 4-method
NotionClient protocol. No live Notion calls, no Notion SDK.
"""

from datetime import datetime, timezone

from youtube_uploader.notion_sync import (
    UPLOADED_STATUS,
    NotionSync,
    adapt_job_dict,
    build_upload_notes,
    build_video_url,
)

VIDEO_ID = "VID_NOTION1"
VIDEO_URL = build_video_url(VIDEO_ID)


class FakeNotionClient:
    """In-memory stand-in for the Notion API helper."""

    def __init__(self, pq_rows=None):
        # NB: `pq_rows or {...}` would be wrong here — an explicitly
        # empty dict (no rows at all) is falsy and must stay empty.
        self.pq_rows = dict(
            pq_rows if pq_rows is not None else {"Test topic": "pq-row-1"}
        )
        self.updated_rows: list[tuple[str, str, str]] = []
        self.uploaded_rows: list[dict] = []
        self.recorded_urls: set[str] = set()

    def find_production_queue_row(self, topic_title):
        return self.pq_rows.get(topic_title)

    def update_row_status(self, row_id, status, notes=""):
        self.updated_rows.append((row_id, status, notes))

    def add_uploaded_row(self, title, video_url, language, duration, playlist):
        self.recorded_urls.add(video_url)
        row_id = f"uploaded-row-{len(self.uploaded_rows) + 1}"
        self.uploaded_rows.append(
            {
                "row_id": row_id,
                "title": title,
                "video_url": video_url,
                "language": language,
                "duration": duration,
                "playlist": playlist,
            }
        )
        return row_id

    def row_exists(self, video_url):
        return video_url in self.recorded_urls


def _queue_job(**overrides):
    job = {
        "job_id": "job-notion-001",
        "topic": "Test topic",
        "title": "Test video title",
        "description": "Test description",
        "metadata": {
            "language": "Urdu",
            "duration": "18:11",
            "playlist_id": "PL_FAKE",
        },
        "claimed_by": "muse",
        "agent_id": "muse",
    }
    job.update(overrides)
    return job


def test_flip_after_upload_flips_pq_row_and_adds_uploaded_row():
    client = FakeNotionClient()
    sync = NotionSync(client)
    result = sync.flip_after_upload(_queue_job(), VIDEO_ID, VIDEO_URL)
    assert result["flipped"] is True
    assert result["production_queue_row_id"] == "pq-row-1"
    assert result["uploaded_row_id"] == "uploaded-row-1"
    assert result["video_id"] == VIDEO_ID
    assert result["video_url"] == VIDEO_URL
    # The PQ row was flipped to the uploaded status with notes.
    assert len(client.updated_rows) == 1
    row_id, status, notes = client.updated_rows[0]
    assert row_id == "pq-row-1"
    assert status == UPLOADED_STATUS == "uploaded"
    assert VIDEO_ID in notes
    assert VIDEO_URL in notes
    # The ✅ Uploaded row carries the video's own metadata.
    assert len(client.uploaded_rows) == 1
    uploaded = client.uploaded_rows[0]
    assert uploaded["title"] == "Test video title"
    assert uploaded["video_url"] == VIDEO_URL
    assert uploaded["language"] == "Urdu"
    assert uploaded["duration"] == "18:11"
    assert uploaded["playlist"] == "PL_FAKE"


def test_flip_is_idempotent_second_flip_records_nothing():
    client = FakeNotionClient()
    sync = NotionSync(client)
    first = sync.flip_after_upload(_queue_job(), VIDEO_ID, VIDEO_URL)
    assert first["flipped"] is True
    second = sync.flip_after_upload(_queue_job(), VIDEO_ID, VIDEO_URL)
    assert second["flipped"] is False
    assert second["reason"] == "already recorded"
    # add_uploaded_row ran exactly once in total.
    assert len(client.uploaded_rows) == 1
    assert len(client.updated_rows) == 1


def test_missing_pq_row_still_records_uploaded_row():
    client = FakeNotionClient(pq_rows={})  # no Production Queue row
    sync = NotionSync(client)
    result = sync.flip_after_upload(_queue_job(), VIDEO_ID, VIDEO_URL)
    assert result["flipped"] is False
    assert result["reason"] == "production_queue_row_not_found"
    assert result["uploaded_row_id"] == "uploaded-row-1"
    # The 🎬 page is the authority against duplicate uploads, so its
    # row is recorded even when the PQ row is missing.
    assert len(client.uploaded_rows) == 1
    assert client.updated_rows == []


def test_adapt_job_dict_maps_queue_job_to_upload_job_view():
    view = adapt_job_dict(_queue_job())
    assert view.topic_title == "Test topic"
    assert view.title == "Test video title"
    assert view.language == "Urdu"
    assert view.duration == "18:11"
    assert view.playlist == "PL_FAKE"
    assert view.agent_id == "muse"


def test_adapt_job_dict_prefers_playlist_name_and_agent_id_fallback():
    job = _queue_job(
        metadata={
            "language": "English",
            "duration": "05:00",
            "playlist_name": "Hadith Studies",
            "playlist_id": "PL_FAKE",
        },
        claimed_by=None,
    )
    view = adapt_job_dict(job)
    assert view.playlist == "Hadith Studies"
    assert view.agent_id == "muse"  # falls back to agent_id


def test_flip_works_when_passed_a_raw_dict():
    client = FakeNotionClient()
    sync = NotionSync(client)
    result = sync.flip_after_upload(_queue_job(), VIDEO_ID, VIDEO_URL)
    assert result["flipped"] is True
    assert result["production_queue_row_id"] == "pq-row-1"


def test_build_upload_notes_contains_video_and_agent_no_secrets():
    job = _queue_job(
        metadata={
            "language": "Urdu",
            "duration": "18:11",
            "playlist_id": "PL_FAKE",
            # A fake token string sitting in metadata must NEVER leak
            # into tracker notes.
            "note": "FAKE_TOKEN_VALUE_XYZ_12345",
        }
    )
    now = datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc)
    notes = build_upload_notes(job, VIDEO_ID, now=now)
    assert VIDEO_ID in notes
    assert VIDEO_URL in notes
    assert "muse" in notes
    assert "2026-11-20 17:00 UTC" in notes
    assert "FAKE_TOKEN_VALUE_XYZ_12345" not in notes
    # The NotionSync static wrapper builds the same notes.
    assert NotionSync.build_upload_notes(job, VIDEO_ID, now=now) == notes


def test_build_video_url():
    assert build_video_url("abc123") == "https://www.youtube.com/watch?v=abc123"
