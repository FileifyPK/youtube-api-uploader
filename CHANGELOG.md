# Changelog

All notable changes to the Sacred Knowledge YouTube API uploader
will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added — 20 Nov 2026 (Phase E)

- Full pytest suite: **164 tests, all passing** (see TEST_REPORT.md):
  state machine, config, quota, auth (mocked), FakeQueue incl.
  8-thread claim race, engine gates, resumable upload mechanics,
  assets + verification, reconciliation, dry-run + CLI, Notion
  sync, repo secret hygiene.
- `TEST_REPORT.md` with the acceptance-checklist status (9 of 11
  fully pass; live-DB locking and live token refresh await the
  owner's setup steps).

### Fixed — 20 Nov 2026 (found by the test suite)

- Session restart no longer violates `UNIQUE(job_id, attempt_no)`
  (restarts go to the audit log).
- Upload gate accepts a job this agent already claimed (`uploading`
  + `claimed_by == self`); another agent's claim is still refused.
- Resume reads `upload_session_uri` (the key the queue actually
  stores) as well as `session_uri`.
- Tracker notes show the uploading agent even after the claim is
  cleared (`claimed_by` is `None`, not missing).
- CLI records playlist assets as `"playlist"` (the SQL CHECK
  allows only `caption`/`thumbnail`/`playlist`).

### Changed — 20 Nov 2026 (owner order ~22:04)

- Publishing policy: **every video publishes PUBLIC — no per-video
  publishing approval.** The `approved_for_publishing` and
  `scheduled` states are dropped from the state machine; the final
  flow is queued → approved_for_upload → uploading →
  uploaded_private → assets_verified → ready_for_review →
  published (public, automatic per the standing rule). Uploads
  still start PRIVATE as a safety measure; no scheduled publishing.
  Kill switch + stop order still gate all uploads.

## [0.1.0] — 2026-11-20

Initial implementation preparation (Phases A–E scope, per Plan v2).

> **No real YouTube upload has been performed.** The owner's global
> stop order (15 November 2026) remains active and supreme. All
> testing is mock / dry-run only.

### Added

- **Phase A — Preparation**
  - Project skeleton: `src/youtube_uploader/`, `db/migrations/`,
    `tests/`, `docs/`.
  - `pyproject.toml`, `requirements.txt`, `.env.example`
    (placeholders only), `.gitignore` (secrets excluded).
  - Shared contracts: `state.py` (job state machine with
    `upload_outcome_unknown`), `config.py` (environment-only
    configuration; secrets never logged; dry-run defaults to true),
    `quota.py` (two quota buckets, Pacific-midnight reset,
    500 units/video full pipeline), `errors.py` (shared exceptions).
  - OAuth helper (`auth.py`).
- **Phase B — Upload engine**
  - Resumable upload engine (`uploader.py`), assets
    (`assets.py`: captions, thumbnail, playlist), post-upload
    verification (`verify.py`), reconciliation
    (`reconcile.py`), dry-run mode (`dryrun.py`), CLI (`cli.py`).
- **Phase C — Central queue (Neon PostgreSQL)**
  - Schema + SQL migrations, `queue.py` (atomic claiming, leases,
    kill switch, quota table), backup procedure.
- **Phase D — Agent integration**
  - `notion_sync.py`: Notion tracker sync with an injected client
    (Production Queue flip + ✅ Uploaded row, idempotent —
    refuses to record a video URL twice); pure, unit-testable
    payload builders; no Notion SDK dependency.
  - `docs/MEEM_SETUP.md`: Meem integration package (own token,
    own restricted Neon credentials, atomic claiming,
    `upload_outcome_unknown` handling, troubleshooting).
- **Phase E — Testing (mock / dry-run only)**
  - Test layout in `tests/`; mock-only tests — no test ever calls
    the real YouTube API, the real Notion API, or a live database.
- **Documentation**
  - `README.md` (project overview, safety model, quickstart).
  - `ARCHITECTURE.md` (components, data flow, state machine,
    quota buckets, failure modes, concurrency, security
    boundaries).
  - `SECURITY.md` (secret handling, least privilege, kill switch,
    private-by-default, duplicate-upload honesty, incident
    response).
  - `docs/INSTALL.md` (install, test, dry-run, migrations,
    backup).
  - `docs/USER_SETUP_GUIDE.md` (owner-only setup: Google Cloud,
    consent screen, Desktop client, per-agent consent, Neon).
  - `PROGRESS.md` (live status + acceptance checklist).

### Security

- Kill switch defaults OFF; database unreachable is treated as OFF
  (fail closed).
- Every upload request carries `privacyStatus: private` explicitly.
- Scopes limited to `youtube.upload` + `youtube.force-ssl`.
- No secrets in the repository, logs, or Git history.
