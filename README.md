# Sacred Knowledge — YouTube API Uploader

Uploads videos to the **Sacred Knowledge** channel
(@SacredKnowledgeHQ) through the YouTube Data API v3 instead of a
browser login — private by default, kill-switch gated, and
coordinated through a shared Neon PostgreSQL job queue so two agents
(Muse and Meem) never upload the same video twice.

**Status: in build (Phases A–E). No real YouTube upload has been
performed.** The owner's global stop order (15 November 2026) is
active and supreme: nothing uploads until the owner lifts it. See
`PROGRESS.md` for the live status and the acceptance checklist.

## What it is

- **Resumable uploads** via `videos.insert`, with session URIs and
  byte tracking so an interrupted upload resumes where it stopped.
- **Full pipeline per video:** metadata, captions (SRT), thumbnail,
  and playlist add — every step verified on YouTube before the job
  counts as complete.
- **Shared queue:** Neon PostgreSQL holds jobs, quota usage, agent
  registry, and audit log. Atomic claiming means two agents can
  never claim the same job.
- **Honest duplicate handling:** if YouTube may have accepted an
  upload but the response was lost, the job goes to
  `upload_outcome_unknown` and is reconciled before any retry. We
  never claim duplicates are impossible.
- **Notion sync:** after a verified upload, the Production Queue row
  flips to uploaded and a row is added to the 🎬 Uploaded Videos
  ✅ Uploaded table with the live link.

## Safety model

- **Kill switch** (`global_upload_enabled` in the database) —
  default OFF. Database unreachable is treated as OFF (fail closed).
- **Stop order supreme** — the owner's stop order overrides
  everything, including the kill switch. No agent, automation, or
  test script can bypass it.
- **Private by default** — every upload request carries
  `privacyStatus: private` explicitly. Once assets verify, the
  video goes Public automatically per the owner's standing rule
  (no per-video publishing approval, no scheduled publishing).
- **Dry-run default** — `DRY_RUN=true` unless deliberately turned
  off. A dry-run never calls `videos.insert`.
- **No secrets in the repo** — tokens, client secrets, and database
  URLs live in each agent's Secure Vault only. Never in files, logs,
  chat, or GitHub.
- **Least-privilege scopes** — `youtube.upload` +
  `youtube.force-ssl` only. Never the broad `youtube` scope.
- **Per-video assessment** — `madeForKids` and
  `containsSyntheticMedia` are decided per video from a content
  assessment, never hardcoded.
- **Zero spend** — no paid service anywhere. Neon free plan (no
  card), YouTube Data API free within quota.

## Quickstart

```bash
git clone <repo-url>
cd youtube-api-upload
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # placeholders only; never commit .env

pytest                                    # mock-only tests
DRY_RUN=true python -m youtube_uploader.cli dry-run --job <job-id>
```

Full install instructions: `docs/INSTALL.md`.
Owner setup (Google Cloud, consent, Neon): `docs/USER_SETUP_GUIDE.md`.
Meem's integration package: `docs/MEEM_SETUP.md`.

## Project layout

```
youtube-api-upload/
├── README.md                  This file
├── ARCHITECTURE.md            Full architecture (components, data flow, failure modes)
├── SECURITY.md                Security policy
├── CHANGELOG.md               Version history
├── PROGRESS.md                Live status + acceptance checklist (coordinator-owned)
├── pyproject.toml             Package metadata + dependencies
├── requirements.txt
├── .env.example               Placeholder config (never real secrets)
├── src/youtube_uploader/
│   ├── __init__.py            Package version
│   ├── state.py               Job state machine (done)
│   ├── config.py              Env-only config; secrets never logged (done)
│   ├── quota.py               Two quota buckets, Pacific reset (done)
│   ├── errors.py              Shared exceptions (done)
│   ├── notion_sync.py         Notion tracker flip, injected client (done)
│   ├── auth.py                OAuth helper
│   ├── uploader.py            Resumable upload engine
│   ├── assets.py              Captions / thumbnail / playlist
│   ├── verify.py              Post-upload verification
│   ├── reconcile.py           upload_outcome_unknown reconciliation
│   ├── dryrun.py              Dry-run mode
│   ├── queue.py               Neon queue: claiming, leases, kill switch
│   └── cli.py                 Command-line entry point
├── db/migrations/             SQL migrations (Phase C)
├── scripts/                   Backup script (Phase C)
├── tests/                     Unit + mock tests (Phase E)
└── docs/
    ├── INSTALL.md             Installation guide
    ├── USER_SETUP_GUIDE.md    Owner setup steps
    └── MEEM_SETUP.md          Meem integration package
```

All modules are built against the shared
contracts in `state.py`, `config.py`, `quota.py`, and `errors.py`;
their public interfaces follow the architecture in
`ARCHITECTURE.md`.

## Quota at a glance

Two independent buckets, resetting at midnight **Pacific Time**:

| Bucket | Limit | Cost |
|---|---|---|
| Video uploads (`videos.insert`) | 100/day | 1 unit each |
| Shared (captions, thumbnail, playlist, update) | 10,000 units/day | caption 400, thumbnail 50, playlist 50, update 50 |

Full pipeline per video with playlist: **500 units → 20 videos/day**.
Without playlist: 450 units → ~22/day. The tracker auto-pauses at
90% of the shared bucket.

## Acceptance checklist

The 11 acceptance conditions from Plan v2 §8 (all tests pass,
locking works under concurrency, kill switch blocks every upload
path, dry-run never calls the upload endpoint, token refresh works
on test credentials, interruption recovery works, uncertain uploads
are never re-uploaded, caption/thumbnail verification works, quota
monitoring works, no secrets in logs or Git history, stop order
fully enforced) are tracked in `PROGRESS.md`. No test-upload
approval will be requested until every item passes.

## Documentation

- `ARCHITECTURE.md` — components, data flow, state machine, failure modes, security boundaries.
- `SECURITY.md` — secret handling, kill switch, incident response.
- `docs/INSTALL.md` — install, test, dry-run, migrations, backup.
- `docs/USER_SETUP_GUIDE.md` — owner-only setup steps.
- `docs/MEEM_SETUP.md` — Meem integration package.
- `CHANGELOG.md` — version history.
