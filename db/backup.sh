#!/usr/bin/env bash
# Backup / export procedure for the Sacred Knowledge YouTube uploader queue.
#
# WHAT THIS BACKS UP — job metadata ONLY (jobs, attempts, assets, quota
# usage, agent registry, audit log, system settings). It contains:
#   * NO video files (videos live on disk / YouTube, never in this DB),
#   * NO OAuth tokens, client secrets, or passwords — tokens are NEVER
#     stored in this database (each agent keeps its own refresh token in
#     its Secure Vault). A backup therefore cannot leak credentials.
#
# USAGE:
#   DATABASE_URL='postgresql://...' ./db/backup.sh
#
# DATABASE_URL must come from the environment (or the Secure Vault on the
# machine running this). It is NEVER written into this script or into any
# file in this repository, and it is never echoed by this script.
#
# WHAT IT DOES:
#   1. Dumps the database twice: once in pg_dump custom format (.dump,
#      for pg_restore) and once as plain SQL (.sql, human-readable and
#      restorable with plain psql).
#   2. Verifies both dumps are non-empty; fails loudly if either is empty.
#   3. Keeps the last 7 backups of each format and deletes older ones.
#   4. Prints restore instructions.
#
# REQUIREMENTS: pg_dump / pg_restore / psql (PostgreSQL client tools).
# Neon Free plan: backups are taken from the client side; no paid Neon
# feature is used or assumed.
set -euo pipefail

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL is not set. Refusing to run." >&2
    echo "Set it from the Secure Vault, e.g.:" >&2
    echo "  export DATABASE_URL='postgresql://user:password@host/db?sslmode=require'" >&2
    echo "Never paste a real connection string into a file, chat message, or shell history file." >&2
    exit 1
fi

command -v pg_dump >/dev/null 2>&1 || {
    echo "ERROR: pg_dump not found. Install the PostgreSQL client tools first." >&2
    exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/backups}"
KEEP="${BACKUP_KEEP:-7}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

CUSTOM_FILE="$BACKUP_DIR/youtube-queue-$TIMESTAMP.dump"
PLAIN_FILE="$BACKUP_DIR/youtube-queue-$TIMESTAMP.sql"

echo "Backing up uploader queue database (metadata only — no videos, no tokens)..."
echo "Backup directory: $BACKUP_DIR"

# Custom format (compressed, pg_restore-able) + plain SQL (psql-readable).
pg_dump --format=custom --file="$CUSTOM_FILE" "$DATABASE_URL"
pg_dump --format=plain --file="$PLAIN_FILE" "$DATABASE_URL"

# Verify: both dumps must exist and be non-empty.
for f in "$CUSTOM_FILE" "$PLAIN_FILE"; do
    if [ ! -s "$f" ]; then
        echo "ERROR: backup file is missing or empty: $f" >&2
        echo "The database may be unreachable or empty. Backup FAILED — older backups were NOT deleted." >&2
        exit 1
    fi
done

echo "Backup OK:"
ls -l "$CUSTOM_FILE" "$PLAIN_FILE"

# Retention: keep the last $KEEP backups of each format, delete older ones.
# (Filenames sort chronologically thanks to the UTC timestamp.)
for pattern in 'youtube-queue-*.dump' 'youtube-queue-*.sql'; do
    # shellcheck disable=SC2086
    old_files="$(ls -1t "$BACKUP_DIR"/$pattern 2>/dev/null | tail -n +"$((KEEP + 1))" || true)"
    if [ -n "$old_files" ]; then
        echo "Deleting backups older than the last $KEEP ($pattern):"
        echo "$old_files"
        echo "$old_files" | xargs rm --
    fi
done

cat <<'EOF'

Restore instructions
--------------------
Custom-format dump (preferred — restores cleanly into an empty database):
  pg_restore --clean --if-exists -d "$DATABASE_URL" youtube-queue-<timestamp>.dump

Plain-SQL dump (works with plain psql, no pg_restore needed):
  psql "$DATABASE_URL" < youtube-queue-<timestamp>.sql

Notes:
  * Restore into an EMPTY database (or use --clean as above). The schema
    is idempotent, so restoring over the current schema is safe.
  * Set DATABASE_URL from the Secure Vault before running either command.
    Never paste a real connection string into chat or a file.
  * After restoring, confirm the kill switch is still OFF unless the
    owner has explicitly authorised uploads:
      SELECT key, value FROM system_settings WHERE key = 'global_upload_enabled';
EOF
