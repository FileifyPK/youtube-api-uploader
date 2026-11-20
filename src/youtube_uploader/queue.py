"""Central job queue — Neon PostgreSQL client plus an in-memory fake.

Plan v2, Phase C. Two implementations share ONE interface, defined once
by :class:`BaseQueue`:

    uploads_enabled() -> bool
    set_uploads_enabled(enabled: bool) -> None
    create_job(*, job_id, topic, source_file, file_sha256, title,
               description, metadata) -> dict
    claim_job(lease_seconds=3600) -> dict | None
    get_job(job_id) -> dict | None
    transition(job_id, to_state, video_id=None, error=None) -> dict
    heartbeat(job_id, lease_seconds=3600) -> None
    release_expired_leases() -> int
    save_session(job_id, session_uri, bytes_uploaded) -> None
    record_attempt(job_id, attempt_no, outcome, error=None,
                   video_id=None) -> None
    record_asset(job_id, asset_type, asset_id, verified) -> None
    audit(job_id, agent_id, action, detail="") -> None
    register_agent(agent_id, display_name) -> None
    get_usage(day) -> dict[str, int]          (QuotaStore protocol)
    record(day, endpoint, units) -> None      (QuotaStore protocol)
    close() -> None

:class:`QueueClient` talks to Neon PostgreSQL via psycopg (v3). It uses
a ``psycopg_pool`` connection pool when that package is installed, and
otherwise a single lazily-opened connection with reconnect-on-failure.
All SQL is parameterized; values are never string-formatted into queries.

:class:`FakeQueue` is a thread-safe in-memory implementation of the same
interface for tests and dry-run, with no database at all. It enforces
the same rules: fail-closed kill switch (default OFF), atomic claim,
transition validation via ``state.validate_transition``, and the
QuotaStore protocol.

Safety rules both implementations follow:
- Kill switch FAILS CLOSED: :meth:`uploads_enabled` returns False on ANY
  error (unreachable DB, missing row, missing table) and never raises.
  An unreachable queue means NO uploads, never "assume ON".
- The DSN is never logged. Neither is any secret.
- ``upload_outcome_unknown`` is a real state, not an error string: a job
  lands there when YouTube may have accepted an upload whose response
  was lost. It must be reconciled before any retry.
"""

from __future__ import annotations

import abc
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from youtube_uploader.quota import InMemoryQuotaStore, QuotaStore
from youtube_uploader.state import JobStatus, validate_transition

log = logging.getLogger(__name__)

try:  # psycopg-pool is optional; fall back to a single connection.
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - depends on installed packages
    ConnectionPool = None  # type: ignore[assignment]

