# TEST REPORT — Sacred Knowledge YouTube API Upload Migration

**Date:** 20 Nov 2026 ~22:20 PKT
**Suite:** pytest, run locally in the project venv (Python 3.12, pytest 9.2)
**Command:** `.venv/bin/python -m pytest tests/ -q`
**Result: 164 passed, 0 failed, 0 skipped** (0.51s, stable across repeated runs)

No network, no real YouTube calls, no real credentials, no live
database in any test. YouTube is mocked (`DryRunService` /
`unittest.mock`); the database is `FakeQueue` (in-memory); the only
`QueueClient` test is fail-closed against an unreachable DSN.

## Per-file counts

| File | Tests | Covers |
|---|---|---|
| test_state.py | 30 | State machine (10 states, all transitions, illegal-transition rejection, dropped states absent) |
| test_assets_verify.py | 21 | Captions/thumbnail/playlist/publish + verification + quota |
| test_queue_fake.py | 19 | FakeQueue CRUD/transitions/leases/kill switch + 8-thread claim race |
| test_quota.py | 15 | Quota costs, Pacific reset, thresholds, videos_fittable |
| test_config.py | 14 | Settings defaults, secrets never in repr/logs |
| test_engine_upload.py | 14 | Resumable upload, privacy, assessment, resume, expiry restart, outcome-unknown, retries |
| test_auth.py | 13 | Scopes, auth URL, code exchange (mocked Flow), token refresh (mocked), channel verification |
| test_engine_gates.py | 11 | Upload gates in order (dry-run, force-off, kill switch, state, quota, assessment, sha256) |
| test_notion_sync.py | 8 | Notion flip, idempotency, missing-row path, job-dict adapter |
| test_dryrun_cli.py | 7 | DryRunService block, CLI dry-run end-to-end, full mock pipeline to `published` |
| test_no_secrets.py | 6 | Repo hygiene (no tokens/keys, no .env, .gitignore, no dropped states in src) |
| test_reconcile.py | 6 | Reconciliation found / not_found / uncertain (never blind re-upload) |
| conftest.py | — | Shared fakes (scripted resumable service, HTTP-error builder, job/file helpers) |

## Acceptance checklist (Plan v2 §8)

| # | Condition | Status | Evidence |
|---|---|---|---|
| 1 | All unit tests pass | ✅ PASS | 164/164 green, verified by the coordinator's own run |
| 2 | Database locking works under concurrent agents | ⚠️ PARTIAL | 8-thread claim race on FakeQueue: exactly one winner. `QueueClient` uses a single-statement `FOR UPDATE SKIP LOCKED` claim (reviewed, not executed — no live Neon DB exists yet). Live-DB verification awaits setup day. |
| 3 | Kill switch blocks every upload path | ✅ PASS | Gates tested: kill switch OFF, unreachable DB → False (fail closed), dry-run, force-off; DryRunService blocks videos.insert unconditionally |
| 4 | Dry-run never calls YouTube upload endpoints | ✅ PASS | DryRunService raises on videos.insert; engine gate tests assert zero service calls in dry-run; CLI dry-run records exactly 1 blocked attempt |
| 5 | Token refresh works with test credentials | ⚠️ MOCKED | Refresh wiring tested with mocked credentials (build + refresh call + scopes + missing-credential errors). A live refresh against Google needs the owner's real refresh token (setup day) |
| 6 | Recovery handles interrupted sessions | ✅ PASS | Session expiry → fresh session (cap 2); resume from saved session URI + bytes; chunk retry with backoff |
| 7 | Ambiguous upload outcomes do not trigger blind retries | ✅ PASS | Final-chunk ambiguity → UploadOutcomeUnknownError, videos.insert called exactly once; reconciliation decides found/not_found/uncertain; uncertain stays unknown for human review |
| 8 | Captions and thumbnail verification work | ✅ PASS | verify_caption / verify_thumbnail tested (boolean results; CLI raises on False); full pipeline verifies all assets before publish |
| 9 | Quota monitoring works | ✅ PASS | Costs, Pacific-midnight reset, 90% safety threshold, 100/day video bucket, exact full-pipeline total (insert 1 + captions 400 + thumbnail 50 + playlist 50 + update 50) |
| 10 | No secrets in logs or Git history | ✅ PASS | test_no_secrets scans the repo; config repr masks secrets; code/token never logged (asserted via caplog); the initial commit contains only clean source (verified before commit) |
| 11 | Stop order remains fully enforced | ✅ PASS | Kill switch defaults OFF, DRY_RUN defaults true, uploads_force_off defaults true; every upload path funnels through the engine gates; the stop order is documented as supreme in README/SECURITY/PROGRESS |

## Bugs found by the tests and fixed (5)

1. **Session-restart violated `UNIQUE(job_id, attempt_no)`** — restarts now go to the append-only audit log instead of `record_attempt`.
2. **Claim→upload gate conflict** — `claim_job()` moves a job to `uploading`, but the upload gate required `approved_for_upload`; gate now also accepts `uploading` when `claimed_by == this agent` (another agent's claim is still refused).
3. **Resume key mismatch** — queue rows use `upload_session_uri`, the uploader read only `session_uri`; both keys are now read (uploader + CLI).
4. **`JobView.agent_id` fallback dead** — `claimed_by` is `None` (not missing) after upload, so notes read "Agent: "; fallback now triggers on a `None` value.
5. **CLI asset-type bug** (found at integration, before tests) — playlist assets were recorded as `"playlist_item"`, which the SQL CHECK rejects; now `"playlist"`.

## What is NOT yet verified (needs the owner's setup steps)

- A live `psql` apply of the migrations + `QueueClient` against real Neon.
- A live Google token refresh + channel verification with the real refresh token.
- A real resumable upload against YouTube (needs the owner's separate explicit approval — Phase 5).
- The `psycopg_pool` code path (`psycopg-pool` not installed; single-connection path active).
