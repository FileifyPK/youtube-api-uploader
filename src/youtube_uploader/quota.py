"""Quota accounting — two independent buckets (Plan v2, Phase B/C).

Video uploads have their OWN bucket: 100 videos.insert calls per
project per day at 1 unit each. Everything else shares the 10,000
units/day bucket:

- captions.insert:        400 units
- thumbnails.set:          50 units
- playlistItems.insert:    50 units
- videos.update:           50 units

Full pipeline per video with playlist: 500 units -> 20 videos/day.
Without playlist insertion: 450 units -> ~22 videos/day.

Quota resets at midnight PACIFIC Time (America/Los_Angeles), not
Pakistan local midnight. All accounting uses the Pacific calendar day.

The tracker is storage-agnostic: it works against any object with
``get_usage(day)`` / ``record(day, endpoint, units)`` semantics (the
PostgreSQL quota_usage table in production, an in-memory store in
tests). It never talks to YouTube itself.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# Shared-bucket costs (units per call), per Google's quota calculator.
QUOTA_COSTS: dict[str, int] = {
    "captions.insert": 400,
    "thumbnails.set": 50,
    "playlistItems.insert": 50,
    "videos.update": 50,
    # Read-only verification calls are cheap; charged at their real cost.
    "videos.list": 1,
    "captions.list": 50,
    "playlistItems.list": 1,
    "channels.list": 1,
    "search.list": 100,
}

# videos.insert lives in its own bucket: 1 unit per call, 100/day.
VIDEO_UPLOAD_BUCKET_LIMIT = 100
VIDEO_UPLOAD_COST = 1

SHARED_BUCKET_LIMIT = 10_000

# Full-pipeline cost of one video (caption + thumbnail + playlist).
FULL_PIPELINE_COST = (
    QUOTA_COSTS["captions.insert"]
    + QUOTA_COSTS["thumbnails.set"]
    + QUOTA_COSTS["playlistItems.insert"]
)  # = 500

# Safety threshold: pause new uploads once this fraction of the shared
# bucket is consumed (default 90%).
DEFAULT_SAFETY_FRACTION = 0.90


def pacific_today(now: datetime | None = None) -> date:
    """Current calendar day in Pacific Time (quota reset boundary)."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(PACIFIC).date()


def pacific_day_start(day: date) -> datetime:
    """Midnight at the start of ``day`` in Pacific Time, as UTC."""
    return datetime.combine(day, time.min, tzinfo=PACIFIC).astimezone(
        timezone.utc
    )


def seconds_until_reset(now: datetime | None = None) -> int:
    """Seconds until the next Pacific-midnight quota reset."""
    now = now or datetime.now(timezone.utc)
    today = pacific_today(now)
    reset = pacific_day_start(today + timedelta(days=1))
    return int((reset - now).total_seconds())


class QuotaExceeded(Exception):
    """Raised when a reservation would exceed the quota budget."""


class QuotaStore:
    """Minimal storage protocol for quota usage.

    Production implementation: the ``quota_usage`` table via QueueClient.
    Test implementation: :class:`InMemoryQuotaStore`.
    """

    def get_usage(self, day: date) -> dict[str, int]:
        """Return {endpoint: units_used} for the Pacific day."""
        raise NotImplementedError

    def record(self, day: date, endpoint: str, units: int) -> None:
        """Add ``units`` of usage for ``endpoint`` on ``day``."""
        raise NotImplementedError


class InMemoryQuotaStore(QuotaStore):
    """Thread-safe in-memory store for tests and dry-run."""

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._usage: dict[date, dict[str, int]] = {}

    def get_usage(self, day: date) -> dict[str, int]:
        with self._lock:
            return dict(self._usage.get(day, {}))

    def record(self, day: date, endpoint: str, units: int) -> None:
        with self._lock:
            bucket = self._usage.setdefault(day, {})
            bucket[endpoint] = bucket.get(endpoint, 0) + units


class QuotaTracker:
    """Reservation-based quota accounting.

    Callers reserve units BEFORE making an API call and record actual
    usage afterwards. A reservation that would exceed the safety
    threshold raises :class:`QuotaExceeded` and the caller must pause
    (never blind-retry after a quota error).
    """

    def __init__(
        self,
        store: QuotaStore,
        safety_fraction: float = DEFAULT_SAFETY_FRACTION,
    ) -> None:
        self.store = store
        self.safety_fraction = safety_fraction

    def shared_used(self, day: date | None = None) -> int:
        day = day or pacific_today()
        usage = self.store.get_usage(day)
        return sum(
            units
            for endpoint, units in usage.items()
            if endpoint != "videos.insert"
        )

    def uploads_used(self, day: date | None = None) -> int:
        day = day or pacific_today()
        return self.store.get_usage(day).get("videos.insert", 0)

    def shared_remaining(self, day: date | None = None) -> int:
        return SHARED_BUCKET_LIMIT - self.shared_used(day)

    def uploads_remaining(self, day: date | None = None) -> int:
        return VIDEO_UPLOAD_BUCKET_LIMIT - self.uploads_used(day)

    def videos_fittable(self, day: date | None = None) -> int:
        """How many more full-pipeline videos fit in today's budget."""
        by_shared = self.shared_remaining(day) // FULL_PIPELINE_COST
        return max(0, min(by_shared, self.uploads_remaining(day)))

    def reserve(self, endpoint: str, day: date | None = None) -> int:
        """Reserve units for one call; raise QuotaExceeded if unsafe.

        Returns the cost reserved. The caller must call ``record`` after
        the API call completes (success or quota-consuming failure).
        """
        day = day or pacific_today()
        if endpoint == "videos.insert":
            if self.uploads_used(day) + VIDEO_UPLOAD_COST > VIDEO_UPLOAD_BUCKET_LIMIT:
                raise QuotaExceeded(
                    f"videos.insert daily limit reached ({VIDEO_UPLOAD_BUCKET_LIMIT}/day)"
                )
            return VIDEO_UPLOAD_COST
        cost = QUOTA_COSTS[endpoint]
        limit = int(SHARED_BUCKET_LIMIT * self.safety_fraction)
        if self.shared_used(day) + cost > limit:
            raise QuotaExceeded(
                f"Shared quota safety threshold reached: "
                f"{self.shared_used(day)} + {cost} > {limit} units"
            )
        return cost

    def record(self, endpoint: str, day: date | None = None) -> None:
        day = day or pacific_today()
        cost = (
            VIDEO_UPLOAD_COST
            if endpoint == "videos.insert"
            else QUOTA_COSTS[endpoint]
        )
        self.store.record(day, endpoint, cost)
