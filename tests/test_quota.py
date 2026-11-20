"""Quota accounting tests: two buckets, Pacific reset, safety threshold."""

from datetime import date, datetime, timezone

import pytest

from youtube_uploader.quota import (
    DEFAULT_SAFETY_FRACTION,
    FULL_PIPELINE_COST,
    QUOTA_COSTS,
    SHARED_BUCKET_LIMIT,
    VIDEO_UPLOAD_BUCKET_LIMIT,
    InMemoryQuotaStore,
    QuotaExceeded,
    QuotaTracker,
    pacific_day_start,
    pacific_today,
    seconds_until_reset,
)

DAY = date(2026, 11, 20)


@pytest.fixture
def tracker():
    return QuotaTracker(InMemoryQuotaStore())


def test_quota_costs():
    assert QUOTA_COSTS["captions.insert"] == 400
    assert QUOTA_COSTS["thumbnails.set"] == 50
    assert QUOTA_COSTS["playlistItems.insert"] == 50
    assert QUOTA_COSTS["videos.update"] == 50
    assert VIDEO_UPLOAD_BUCKET_LIMIT == 100
    assert SHARED_BUCKET_LIMIT == 10_000
    assert DEFAULT_SAFETY_FRACTION == 0.90


def test_full_pipeline_cost():
    # caption + thumbnail + playlist = 400 + 50 + 50 = 500/video,
    # i.e. 20 full-pipeline videos per day on the shared bucket.
    assert FULL_PIPELINE_COST == 500
    assert SHARED_BUCKET_LIMIT // FULL_PIPELINE_COST == 20


def test_pacific_today_boundary():
    # 07:00 UTC on 20 Nov is still 23:00 on 19 Nov in Pacific (PST).
    assert pacific_today(datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc)) == date(
        2026, 11, 19
    )
    # 08:00 UTC is midnight Pacific: the new quota day starts.
    assert pacific_today(datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)) == date(
        2026, 11, 20
    )
    # Midday UTC is early morning Pacific, same calendar day.
    assert pacific_today(datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)) == date(
        2026, 11, 20
    )


def test_seconds_until_reset_in_range():
    # Exactly one hour before the Pacific-midnight reset.
    assert (
        seconds_until_reset(datetime(2026, 11, 20, 7, 0, tzinfo=timezone.utc))
        == 3600
    )
    for hour in (0, 5, 12, 18, 23):
        seconds = seconds_until_reset(
            datetime(2026, 11, 20, hour, 0, tzinfo=timezone.utc)
        )
        assert 0 < seconds <= 86400
    assert 0 < seconds_until_reset() <= 86400


def test_pacific_day_start_is_utc_midnight_pacific():
    # 20 Nov 00:00 Pacific (PST, UTC-8) is 08:00 UTC.
    assert pacific_day_start(date(2026, 11, 20)) == datetime(
        2026, 11, 20, 8, 0, tzinfo=timezone.utc
    )


def test_reserve_record_accounting(tracker):
    assert tracker.reserve("captions.insert", DAY) == 400
    tracker.record("captions.insert", DAY)
    assert tracker.shared_used(DAY) == 400
    assert tracker.shared_remaining(DAY) == 9600
    assert tracker.uploads_used(DAY) == 0
    assert tracker.uploads_remaining(DAY) == 100
    # 9600 shared units left -> 19 more full-pipeline videos fit.
    assert tracker.videos_fittable(DAY) == 19


def test_videos_fittable_full_budget(tracker):
    assert tracker.videos_fittable(DAY) == 20


def test_videos_fittable_zero_when_shared_exhausted(tracker):
    tracker.store.record(DAY, "captions.insert", 10_000)
    assert tracker.videos_fittable(DAY) == 0
    assert tracker.shared_remaining(DAY) == 0


def test_videos_fittable_zero_when_uploads_exhausted(tracker):
    tracker.store.record(DAY, "videos.insert", 100)
    assert tracker.videos_fittable(DAY) == 0
    assert tracker.uploads_remaining(DAY) == 0


def test_reserve_raises_at_ninety_percent_threshold(tracker):
    # 90% of 10,000 = 9,000 units. At exactly 9,000 used, one more
    # 400-unit caption would exceed the threshold.
    tracker.store.record(DAY, "captions.insert", 9000)
    with pytest.raises(QuotaExceeded):
        tracker.reserve("captions.insert", DAY)


def test_reserve_allowed_exactly_at_threshold(tracker):
    # 8,600 + 400 = 9,000: exactly at the threshold, NOT over it.
    tracker.store.record(DAY, "captions.insert", 8600)
    assert tracker.reserve("captions.insert", DAY) == 400


def test_reserve_raises_at_100_videos_insert(tracker):
    tracker.store.record(DAY, "videos.insert", 100)
    with pytest.raises(QuotaExceeded):
        tracker.reserve("videos.insert", DAY)


def test_reserve_allowed_at_99_videos_insert(tracker):
    tracker.store.record(DAY, "videos.insert", 99)
    assert tracker.reserve("videos.insert", DAY) == 1


def test_videos_insert_tracked_separately_from_shared_bucket(tracker):
    tracker.store.record(DAY, "videos.insert", 50)
    assert tracker.uploads_used(DAY) == 50
    assert tracker.uploads_remaining(DAY) == 50
    # Uploads do not touch the shared bucket.
    assert tracker.shared_used(DAY) == 0
    assert tracker.shared_remaining(DAY) == 10_000
    tracker.store.record(DAY, "captions.insert", 400)
    assert tracker.shared_used(DAY) == 400
    assert tracker.uploads_used(DAY) == 50


def test_record_charges_real_costs(tracker):
    tracker.record("videos.insert", DAY)
    tracker.record("captions.insert", DAY)
    tracker.record("thumbnails.set", DAY)
    tracker.record("playlistItems.insert", DAY)
    tracker.record("videos.update", DAY)
    usage = tracker.store.get_usage(DAY)
    assert usage == {
        "videos.insert": 1,
        "captions.insert": 400,
        "thumbnails.set": 50,
        "playlistItems.insert": 50,
        "videos.update": 50,
    }
