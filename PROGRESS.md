# PROGRESS.md — Sacred Knowledge YouTube API Upload Migration

> Updated after each meaningful milestone. Overall % = completed work
> across Phases A–E. **No real YouTube upload has been performed.**
> The owner's global stop order remains active and supreme.

## Status

| Item | Value |
|---|---|
| Overall completion | 100% (Phases A–E) |
| Current phase | DONE — Phases A–E complete; awaiting owner-only setup steps + Phase 5 approval |
| Started | 20 Nov 2026 ~22:00 PKT |
| Completed | 20 Nov 2026 ~22:20 PKT |
| Last update | 20 Nov 2026 ~22:20 PKT — tests green (164/164), test report written, local git repo initialized |

## Policy change — owner order 20 Nov 2026 ~22:04

**Every video publishes PUBLIC — no per-video publishing approval.**
State machine updated: `approved_for_publishing` and `scheduled`
states DROPPED. Final flow: queued → approved_for_upload →
uploading → uploaded_private → assets_verified → ready_for_review
→ published (public, automatic per the standing rule). Uploads
still start PRIVATE (`privacyStatus: private` on every upload
request) as a safety measure; publish to public happens
automatically after assets verify. No scheduled publishing.
Kill switch + stop order still gate ALL uploads.

## Completed

- [x] Phase A — scaffolding, shared contracts (`state.py` 10 states, `config.py`, `quota.py`, `errors.py`), OAuth helper (`auth.py`: scopes youtube.upload + youtube.force-ssl only, offline access, token refresh, channel verification)
- [x] Phase B — Upload engine: `uploader.py` (resumable, session persistence, expiry restart, outcome-unknown), `assets.py` (captions/thumbnail/playlist + publish), `verify.py`, `reconcile.py`, `dryrun.py`, `cli.py`
- [x] Phase C — Neon schema + migrations (parse-validated), `queue.py` (QueueClient + thread-safe FakeQueue, atomic claiming, kill switch fail-closed), `db/backup.sh`
- [x] Phase D — `notion_sync.py` (idempotent flip + job-dict adapter), Meem package, user setup guide, install guide
- [x] Phase E — Tests: **164 passed, 0 failed, 0 skipped** (see TEST_REPORT.md). 5 genuine src bugs found by tests and fixed.
- [x] Docs: README.md, ARCHITECTURE.md, SECURITY.md, CHANGELOG.md, TEST_REPORT.md
- [x] Integration verified: compileall OK, all-module import OK, CLI `dry-run-upload` end-to-end OK (videos.insert blocked as designed)
- [x] Local git repo initialized (no remote yet — see Pending)

## Acceptance checklist (Plan v2 §8)

9 of 11 fully PASS; 2 PARTIAL (both need the owner's setup steps —
no live Neon DB and no real Google credentials exist yet):

- ✅ Unit tests pass (164/164) · ✅ Concurrent locking (in-memory race test; live-DB check pending) · ✅ Kill switch blocks every upload path · ✅ Dry-run never calls upload endpoints · ⚠️ Token refresh (mocked credentials only; live refresh pending) · ✅ Interrupted-session recovery · ✅ No blind retries on ambiguous outcomes · ✅ Caption/thumbnail verification · ✅ Quota monitoring · ✅ No secrets in logs/Git · ✅ Stop order enforced

Full detail: TEST_REPORT.md.

## Pending (outside this build)

- [ ] GitHub repo `FileifyPK/youtube-api-uploader` — **could not be created from here**: the MCP GitHub connector is not connected, `gh` CLI is not logged in, and the custom.github token is scoped only to `muse-coordination`. Manual step: create the repo (public, empty) on GitHub, then `git remote add origin https://github.com/FileifyPK/youtube-api-uploader.git && git push -u origin main` from `~/workspace/youtube-api-upload/`.
- [ ] Owner-only steps (Google Cloud project, API enable, consent screen Production, Desktop client, Neon account, per-agent consent) — user side, not started
- [ ] Setup-day verifications: live `psql` migration apply, QueueClient against Neon, live token refresh + channel check
- [ ] Phase 5 — any real upload: needs the owner's separate explicit approval

## Known issues

- Quota `reserve()` is a check, not an atomic write: two agents could both pass the check concurrently and together overshoot the budget by up to one video (~500 units). The 90% safety threshold (1,000 units of headroom) covers this; atomic claiming prevents double-upload of the same job. A DB-atomic reservation is a possible future hardening, not required for acceptance.
- `psycopg_pool` is not installed; QueueClient uses the single-connection path (with reconnect-and-retry on operational failure). The pool path activates automatically if `psycopg-pool` is added to requirements later.
- SQL validated by parsing only — a real `psql` apply + QueueClient integration test against Neon remains for setup day (no database exists yet).

