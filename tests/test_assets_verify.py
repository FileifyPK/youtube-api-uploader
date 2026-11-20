"""Assets (captions / thumbnail / playlist / publish) and verification
against a mock service.

NOTE on the brief: ``verify_caption`` and
``verify_playlist_membership`` return booleans in the real code — they
do NOT raise. The tests below assert True/False (the CLI is what
raises ``VerificationError`` on a False). ``verify_video`` DOES raise.
"""

from pathlib import Path

import pytest

from youtube_uploader.assets import (
    MAX_THUMBNAIL_BYTES,
    add_to_playlist,
    publish_video,
    set_thumbnail,
    upload_caption,
)
from youtube_uploader.errors import VerificationError
from youtube_uploader.quota import QuotaExceeded, pacific_today
from youtube_uploader.verify import (
    verify_caption,
    verify_playlist_membership,
    verify_thumbnail,
    verify_video,
)

from conftest import FakeYouTubeService

VIDEO_ID = "VID_ASSETS1"


@pytest.fixture
def service():
    return FakeYouTubeService()


def _usage(tracker, endpoint):
    return tracker.store.get_usage(pacific_today()).get(endpoint, 0)


def _srt(tmp_path, name="captions.srt", content=b"1\n00:00:00,000 --> 00:00:02,000\nTest caption\n"):
    path = Path(tmp_path) / name
    path.write_bytes(content)
    return path


def _image(tmp_path, name="thumb.jpg", size=1024):
    path = Path(tmp_path) / name
    path.write_bytes(b"\xff" * size)
    return path


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------


def test_upload_caption_success_records_400_units(
    service, quota_tracker, tmp_path
):
    caption_id = upload_caption(
        service, quota_tracker, VIDEO_ID, _srt(tmp_path), "ur"
    )
    assert caption_id == "CAP_FAKE123"
    assert _usage(quota_tracker, "captions.insert") == 400
    assert len(service.calls) == 1
    assert service.calls[0][0] == "captions"
    assert service.calls[0][1] == "insert"
    assert service.calls[0][2]["body"]["snippet"]["videoId"] == VIDEO_ID
    assert service.calls[0][2]["body"]["snippet"]["language"] == "ur"


def test_upload_caption_missing_file_raises(service, quota_tracker, tmp_path):
    with pytest.raises(FileNotFoundError):
        upload_caption(
            service, quota_tracker, VIDEO_ID, tmp_path / "nope.srt", "ur"
        )
    assert service.calls == []


def test_upload_caption_empty_file_raises(service, quota_tracker, tmp_path):
    with pytest.raises(ValueError, match="empty"):
        upload_caption(
            service, quota_tracker, VIDEO_ID, _srt(tmp_path, content=b""), "ur"
        )
    assert service.calls == []


def test_upload_caption_wrong_suffix_raises(service, quota_tracker, tmp_path):
    path = Path(tmp_path) / "captions.txt"
    path.write_bytes(b"not an srt")
    with pytest.raises(ValueError, match=r"\.srt"):
        upload_caption(service, quota_tracker, VIDEO_ID, path, "ur")
    assert service.calls == []


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------


def test_set_thumbnail_success_records_50_units(
    service, quota_tracker, tmp_path
):
    assert set_thumbnail(service, quota_tracker, VIDEO_ID, _image(tmp_path)) is True
    assert _usage(quota_tracker, "thumbnails.set") == 50
    assert service.thumbnail_set is True


def test_set_thumbnail_wrong_extension_raises(
    service, quota_tracker, tmp_path
):
    path = Path(tmp_path) / "thumb.gif"
    path.write_bytes(b"\xff" * 100)
    with pytest.raises(ValueError, match=r"\.jpg/.jpeg/.png"):
        set_thumbnail(service, quota_tracker, VIDEO_ID, path)
    assert service.calls == []


def test_set_thumbnail_over_2mb_raises(service, quota_tracker, tmp_path):
    path = _image(tmp_path, size=MAX_THUMBNAIL_BYTES + 1)
    with pytest.raises(ValueError, match="2 MB"):
        set_thumbnail(service, quota_tracker, VIDEO_ID, path)
    assert service.calls == []


def test_set_thumbnail_empty_file_raises(service, quota_tracker, tmp_path):
    path = _image(tmp_path, size=0)
    with pytest.raises(ValueError, match="empty"):
        set_thumbnail(service, quota_tracker, VIDEO_ID, path)
    assert service.calls == []


# ---------------------------------------------------------------------------
# Playlist + publish
# ---------------------------------------------------------------------------


def test_add_to_playlist_success_records_50_units(service, quota_tracker):
    item_id = add_to_playlist(service, quota_tracker, VIDEO_ID, "PL_FAKE")
    assert item_id == "PLI_FAKE123"
    assert _usage(quota_tracker, "playlistItems.insert") == 50
    assert VIDEO_ID in service.playlist_video_ids


def test_add_to_playlist_requires_playlist_id(service, quota_tracker):
    with pytest.raises(ValueError, match="playlist_id is required"):
        add_to_playlist(service, quota_tracker, VIDEO_ID, "")
    assert service.calls == []


