# Installation Guide

How to install and run the Sacred Knowledge YouTube API uploader.
No step here performs a real upload, and no step needs a real
secret — placeholders are used throughout. Real credentials live in
each agent's Secure Vault and are injected as environment variables.

## Requirements

- Python **3.11** or newer.
- `git`.
- PostgreSQL client tools (`psql`) — only needed if you apply
  database migrations by hand (see below).
- No paid service is used anywhere. The database is Neon's free
  plan; the YouTube Data API is free within its quota.

## 1. Clone the repository

```bash
git clone <repo-url>
cd youtube-api-upload
```

## 2. Create a virtual environment and install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or, for an editable install (recommended when developing):

```bash
pip install -e .
```

Either way, verify the install:

```bash
python -c "import youtube_uploader; print(youtube_uploader.__version__)"
```

## 3. Configuration

```bash
cp .env.example .env
```

Edit `.env` and fill in your own values. The file is git-ignored and
must never be committed. In production the values come from the
Secure Vault as environment variables; `.env` is for local
development only.

| Variable | Meaning |
|---|---|
| `AGENT_ID` | Agent identity: `muse`, `meem`, or `agent3`. |
| `GOOGLE_CLIENT_ID` | Desktop OAuth client ID (no secret — safe to show). |
| `GOOGLE_CLIENT_SECRET` | Desktop OAuth client secret. **Secret.** |
| `YOUTUBE_REFRESH_TOKEN` | This agent's own refresh token. **Secret.** |
| `DATABASE_URL` | This agent's restricted Neon connection string. **Secret.** |
| `DRY_RUN` | `true` (default) = no real YouTube calls. |
| `UPLOADS_FORCE_OFF` | `true` (default). Can only turn uploads OFF, never ON. |
| `LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR`. |

**Defaults are safe:** a fresh checkout has `DRY_RUN=true`,
`UPLOADS_FORCE_OFF=true`, and the database kill switch defaults to
OFF, so nothing can upload until the owner deliberately enables it.

## 4. Run the tests

```bash
pytest
```

To run a single test file:

```bash
pytest tests/test_state.py -v
```

Tests are mock-only: no test ever calls the real YouTube API, the
real Notion API, or a live database (tests use an in-memory quota
store and injected fake clients).

## 5. Run a dry-run

A dry-run walks the whole upload flow — job claim, metadata
validation, quota reservation, state transitions — without ever
calling `videos.insert` or any other YouTube write endpoint.

```bash
DRY_RUN=true python -m youtube_uploader.cli dry-run --job <job-id>
```

Expected result: the flow completes (or stops at the kill switch /
stop order, which is also a correct dry-run outcome) and the report
shows which calls *would* have been made. If any real YouTube call
is attempted during a dry-run, that is a bug — report it.

## 6. Apply database migrations

Migrations live in `db/migrations/` (Phase C) and are numbered in
order (`001_...sql`, `002_...sql`, …). Apply them against the Neon
database with `psql`, using the connection string from the Secure
Vault — never paste the connection string into chat or a shared
file:

```bash
psql "$DATABASE_URL" -f db/migrations/001_init.sql
psql "$DATABASE_URL" -f db/migrations/002_quota.sql
```

Apply every migration in numeric order, exactly once. Re-running a
migration must be safe (migrations are written idempotently where
possible); if one fails part-way, fix the cause and re-run it — do
not hand-edit the database schema.

Verify the schema afterwards:

```bash
psql "$DATABASE_URL" -c "\dt"
```

You should see the queue tables: `jobs`, `upload_attempts`,
`upload_assets`, `quota_usage`, `agent_registry`, `audit_log`,
`system_settings`.

## 7. Back up the database

The backup script (`db/backup.sh`, Phase C) exports the
queue database to a timestamped file. Run it regularly — at least
before any migration and at least once a day while uploads are
running:

```bash
./db/backup.sh
```

The script writes to `backups/` (git-ignored). It exports job and
quota data only — **the backup contains no secrets**: no tokens, no
client secrets, no connection strings. Keep backup files on the
local machine; do not upload them to GitHub or any shared folder.

To restore from a backup (only when the database is lost or
corrupted):

```bash
psql "$DATABASE_URL" -f backups/<backup-file>.sql
```

## 8. Run the uploader

```bash
python -m youtube_uploader.cli run
```

The CLI reads its configuration from the environment, checks the
kill switch and the stop order first, and only then claims and
uploads jobs. With the defaults above (`DRY_RUN=true`,
`UPLOADS_FORCE_OFF=true`) it will refuse to upload and say why —
that is the expected behaviour until the owner enables uploads.

See `README.md` for the project overview and `SECURITY.md` for the
security rules that apply to every run.
