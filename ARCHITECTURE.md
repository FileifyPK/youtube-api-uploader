# Architecture — Sacred Knowledge YouTube API Uploader

Authoritative spec: **YouTube API Upload Plan v2** (20 November
2026). This document describes the architecture as specified there
and as implemented in this repository. All components listed here
are implemented; test status is tracked in PROGRESS.md.

## 1. Overview

The uploader replaces only the **browser upload step** of the
Sacred Knowledge pipeline. NotebookLM Studio work stays in the
browser (it has no API). Meem builds the video files (download /
transcription / package / build); this uploader takes a finished
video file + metadata package and uploads it to YouTube via the
YouTube Data API v3, coordinated through a shared Neon PostgreSQL
job queue.

Two agents (Muse, Meem; a third later) share:

- one Google Cloud project ("Sacred Knowledge Uploads"),
- one OAuth client (Desktop app),
- one Neon database (the queue),
- one kill switch,

but each agent has its **own** refresh token and its **own**
restricted database credentials.

## 2. Components

| Component | Module | Responsibility |
|---|---|---|
| Auth | `auth.py` | OAuth 2.0 for YouTube: builds credentials from the per-agent refresh token, refreshes access tokens. The authorization code / token never passes through chat; it goes straight to the Secure Vault. Scopes: `youtube.upload` + `youtube.force-ssl` only. |
| Upload engine | `uploader.py` | Resumable `videos.insert` upload: creates the session, saves the session URI immediately, tracks uploaded bytes, resumes from the last acknowledged byte, starts a fresh session if the old one expired (~1 week of inactivity). Every request carries `privacyStatus: private` explicitly. |
| Assets | `assets.py` | Captions (SRT upload), thumbnail set, playlist add — all via API. |
| Verification | `verify.py` | Post-upload checks: caption present, thumbnail applied, playlist membership, metadata matches. A job is not "complete" on the video ID alone. |
| Reconciliation | `reconcile.py` | Resolves `upload_outcome_unknown`: searches YouTube for a video matching the job (title + file fingerprint) and decides whether the video exists, definitely does not exist, or is uncertain (→ human review). |
| Queue | `queue.py` | Neon PostgreSQL access: atomic job claiming, leases, state transitions, kill-switch reads, quota table, audit log. |
| Quota | `quota.py` (done) | Two-bucket accounting with Pacific-midnight reset; reservation-based, with a 90% safety threshold that auto-pauses uploads. |
| State machine | `state.py` (done) | `JobStatus` enum + allowed transitions; `VIDEO_MAY_EXIST_STATES` forbids re-upload from any state where a video may already exist. |
| Config | `config.py` (done) | Environment-only settings; secrets never logged; `DRY_RUN` defaults to true; `UPLOADS_FORCE_OFF` can only turn uploads off. |
| Errors | `errors.py` (done) | Shared exceptions (`KillSwitchOffError`, `UploadOutcomeUnknownError`, `SessionExpiredError`, `VerificationError`, `ReconciliationUncertainError`, …). |
| Dry-run | `dryrun.py` | Simulates the full flow without any YouTube write call. A dry-run never calls `videos.insert`. |
| CLI | `cli.py` | Command-line entry point: `run`, `dry-run`, and status commands. Checks kill switch + stop order before anything else. |
| Notion sync | `notion_sync.py` (done) | Flips the Production Queue row to uploaded and adds the ✅ Uploaded row with the live link. Takes an injected client; no live Notion calls, no Notion SDK. |

## 3. Data flow for one upload