KILL_SWITCH_KEY = "global_upload_enabled"
ASSET_TYPES = frozenset({"caption", "thumbnail", "playlist"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_T = TypeVar("_T")


def _parse_bool(value: str | None) -> bool:
    """Parse a system_settings value; anything unrecognised is False."""
    if value is None:
        return False
    return value.strip().lower() in _TRUE_VALUES


class BaseQueue(QuotaStore, abc.ABC):
    """Shared queue interface. Both QueueClient and FakeQueue implement it.

    Job rows are returned as plain dicts with the ``jobs`` table columns
    as keys (``job_id``, ``topic``, ``agent_id``, ``source_file``,
    ``file_sha256``, ``title``, ``description``, ``metadata``, ``status``,
    ``video_id``, ``upload_session_uri``, ``bytes_uploaded``,
    ``claimed_by``, ``lease_expires_at``, ``attempt_count``,
    ``last_error``, ``created_at``, ``updated_at``). ``status`` is the
    plain string value (e.g. ``"uploading"``); compare with
    ``JobStatus(row["status"])`` or ``JobStatus.UPLOADING.value``.
    """

    agent_id: str

    @abc.abstractmethod
    def uploads_enabled(self) -> bool:
        """True only if the kill switch row says so. Never raises:
        ANY error (unreachable DB, missing row/table) returns False."""
        raise NotImplementedError

    @abc.abstractmethod
    def set_uploads_enabled(self, enabled: bool) -> None:
        """Set the kill switch. ADMIN ONLY — call only on the owner's
        explicit instruction, and never while his stop order stands."""
        raise NotImplementedError

    @abc.abstractmethod
    def create_job(
        self,
        *,
        job_id: str,
        topic: str,
        source_file: str,
        file_sha256: str,
        title: str,
        description: str,
        metadata: dict,
    ) -> dict:
        """Insert a job in status ``queued``; return the row as a dict.

        Raises ValueError on a duplicate ``job_id`` or on a live
        duplicate ``file_sha256`` (another non-terminal job for the
        same file already exists)."""
        raise NotImplementedError

    @abc.abstractmethod
    def claim_job(self, lease_seconds: int = 3600) -> dict | None:
        """Atomically claim the oldest ``approved_for_upload`` job with
        no live lease. Sets status ``uploading``, ``claimed_by`` to this
        agent, extends the lease, bumps ``attempt_count``. Returns the
        claimed row, or None if no job is claimable."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_job(self, job_id: str) -> dict | None:
        """Return the job row as a dict, or None if it does not exist."""
        raise NotImplementedError

    @abc.abstractmethod
    def transition(
        self,
        job_id: str,
        to_state: JobStatus,
        video_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        """Move a job to ``to_state`` (row-locked, validated via
        ``state.validate_transition``). Updates ``video_id`` /
        ``last_error`` when given, clears the lease when leaving
        ``uploading``, and writes an audit_log row. Returns the updated
        row. Raises ValueError on an illegal transition or unknown job."""
        raise NotImplementedError

    @abc.abstractmethod
    def heartbeat(self, job_id: str, lease_seconds: int = 3600) -> None:
        """Extend the lease of a job claimed by this agent. No-op (with
        a warning log) if the job is not ours or is not uploading."""
        raise NotImplementedError

    @abc.abstractmethod
    def release_expired_leases(self) -> int:
        """Return ``uploading`` jobs with expired leases to
        ``approved_for_upload``. Returns the number released."""
        raise NotImplementedError

    @abc.abstractmethod
    def save_session(
        self, job_id: str, session_uri: str, bytes_uploaded: int
    ) -> None:
        """Persist a resumable-upload session URI + uploaded byte count."""
        raise NotImplementedError

    @abc.abstractmethod
    def record_attempt(
        self,
        job_id: str,
        attempt_no: int,
        outcome: str,
        error: str | None = None,
        video_id: str | None = None,
    ) -> None:
        """Append an upload_attempts row (finished now)."""
        raise NotImplementedError

    @abc.abstractmethod
    def record_asset(
        self, job_id: str, asset_type: str, asset_id: str, verified: bool
    ) -> None:
        """Upsert an upload_assets row. ``asset_type`` must be one of
        ``caption`` / ``thumbnail`` / ``playlist``."""
        raise NotImplementedError

    @abc.abstractmethod
    def audit(
        self,
        job_id: str | None,
        agent_id: str,
        action: str,
        detail: str = "",
    ) -> None:
        """Append an audit_log row. ``job_id`` may be None for
        system-level actions."""
        raise NotImplementedError

    @abc.abstractmethod
    def register_agent(self, agent_id: str, display_name: str) -> None:
        """Register (or refresh last_seen for) an agent."""
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        """Release connections. Safe to call more than once."""
        raise NotImplementedError


class QueueClient(BaseQueue):
    """PostgreSQL queue client (Neon).

    Connection handling: a ``psycopg_pool`` pool (min 1, max 5) when
    that package is installed, otherwise a single lazily-opened
    connection with one reconnect-and-retry on operational failure.
    The single-connection path serializes access with a lock because a
    psycopg connection is not safe for concurrent use from threads.

    The DSN is stored only to (re)connect and is NEVER logged.
    """

    def __init__(self, dsn: str, agent_id: str) -> None:
        self._dsn = dsn
        self.agent_id = agent_id
        self._lock = threading.Lock()
        self._conn: psycopg.Connection | None = None
        self._pool: Any | None = None
        self._pool_opened = False
        self._pool_lock = threading.Lock()
        if ConnectionPool is not None:
            # open=False: connect lazily on first use, so constructing
            # the client never touches the network.
            self._pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=5,
                kwargs={"row_factory": dict_row},
                open=False,
            )
        log.debug("QueueClient created for agent_id=%r", agent_id)

    # -- connection plumbing ------------------------------------------------

    def _ensure_pool_open(self) -> None:
        if self._pool is None or self._pool_opened:
            return
        with self._pool_lock:
            if not self._pool_opened:
                self._pool.open()
                self._pool_opened = True

    def _ensure_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
        return self._conn

    def _drop_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _with_conn(self, fn: Callable[[psycopg.Connection], _T]) -> _T:
        """Run ``fn(conn)`` in one transaction and commit.

        Pool path: check out a connection, commit on success, roll back
        on error. Single-connection path: retry once after an
        operational failure (reconnect first); a failure during commit
        itself is NOT retried, because the commit may have landed.
        """
        if self._pool is not None:
            self._ensure_pool_open()
            with self._pool.connection() as conn:
                try:
                    result = fn(conn)
                except Exception:
                    conn.rollback()
                    raise
                conn.commit()
                return result

        with self._lock:
            for attempt in range(2):
                conn = self._ensure_conn()
                try:
                    result = fn(conn)
                except psycopg.OperationalError:
                    # Connection died mid-work; the transaction is dead
                    # with it. Drop it and try once on a fresh one.
                    self._drop_conn()
                    if attempt == 1:
                        raise
                    log.warning(
                        "Queue database connection lost; reconnecting once"
                    )
                    continue
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        self._drop_conn()
                    raise
                try:
                    conn.commit()
                except psycopg.OperationalError:
                    # Ambiguous: the commit may have landed server-side.
                    # Do NOT retry the work; surface the error.
                    self._drop_conn()
                    raise
                return result
            raise AssertionError("unreachable")  # pragma: no cover

    # -- kill switch (fail closed) ------------------------------------------

    def uploads_enabled(self) -> bool:
        """True only if system_settings says so.

        FAIL CLOSED: ANY exception — unreachable database, missing row,
        missing table — returns False and logs a warning. Never raises.
        """
        try:

            def _read(conn: psycopg.Connection) -> str | None:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT value FROM system_settings WHERE key = %s",
                        (KILL_SWITCH_KEY,),
                    )
                    row = cur.fetchone()
                    return row["value"] if row else None

            return _parse_bool(self._with_conn(_read))
        except Exception as exc:
            # Deliberately vague: never let a connection error leak the
            # DSN or host details into logs.
            log.warning(
                "Kill-switch check failed (%s); treating uploads as OFF",
                type(exc).__name__,
            )
            return False

    def set_uploads_enabled(self, enabled: bool) -> None:
        """Set the global kill switch.

        ADMIN ONLY. Call only on the owner's explicit instruction, and
        never while his global stop order stands. There is no other
        legitimate caller: the engine worker reads the switch, it never
        sets it.
        """

        def _write(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO system_settings (key, value, updated_at)
                       VALUES (%s, %s, now())
                       ON CONFLICT (key) DO UPDATE
                         SET value = EXCLUDED.value, updated_at = now()""",
                    (KILL_SWITCH_KEY, "true" if enabled else "false"),
                )

        self._with_conn(_write)
        log.warning(
            "Kill switch set to %s by agent_id=%r",
            "ON" if enabled else "OFF",
            self.agent_id,
        )

    # -- jobs ----------------------------------------------------------------

    def create_job(
        self,
        *,
        job_id: str,
        topic: str,
        source_file: str,
        file_sha256: str,
        title: str,
        description: str,
        metadata: dict,
    ) -> dict:
        def _insert(conn: psycopg.Connection) -> dict:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jobs
                           (job_id, topic, agent_id, source_file, file_sha256,
                            title, description, metadata, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued')
                       RETURNING *""",
                    (
                        job_id,
                        topic,
                        self.agent_id,
                        source_file,
                        file_sha256,
                        title,
                        description,
                        Json(metadata or {}),
                    ),
                )
                row = cur.fetchone()
                assert row is not None
                return dict(row)

        try:
            return self._with_conn(_insert)
        except psycopg.errors.UniqueViolation as exc:
            constraint = ""
            try:
                constraint = exc.diag.constraint_name or ""
            except Exception:
                pass
            if "file_sha256" in constraint:
                raise ValueError(
                    f"Live job already exists for file_sha256={file_sha256!r}"
                ) from exc
            raise ValueError(f"Duplicate job_id={job_id!r}") from exc

    def claim_job(self, lease_seconds: int = 3600) -> dict | None:
        """Atomic claim: one UPDATE, SKIP LOCKED, so two agents can
        never claim the same job. The oldest claimable job wins."""

        def _claim(conn: psycopg.Connection) -> dict | None:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs
                       SET status = 'uploading',
                           claimed_by = %s,
                           lease_expires_at = now() + make_interval(secs => %s),
                           attempt_count = attempt_count + 1
                       WHERE job_id = (
                           SELECT job_id FROM jobs
                           WHERE status = 'approved_for_upload'
                             AND (lease_expires_at IS NULL
                                  OR lease_expires_at < now())
                           ORDER BY created_at ASC
                           LIMIT 1
                           FOR UPDATE SKIP LOCKED
                       )
                       RETURNING *""",
                    (self.agent_id, lease_seconds),
                )
                row = cur.fetchone()
                return dict(row) if row else None

        claimed = self._with_conn(_claim)
        if claimed is not None:
            log.info(
                "Agent %r claimed job %r (attempt %d)",
                self.agent_id,
                claimed["job_id"],
                claimed["attempt_count"],
            )
        return claimed

    def get_job(self, job_id: str) -> dict | None:
        def _get(conn: psycopg.Connection) -> dict | None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM jobs WHERE job_id = %s", (job_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None

        return self._with_conn(_get)

    def transition(
        self,
        job_id: str,
        to_state: JobStatus,
        video_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        to_state = JobStatus(to_state)

        def _transition(conn: psycopg.Connection) -> dict:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM jobs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"Unknown job_id={job_id!r}")
                current = JobStatus(row["status"])
                validate_transition(current, to_state)

                sets = ["status = %s"]
                params: list[Any] = [to_state.value]
                if video_id is not None:
                    sets.append("video_id = %s")
                    params.append(video_id)
                if error is not None:
                    sets.append("last_error = %s")
                    params.append(error)
                if current == JobStatus.UPLOADING:
                    # Leaving 'uploading': the lease is over.
                    sets.append("claimed_by = NULL")
                    sets.append("lease_expires_at = NULL")
                params.append(job_id)
                cur.execute(
                    f"UPDATE jobs SET {', '.join(sets)} "  # noqa: S608
                    "WHERE job_id = %s RETURNING *",
                    params,
                )
                updated = cur.fetchone()
                assert updated is not None
                detail = f"{current.value} -> {to_state.value}"
                if error is not None:
                    detail += f"; error: {error[:500]}"
                cur.execute(
                    """INSERT INTO audit_log (job_id, agent_id, action, detail)
                       VALUES (%s, %s, 'transition', %s)""",
                    (job_id, self.agent_id, detail),
                )
                return dict(updated)

        return self._with_conn(_transition)

    def heartbeat(self, job_id: str, lease_seconds: int = 3600) -> None:
        def _beat(conn: psycopg.Connection) -> int:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs
                       SET lease_expires_at = now() + make_interval(secs => %s)
                       WHERE job_id = %s AND claimed_by = %s
                         AND status = 'uploading'""",
                    (lease_seconds, job_id, self.agent_id),
                )
                return cur.rowcount

        if self._with_conn(_beat) == 0:
            log.warning(
                "Heartbeat for job %r had no effect: not claimed by %r "
                "or no longer uploading (lease may have been lost)",
                job_id,
                self.agent_id,
            )

    def release_expired_leases(self) -> int:
        """Release ``uploading`` jobs whose lease expired.

        SUBTLETY — a lease expiry alone does NOT prove an upload
        happened, so the job goes back to ``approved_for_upload`` and
        NOT to ``upload_outcome_unknown``. But the mirror risk is real:
        the worker that lost its lease may STILL be uploading, and if a
        second worker claims the released job and uploads the same file,
        YouTube ends up with a duplicate. That is why this method logs a
        warning per released job (an operator should check whether the
        old worker is really dead before the job is re-claimed), and why
        the state machine + ``upload_outcome_unknown`` + reconciliation
        exist as the backstop: a worker that finishes an upload for a
        job it no longer owns must NOT blindly report success — its
        transition will fail validation and the outcome must be
        reconciled instead of re-uploaded.
        """

        def _release(conn: psycopg.Connection) -> list[str]:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs
                       SET status = 'approved_for_upload',
                           claimed_by = NULL,
                           lease_expires_at = NULL
                       WHERE status = 'uploading'
                         AND claimed_by IS NOT NULL
                         AND lease_expires_at IS NOT NULL
                         AND lease_expires_at < now()
                       RETURNING job_id""",
                    (),
                )
                released = [row["job_id"] for row in cur.fetchall()]
                for released_id in released:
                    cur.execute(
                        """INSERT INTO audit_log
                               (job_id, agent_id, action, detail)
                           VALUES (%s, %s, 'lease_expired_released',
                                   'Lease expired while uploading; returned to '
                                   'approved_for_upload. The previous worker may '
                                   'still be uploading — verify it is dead '
                                   'before re-claim.')""",
                        (released_id, self.agent_id),
                    )
                return released

        released = self._with_conn(_release)
        for released_id in released:
            log.warning(
                "Released expired lease on job %r back to "
                "approved_for_upload; the previous worker may still be "
                "uploading — verify it is dead before the job is re-claimed",
                released_id,
            )
        return len(released)

    def save_session(
        self, job_id: str, session_uri: str, bytes_uploaded: int
    ) -> None:
        def _save(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs
                       SET upload_session_uri = %s, bytes_uploaded = %s
                       WHERE job_id = %s""",
                    (session_uri, bytes_uploaded, job_id),
                )

        self._with_conn(_save)

    def record_attempt(
        self,
        job_id: str,
        attempt_no: int,
        outcome: str,
        error: str | None = None,
        video_id: str | None = None,
    ) -> None:
        def _record(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO upload_attempts
                           (job_id, attempt_no, outcome, video_id, error,
                            finished_at)
                       VALUES (%s, %s, %s, %s, %s, now())""",
                    (job_id, attempt_no, outcome, video_id, error),
                )

        self._with_conn(_record)

    def record_asset(
        self, job_id: str, asset_type: str, asset_id: str, verified: bool
    ) -> None:
        if asset_type not in ASSET_TYPES:
            raise ValueError(
                f"Unknown asset_type={asset_type!r}; "
                f"expected one of {sorted(ASSET_TYPES)}"
            )

        def _record(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO upload_assets
                           (job_id, asset_type, asset_id, verified)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (job_id, asset_type, asset_id) DO UPDATE
                         SET verified = EXCLUDED.verified""",
                    (job_id, asset_type, asset_id, verified),
                )

        self._with_conn(_record)

    def audit(
        self,
        job_id: str | None,
        agent_id: str,
        action: str,
        detail: str = "",
    ) -> None:
        def _audit(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO audit_log (job_id, agent_id, action, detail)
                       VALUES (%s, %s, %s, %s)""",
                    (job_id, agent_id, action, detail),
                )

        self._with_conn(_audit)

    def register_agent(self, agent_id: str, display_name: str) -> None:
        def _register(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO agent_registry
                           (agent_id, display_name, last_seen_at)
                       VALUES (%s, %s, now())
                       ON CONFLICT (agent_id) DO UPDATE
                         SET display_name = EXCLUDED.display_name,
                             last_seen_at = now()""",
                    (agent_id, display_name),
                )

        self._with_conn(_register)

    # -- quota (QuotaStore protocol) -----------------------------------------

    def get_usage(self, day: date) -> dict[str, int]:
        def _get(conn: psycopg.Connection) -> dict[str, int]:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT endpoint, units FROM quota_usage "
                    "WHERE pacific_day = %s",
                    (day,),
                )
                return {
                    row["endpoint"]: row["units"] for row in cur.fetchall()
                }

        return self._with_conn(_get)

    def record(self, day: date, endpoint: str, units: int) -> None:
        def _record(conn: psycopg.Connection) -> None:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO quota_usage (pacific_day, endpoint, units)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (pacific_day, endpoint) DO UPDATE
                         SET units = quota_usage.units + EXCLUDED.units,
                             updated_at = now()""",
                    (day, endpoint, units),
                )

        self._with_conn(_record)

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception:
                pass
            self._pool = None
        self._drop_conn()