## File tree

```
youtube-api-upload/
├── README.md  ARCHITECTURE.md  SECURITY.md  CHANGELOG.md
├── PROGRESS.md  TEST_REPORT.md
├── pyproject.toml  requirements.txt  .env.example  .gitignore
├── src/youtube_uploader/
│   ├── __init__.py  state.py  config.py  quota.py  errors.py
│   ├── auth.py  uploader.py  assets.py  verify.py  reconcile.py
│   ├── dryrun.py  queue.py  cli.py  notion_sync.py
├── db/
│   ├── migrations/001_init.sql  schema.sql  backup.sh
├── docs/
│   ├── INSTALL.md  MEEM_SETUP.md  USER_SETUP_GUIDE.md
└── tests/
    ├── conftest.py
    ├── test_state.py  test_config.py  test_quota.py  test_auth.py
    ├── test_queue_fake.py  test_engine_gates.py  test_engine_upload.py
    ├── test_assets_verify.py  test_reconcile.py  test_dryrun_cli.py
    ├── test_notion_sync.py  test_no_secrets.py
```

Totals: ~4,100 lines of source, ~3,200 lines of tests + SQL/shell.
Test suite: 164 tests, all passing.

## 20 Nov 2026 ~22:55 — Setup day: Google side complete, live verification done

- Google Cloud: project verified, API enabled, consent screen scopes
  added, Desktop OAuth client created, app published to Production
  (homepage = repo URL, privacy policy = GitHub Pages URL).
- Muse authorization: consent under fileifypk@gmail.com, code
  exchanged, refresh token in .env (600, gitignored).
- Channel verified live: Sacred Knowledge (UCxOd_poXW1Yn_geTd8AqL4A),
  79 subscribers — via verify_channel() with expected_channel_id.
- Live token refresh verified (acceptance item 5 now PASS, not
  PARTIAL): refresh_access_token() succeeded repeatedly.
- Proxy fix (auth.py): this VM sits behind an egress proxy that
  MITMs TLS with a custom CA. httplib2 reads neither proxy env vars
  nor SSL_CERT_FILE, so API calls failed first with SSL
  WRONG_VERSION_NUMBER, then (after adding ProxyInfo) with
  CERTIFICATE_VERIFY_FAILED. Fix: _proxy_http() builds a
  proxy-aware httplib2.Http (PySocks added as a dependency) with
  ca_certs from SSL_CERT_FILE / REQUESTS_CA_BUNDLE / the system
  bundle; build_youtube_service wraps it in AuthorizedHttp and
  passes ONLY http (googleapiclient treats http and credentials as
  mutually exclusive). Test suite now 165/165.
- test_no_env_file_exists replaced by
  test_env_file_not_tracked_by_git: a live .env legitimately exists
  from setup day on; the invariant is that git never tracks it.

## 20 Nov 2026 ~23:05 — Setup day: Neon complete, all acceptance live-verified

- Neon account created via fileifypk Google SSO (Free plan, no card
  or phone requested). Project sacred-knowledge-upload-queue
  (cold-rice-96435730), AWS US East 2, Postgres 18, DB
  sacred_knowledge, owner role sacred_knowledge_owner.
- Migration 001 applied live (7 tables). Restricted roles muse +
  meem created by scripts/setup_neon.py (CRUD on app tables,
  SELECT-only on system_settings, sequences + default privileges).
  Passwords generated at setup, stored in .env.owner (600);
  muse DSN in .env DATABASE_URL. Meem's password is HELD in
  .env.owner until the owner decides how it reaches Meem's machine.
- Kill switch verified OFF in the live DB (both owner and muse
  reads). Muse role verified: can CRUD jobs, CANNOT write
  system_settings (least privilege).
- Claim-lock test live: two concurrent connections racing the same
  FOR UPDATE SKIP LOCKED claim — exactly one winner. Acceptance
  item 4 (live DB locking) now PASS. With live token refresh
  (item 5) also verified, acceptance is 11/11.
- Networking (important): this VM's outbound TCP is intercepted —
  direct psycopg to Neon:5432 gets an HTTP response from the egress
  gateway. The egress proxy DOES allow HTTP CONNECT to Neon, so
  all DB access goes through a local CONNECT tunnel
  (scripts/neon_tunnel.py). Neon routes by TLS SNI, so DSNs keep
  the real Neon hostname (with the local tunnel port) and add
  hostaddr=127.0.0.1; without the hostname Neon answers
  "Endpoint ID is not specified". The pooler tunnel runs as a
  persistent systemd service (neon-tunnel-pooler, 127.0.0.1:15433);
  the user manager fails in this VM so it is a SYSTEM service
  (unit in /etc is ephemeral — reinstall from the repo copy after
  a VM replacement; the proxy env file lives in $HOME).