```
Meem builds video + metadata package
        │
        ▼
Job created in Neon (state: queued)
  — permanent Job ID, file SHA-256 fingerprint, agent, metadata
        │
        ▼
Owner approval → state: approved_for_upload
        │
        ▼
Agent claims job (ATOMIC — one agent only; lease granted)
        │
        ▼
Safety gates, in order:
  1. Stop order lifted?            (if not: stop)
  2. Kill switch ON?               (DB unreachable = OFF → stop)
  3. DRY_RUN off?                  (if on: simulate only)
  4. State == approved_for_upload? (else: refuse)
  5. Quota reservation available?  (else: pause until Pacific reset)
        │
        ▼
Metadata validation + per-video content assessment
  (madeForKids / containsSyntheticMedia decided per video — never hardcoded)
        │
        ▼
Resumable upload (videos.insert, privacyStatus: private)
  — session URI + bytes saved to the job row as it progresses
  — state: uploading
        │
        ├─ response lost ──► state: upload_outcome_unknown
        │                    → reconcile (see §6) — NEVER blind re-upload
        │
        ▼
Video ID received → state: uploaded_private
        │
        ▼
Assets: captions → thumbnail → playlist add
        │
        ▼
Verify every step → state: assets_verified
        │
        ▼
state: ready_for_review
        │
        ▼
Notion sync: Production Queue row → uploaded;
🎬 Uploaded Videos ✅ table gains a row with the live link
        │
        ▼
Publish: ready_for_review → published (PUBLIC, automatic per the
owner's standing rule — no per-video approval, no scheduled
publishing; uploads still start private as a safety measure)
```

## 4. State machine

```
queued ──► approved_for_upload ──► uploading ──► uploaded_private
   │              │                    │  ╲          │
   │              │                    │   ╲         ▼
   │              │                    │    ╲──► upload_outcome_unknown ──► uploaded_private
   │              │                    │    (reconcile found it)      ╲
   │              │                    │                              ╲──► approved_for_upload
   │              │                    │                              (reconcile proved nothing uploaded)
   │              │                    ▼
   │              │                 failed
   │              ▼
   │           cancelled
   ▼
uploaded_private ──► assets_verified ──► ready_for_review ──► published
                                              (PUBLIC, automatic per
                                              standing owner rule —
                                              no per-video approval,
                                              no scheduled publishing)
```

- `UPLOADABLE_STATES`: only `approved_for_upload` may start a
  `videos.insert` call.
- `VIDEO_MAY_EXIST_STATES`: `uploading`, `upload_outcome_unknown`,
  `uploaded_private`, `assets_verified`, `ready_for_review`,
  `published` — re-upload
  from any of these is forbidden without reconciliation.
- Terminal states: `published`, `failed`, `cancelled`.
- Upload and publish are separate steps: every upload starts
  private; publish to Public happens automatically once assets
  verify, per the owner's standing rule (owner order 20 Nov 2026).
  There is no per-video publishing approval and no scheduled
  publishing. The gate that matters is at upload time: the kill
  switch + the owner's stop order.

See `src/youtube_uploader/state.py` for the enforced transition
table (`ALLOWED_TRANSITIONS`, `can_transition`,
`validate_transition`).

## 5. Quota — two buckets

Quota resets at midnight **Pacific Time** (America/Los_Angeles),
not Pakistan midnight. All accounting uses the Pacific calendar day
(`quota.pacific_today`).

| Bucket | Limit | What it covers |
|---|---|---|
| Video uploads | 100 `videos.insert`/day, 1 unit each | Uploads only |
| Shared | 10,000 units/day | captions.insert 400, thumbnails.set 50, playlistItems.insert 50, videos.update 50, plus cheap read calls (videos.list 1, playlistItems.list 1, channels.list 1, captions.list 50, search.list 100) |

- Full pipeline per video with playlist: 400 + 50 + 50 = **500
  units → 20 videos/day**.
- Without playlist insertion: 450 units → ~22/day.
- The video bucket (100/day) is never the binding limit; the shared
  bucket is.
- Reservation-based: units are reserved **before** the API call and
  recorded after. A reservation past the 90% safety threshold raises
  `QuotaExceeded` and the uploader pauses — never blind-retries
  after a quota error.
- Usage is recorded centrally in the `quota_usage` table so all
  agents share one count.

## 6. Failure modes and recovery