class FakeQueue(BaseQueue):
    """Thread-safe in-memory queue for tests and dry-run.

    Enforces the same rules as :class:`QueueClient`: fail-closed kill
    switch (an internal flag, default False — uploads OFF), atomic claim
    under a lock, transition validation via
    ``state.validate_transition``, live-file duplicate rejection, and
    the QuotaStore protocol (delegated to ``InMemoryQuotaStore``).

    No database, no network, no secrets. Safe to construct anywhere.
    """

    def __init__(
        self, agent_id: str = "muse", uploads_enabled: bool = False
    ) -> None:
        """``uploads_enabled=True`` simulates the kill switch ON.

        Only for tests and dry-run simulations — the flag only ever
        feeds a simulation (paired with DryRunService), never a real
        upload. Default False (fail closed, like the real switch).
        """
        self.agent_id = agent_id
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._order: dict[str, int] = {}
        self._seq = 0
        self._attempts: list[dict] = []
        self._assets: dict[tuple[str, str, str], dict] = {}
        self._audit: list[dict] = []
        self._agents: dict[str, dict] = {
            "muse": {"agent_id": "muse", "display_name": "Muse"},
            "meem": {"agent_id": "meem", "display_name": "Meem"},
        }
        self._settings: dict[str, str] = {
            KILL_SWITCH_KEY: "true" if uploads_enabled else "false",
            "quota_safety_fraction": "0.90",
        }
        self._quota = InMemoryQuotaStore()

    # -- inspection helpers (tests only) --------------------------------

    @property
    def attempts(self) -> list[dict]:
        """Deep copy of recorded upload attempts (test inspection)."""
        import copy as _copy

        with self._lock:
            return _copy.deepcopy(self._attempts)

    @property
    def assets(self) -> list[dict]:
        """Deep copy of recorded upload assets (test inspection)."""
        import copy as _copy

        with self._lock:
            return _copy.deepcopy(list(self._assets.values()))

    @property
    def audit_log(self) -> list[dict]:
        """Deep copy of the audit log (test inspection)."""
        import copy as _copy

        with self._lock:
            return _copy.deepcopy(self._audit)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _copy_job(row: dict) -> dict:
        copy = dict(row)
        copy["metadata"] = dict(row["metadata"])
        return copy

    # -- kill switch (fail closed) ------------------------------------------

    def uploads_enabled(self) -> bool:
        try:
            with self._lock:
                return _parse_bool(self._settings.get(KILL_SWITCH_KEY))
        except Exception as exc:  # pragma: no cover - defensive parity
            log.warning(
                "Kill-switch check failed (%s); treating uploads as OFF",
                type(exc).__name__,
            )
            return False

    def set_uploads_enabled(self, enabled: bool) -> None:
        """Set the fake kill switch. Same rule as the real one: only on
        the owner's explicit instruction, never under the stop order."""
        with self._lock:
            self._settings[KILL_SWITCH_KEY] = "true" if enabled else "false"
        log.warning(
            "FakeQueue kill switch set to %s by agent_id=%r",
            "ON" if enabled else "OFF",
            self.agent_id,
        )

    # -- jobs ----------------------------------------------------------------

    def create_job(
        self,
        *,
        job_id: str,
        topic: str,
        source_file: str,
        file_sha256: str,
        title: str,
        description: str,
        metadata: dict,
    ) -> dict:
        with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"Duplicate job_id={job_id!r}")
            for other in self._jobs.values():
                if (
                    other["file_sha256"] == file_sha256
                    and other["status"] not in ("failed", "cancelled")
                ):
                    raise ValueError(
                        "Live job already exists for "
                        f"file_sha256={file_sha256!r}"
                    )
            now = self._now()
            row = {
                "job_id": job_id,
                "topic": topic,
                "agent_id": self.agent_id,
                "source_file": source_file,
                "file_sha256": file_sha256,
                "title": title,
                "description": description,
                "metadata": dict(metadata or {}),
                "status": JobStatus.QUEUED.value,
                "video_id": None,
                "upload_session_uri": None,
                "bytes_uploaded": 0,
                "claimed_by": None,
                "lease_expires_at": None,
                "attempt_count": 0,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            self._jobs[job_id] = row
            self._seq += 1
            self._order[job_id] = self._seq
            return self._copy_job(row)

    def claim_job(self, lease_seconds: int = 3600) -> dict | None:
        with self._lock:
            now = self._now()
            claimable = [
                row
                for row in self._jobs.values()
                if row["status"] == JobStatus.APPROVED_FOR_UPLOAD.value
                and (
                    row["lease_expires_at"] is None
                    or row["lease_expires_at"] < now
                )
            ]
            if not claimable:
                return None
            claimable.sort(
                key=lambda r: (r["created_at"], self._order[r["job_id"]])
            )
            row = claimable[0]
            row["status"] = JobStatus.UPLOADING.value
            row["claimed_by"] = self.agent_id
            row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            row["attempt_count"] += 1
            row["updated_at"] = now
            log.info(
                "Agent %r claimed job %r (attempt %d)",
                self.agent_id,
                row["job_id"],
                row["attempt_count"],
            )
            return self._copy_job(row)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._jobs.get(job_id)
            return self._copy_job(row) if row is not None else None

    def transition(
        self,
        job_id: str,
        to_state: JobStatus,
        video_id: str | None = None,
        error: str | None = None,
    ) -> dict:
        to_state = JobStatus(to_state)
        with self._lock:
            row = self._jobs.get(job_id)
            if row is None:
                raise ValueError(f"Unknown job_id={job_id!r}")
            current = JobStatus(row["status"])
            validate_transition(current, to_state)
            row["status"] = to_state.value
            if video_id is not None:
                row["video_id"] = video_id
            if error is not None:
                row["last_error"] = error
            if current == JobStatus.UPLOADING:
                row["claimed_by"] = None
                row["lease_expires_at"] = None
            row["updated_at"] = self._now()
            detail = f"{current.value} -> {to_state.value}"
            if error is not None:
                detail += f"; error: {error[:500]}"
            self.audit(job_id, self.agent_id, "transition", detail)
            return self._copy_job(row)

    def heartbeat(self, job_id: str, lease_seconds: int = 3600) -> None:
        with self._lock:
            row = self._jobs.get(job_id)
            if (
                row is None
                or row["claimed_by"] != self.agent_id
                or row["status"] != JobStatus.UPLOADING.value
            ):
                log.warning(
                    "Heartbeat for job %r had no effect: not claimed by %r "
                    "or no longer uploading (lease may have been lost)",
                    job_id,
                    self.agent_id,
                )
                return
            row["lease_expires_at"] = self._now() + timedelta(
                seconds=lease_seconds
            )
            row["updated_at"] = self._now()

    def release_expired_leases(self) -> int:
        """See :meth:`QueueClient.release_expired_leases` for the full
        subtlety: expiry alone does not prove an upload happened, so
        jobs return to ``approved_for_upload`` — but the worker that
        lost its lease may still be uploading, so each release is
        logged as a warning and must be checked before re-claim."""
        with self._lock:
            now = self._now()
            released: list[str] = []
            for row in self._jobs.values():
                if (
                    row["status"] == JobStatus.UPLOADING.value
                    and row["claimed_by"] is not None
                    and row["lease_expires_at"] is not None
                    and row["lease_expires_at"] < now
                ):
                    row["status"] = JobStatus.APPROVED_FOR_UPLOAD.value
                    row["claimed_by"] = None
                    row["lease_expires_at"] = None
                    row["updated_at"] = now
                    released.append(row["job_id"])
                    self.audit(
                        row["job_id"],
                        self.agent_id,
                        "lease_expired_released",
                        "Lease expired while uploading; returned to "
                        "approved_for_upload. The previous worker may "
                        "still be uploading — verify it is dead before "
                        "re-claim.",
                    )
            for released_id in released:
                log.warning(
                    "Released expired lease on job %r back to "
                    "approved_for_upload; the previous worker may still be "
                    "uploading — verify it is dead before the job is re-claimed",
                    released_id,
                )
            return len(released)

    def save_session(
        self, job_id: str, session_uri: str, bytes_uploaded: int
    ) -> None:
        with self._lock:
            row = self._jobs.get(job_id)
            if row is None:
                log.warning("save_session for unknown job %r; ignored", job_id)
                return
            row["upload_session_uri"] = session_uri
            row["bytes_uploaded"] = bytes_uploaded
            row["updated_at"] = self._now()

    def record_attempt(
        self,
        job_id: str,
        attempt_no: int,
        outcome: str,
        error: str | None = None,
        video_id: str | None = None,
    ) -> None:
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError(f"Unknown job_id={job_id!r}")
            for attempt in self._attempts:
                if (
                    attempt["job_id"] == job_id
                    and attempt["attempt_no"] == attempt_no
                ):
                    raise ValueError(
                        f"Duplicate attempt_no={attempt_no} for job {job_id!r}"
                    )
            now = self._now()
            self._attempts.append(
                {
                    "job_id": job_id,
                    "attempt_no": attempt_no,
                    "outcome": outcome,
                    "video_id": video_id,
                    "error": error,
                    "started_at": now,
                    "finished_at": now,
                }
            )

    def record_asset(
        self, job_id: str, asset_type: str, asset_id: str, verified: bool
    ) -> None:
        if asset_type not in ASSET_TYPES:
            raise ValueError(
                f"Unknown asset_type={asset_type!r}; "
                f"expected one of {sorted(ASSET_TYPES)}"
            )
        with self._lock:
            if job_id not in self._jobs:
                raise ValueError(f"Unknown job_id={job_id!r}")
            key = (job_id, asset_type, asset_id)
            existing = self._assets.get(key)
            if existing is not None:
                existing["verified"] = verified
            else:
                self._assets[key] = {
                    "job_id": job_id,
                    "asset_type": asset_type,
                    "asset_id": asset_id,
                    "verified": verified,
                    "created_at": self._now(),
                }

    def audit(
        self,
        job_id: str | None,
        agent_id: str,
        action: str,
        detail: str = "",
    ) -> None:
        with self._lock:
            self._audit.append(
                {
                    "job_id": job_id,
                    "agent_id": agent_id,
                    "action": action,
                    "detail": detail,
                    "created_at": self._now(),
                }
            )

    def register_agent(self, agent_id: str, display_name: str) -> None:
        with self._lock:
            now = self._now()
            existing = self._agents.get(agent_id)
            if existing is not None:
                existing["display_name"] = display_name
                existing["last_seen_at"] = now
            else:
                self._agents[agent_id] = {
                    "agent_id": agent_id,
                    "display_name": display_name,
                    "registered_at": now,
                    "last_seen_at": now,
                }

    # -- quota (QuotaStore protocol) -----------------------------------------

    def get_usage(self, day: date) -> dict[str, int]:
        return self._quota.get_usage(day)

    def record(self, day: date, endpoint: str, units: int) -> None:
        self._quota.record(day, endpoint, units)

    def close(self) -> None:
        """No-op: the fake holds no connections."""


__all__ = [
    "ASSET_TYPES",
    "KILL_SWITCH_KEY",
    "BaseQueue",
    "FakeQueue",
    "QueueClient",
]