def test_publish_video_flips_only_privacy_to_public(service, quota_tracker):
    assert publish_video(service, quota_tracker, VIDEO_ID) is True
    assert _usage(quota_tracker, "videos.update") == 50
    assert len(service.calls) == 1
    resource, method, kwargs = service.calls[0]
    assert (resource, method) == ("videos", "update")
    assert kwargs["part"] == "status"
    # The update body changes NOTHING but privacyStatus.
    assert kwargs["body"] == {"id": VIDEO_ID, "status": {"privacyStatus": "public"}}
    assert service.video_privacy == "public"


# ---------------------------------------------------------------------------
# verify_video
# ---------------------------------------------------------------------------


def _prime_video(service, title="Test video title", description="Test description",
                 privacy="private", made_for_kids=False, synthetic=False):
    service.default_video_id = VIDEO_ID
    service.video_title = title
    service.video_description = description
    service.video_privacy = privacy
    service.made_for_kids = made_for_kids
    service.contains_synthetic_media = synthetic


def test_verify_video_matching_metadata_passes(service):
    _prime_video(service)
    video = verify_video(
        service,
        VIDEO_ID,
        {
            "title": "Test video title",
            "description": "Test description",
            "privacy_status": "private",
            "made_for_kids": False,
            "contains_synthetic_media": False,
        },
    )
    assert video["id"] == VIDEO_ID


def test_verify_video_wrong_title_raises(service):
    _prime_video(service, title="Something else entirely")
    with pytest.raises(VerificationError, match="title"):
        verify_video(
            service, VIDEO_ID,
            {"title": "Test video title", "privacy_status": "private"},
        )


def test_verify_video_wrong_privacy_raises(service):
    # Expected private (pre-publish) but the video is public.
    _prime_video(service, privacy="public")
    with pytest.raises(VerificationError, match="privacyStatus"):
        verify_video(service, VIDEO_ID, {"privacy_status": "private"})


def test_verify_video_privacy_both_directions(service):
    _prime_video(service, privacy="private")
    verify_video(service, VIDEO_ID, {"privacy_status": "private"})
    _prime_video(service, privacy="public")
    verify_video(service, VIDEO_ID, {"privacy_status": "public"})
    # And the reverse mismatch also raises.
    with pytest.raises(VerificationError, match="privacyStatus"):
        verify_video(service, VIDEO_ID, {"privacy_status": "private"})


def test_verify_video_privacy_defaults_to_private(service):
    # A caller that does not expect public must never silently pass
    # a video that went public early.
    _prime_video(service, privacy="private")
    verify_video(service, VIDEO_ID, {"title": "Test video title"})
    _prime_video(service, privacy="public")
    with pytest.raises(VerificationError, match="privacyStatus"):
        verify_video(service, VIDEO_ID, {"title": "Test video title"})


def test_verify_video_not_found_raises(service):
    service.default_video_id = VIDEO_ID
    # Empty the payload by pointing videos.list at no items: the fake
    # always returns one item, so simulate "not found" with a service
    # whose list payload is overridden.
    service.videos_list_payload = lambda: {"items": []}  # noqa: E731
    with pytest.raises(VerificationError, match="not found"):
        verify_video(service, VIDEO_ID, {"privacy_status": "private"})


# ---------------------------------------------------------------------------
# verify_caption / verify_thumbnail / verify_playlist_membership (booleans)
# ---------------------------------------------------------------------------


def test_verify_caption_present_and_absent(service):
    service.caption_items = [{"id": "CAP1"}]
    assert verify_caption(service, VIDEO_ID) is True
    service.caption_items = []
    assert verify_caption(service, VIDEO_ID) is False


def test_verify_thumbnail_custom_and_auto_only(service):
    _prime_video(service)
    assert verify_thumbnail(service, VIDEO_ID) is True
    # Only auto-generated URLs: no custom thumbnail detected.
    service.custom_thumbnail_url = (
        f"https://i.ytimg.com/vi/{VIDEO_ID}/hqdefault.jpg"
    )
    assert verify_thumbnail(service, VIDEO_ID) is False


def test_verify_playlist_membership_present_and_absent(service):
    assert verify_playlist_membership(service, VIDEO_ID, "PL_FAKE") is False
    service.playlist_video_ids.add(VIDEO_ID)
    assert verify_playlist_membership(service, VIDEO_ID, "PL_FAKE") is True


# ---------------------------------------------------------------------------
# Quota exhaustion mid-assets
# ---------------------------------------------------------------------------


def test_quota_exhaustion_mid_assets_blocks_before_service_call(
    service, quota_tracker, tmp_path
):
    # Fill the shared bucket to the 90% safety threshold (9,000/10,000).
    quota_tracker.store.record(pacific_today(), "captions.insert", 9000)
    with pytest.raises(QuotaExceeded):
        upload_caption(
            service, quota_tracker, VIDEO_ID, _srt(tmp_path), "ur"
        )
    # The reservation failed, so no API call was attempted.
    assert service.calls == []