| Failure | Detection | Recovery |
|---|---|---|
| VM restart mid-upload | Job row shows `uploading` with saved session URI + bytes; lease expires | New worker claims the job (after lease expiry), resumes the resumable session from the last acknowledged byte. |
| Network cut mid-upload | Upload call times out / errors | Same as VM restart: resume from saved byte offset. |
| Resumable session expired (~1 week idle) | YouTube returns a session error (`SessionExpiredError`) | Start a fresh upload session for the same job; do not treat as failure of the job itself. |
| Response lost after YouTube accepted the upload | No video ID returned, no error either | Job → `upload_outcome_unknown`. **Never re-upload.** Run reconciliation: if the video is found, adopt its ID and continue as `uploaded_private`; if proven absent, return to `approved_for_upload`; if uncertain, escalate to the human. |
| Token revoked / expired | `invalid_grant` / 401 on API calls | Stop. Alert the owner for re-consent (per-agent; other agents unaffected). Never retry with the same token, never borrow another agent's token. |
| Quota exhausted | `QuotaExceeded` / quota error from YouTube | Pause uploads until the Pacific-midnight reset. Never blind-retry. |
| Database unreachable | Connection failure / timeout | **Fail closed:** treat the kill switch as OFF, stop all uploads. Jobs are safe in the queue and resume when the database returns. |
| Caption / thumbnail / playlist step fails | Verification step reports mismatch | Job stays in `uploaded_private`; the failed asset step is retried individually (assets are idempotent). The video itself is not re-uploaded. |
| Verification mismatch | `VerificationError` | Job → `failed` (or stays for manual fix); the mismatch is logged with the exact expected vs actual values. |
| Duplicate suspected | 🎬 Uploaded Videos page already has the URL, or reconciliation finds a video | Do not upload. Record the existing video ID and let the human decide. |

## 7. Concurrency model

- **Atomic claiming:** claiming a job is a single atomic database
  operation (row lock / conditional update). Two agents can never
  hold the same job. A claim is never decided by reading the table
  and then acting — the claim itself is the lock.
- **Leases:** a claim grants a time-limited lease. The holding agent
  renews it while working. If the lease expires (crash, stall), the
  job is released for another agent — but a job released mid-upload
  goes through reconciliation before any retry, because a video may
  already exist.
- **One job per worker:** a worker claims one job at a time and does
  not hold jobs idle.
- **Quota is central:** all agents reserve against the same
  `quota_usage` table, so the daily budget is shared, not per-agent.
- **Kill switch is global:** one row in `system_settings`, read by
  every agent before every upload. No local override can turn
  uploads ON.

## 8. Security boundaries

- **Secrets live in the Secure Vault only.** Refresh tokens, the
  OAuth client secret, and Neon connection strings are injected as
  environment variables at runtime. They never appear in files,
  logs, Git history, chat, or GitHub. `config.redact()` masks them
  in any logged mapping; `Settings.__repr__` shows only set/unset.
- **What the database holds:** job metadata (IDs, titles, file
  SHA-256 fingerprints, state, video IDs, session URIs, byte
  counts, timestamps, error logs), quota usage, agent registry,
  audit log, system settings (kill switch). **What it never holds:**
  tokens, client secrets, or any credential.
- **What GitHub holds:** code and documentation only. No secrets,
  no tokens, no connection strings. Coordination messages between
  agents carry job IDs and statuses, never credentials.
- **Least privilege everywhere:** YouTube scopes are
  `youtube.upload` + `youtube.force-ssl` only; each agent's Neon
  user can only read/write the queue tables; each agent's token is
  its own.
- **Audit log:** every state transition, claim, upload attempt, and
  kill-switch read is recorded in `audit_log` with agent and
  timestamp.
- **Backups** contain job/quota data only — no secrets (see
  `docs/INSTALL.md` §7).

## 9. What this system deliberately does not do

- No NotebookLM Studio work (no API — browser only).
- No pinned comments (no API endpoint — Studio/browser only).
- No scheduled publishing — every video goes Public immediately
  once assets verify (owner order 20 Nov 2026).
- No per-video publishing approval — publishing is automatic per
  the owner's standing rule; the approval gate is at upload time
  (kill switch + stop order).
- No central always-on upload service (VMs restart; the direct
  model has no single point of failure). A service may be added
  later if the agent count grows.
- No real upload of any kind while the stop order stands.
