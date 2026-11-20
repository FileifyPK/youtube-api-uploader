#!/usr/bin/env python3
"""One-time Neon setup for the Sacred Knowledge YouTube uploader.

Run once on setup day (re-runs are safe/idempotent):

1. Runs db/migrations/001_init.sql against the live Neon database
   (direct host — DDL must not go through the PgBouncer pooler).
2. Creates restricted per-agent LOGIN roles (muse, meem) with
   generated passwords — least privilege:
   - CONNECT + USAGE on schema public.
   - SELECT/INSERT/UPDATE/DELETE on jobs, upload_attempts,
     upload_assets, quota_usage, agent_registry, audit_log.
   - SELECT ONLY on system_settings (the kill switch is read by the
     engine; flipping it is an ADMIN action that needs the owner
     connection — a leaked agent credential must never turn uploads ON).
   - USAGE/SELECT on all sequences + default privileges for future
     tables/sequences created by the owner role.
   - NO CREATE/DROP/ALTER, no superuser, no access to other databases.
3. Writes the muse connection string (pooler) into .env as
   DATABASE_URL, and records both agents' passwords + connection
   strings in .env.owner (600, gitignored) — Meem's password is HELD
   there until the owner decides how it reaches Meem's machine.
4. Verifies live: tables exist, kill switch is OFF, the muse role
   can connect + CRUD a test job, the muse role CANNOT write
   system_settings, and the FOR UPDATE SKIP LOCKED claim gives
   exactly one winner between two concurrent connections.

Secrets: this script NEVER prints a password or connection string.
It prints only verification results (counts, booleans, role names).

NETWORKING: this VM cannot reach Neon directly (outbound TCP is
intercepted by the egress gateway). All connections go through local
CONNECT tunnels (scripts/neon_tunnel.py, started in-process here;
the pooler tunnel also runs as a persistent user systemd service).
Neon routes by TLS SNI, so every DSN keeps the real Neon hostname
(with the local tunnel port) and adds hostaddr=127.0.0.1 — without
the hostname, Neon answers "Endpoint ID is not specified".

Usage (from the repo root):
    .venv/bin/python scripts/setup_neon.py
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg
from psycopg import sql

from neon_tunnel import ensure_tunnel

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
OWNER_ENV_PATH = REPO_ROOT / ".env.owner"
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "001_init.sql"

APP_TABLES_CRUD = [
    "jobs",
    "upload_attempts",
    "upload_assets",
    "quota_usage",
    "agent_registry",
    "audit_log",
]
OWNER_ROLE = "sacred_knowledge_owner"
DATABASE = "sacred_knowledge"
# Local tunnel ports (see scripts/neon_tunnel.py). The pooler tunnel
# also runs as a persistent user systemd service on 15433.
DIRECT_TUNNEL_PORT = 15432
POOLER_TUNNEL_PORT = 15433


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").split("\n")
    out, replaced = [], False
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out), encoding="utf-8")
    os.chmod(path, 0o600)


def parse_dsn(url: str) -> tuple[str, str, str]:
    """Split a DSN into (user, password, host). Values are never
    printed by this module."""
    parsed = urlparse(url)
    return parsed.username or "", parsed.password or "", parsed.hostname or ""


def build_dsn(user: str, password: str, host: str, port: int) -> str:
    """Build a tunneled DSN: the real Neon hostname (SNI routing)
    with the local tunnel port, connecting via 127.0.0.1."""
    return (
        f"postgresql://{user}:{password}@{host}:{port}/{DATABASE}"
        "?sslmode=require&channel_binding=require&hostaddr=127.0.0.1"
    )


def role_exists(conn: psycopg.Connection, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        )
        return cur.fetchone() is not None


def ensure_role(
    conn: psycopg.Connection, role: str, password: str
) -> bool:
    """Create the role if missing, else set its password. Returns
    True if the role was newly created."""
    if role_exists(conn, role):
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        return False
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE ROLE {} WITH LOGIN PASSWORD {}"
            ).format(sql.Identifier(role), sql.Literal(password))
        )
    return True


def grant_privileges(conn: psycopg.Connection, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(DATABASE), sql.Identifier(role)
            )
        )
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(role)
            )
        )
        for table in APP_TABLES_CRUD:
            cur.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}"
                ).format(sql.Identifier(table), sql.Identifier(role))
            )
        # Kill switch: read-only for agents (see module docstring).
        cur.execute(
            sql.SQL("GRANT SELECT ON TABLE system_settings TO {}").format(
                sql.Identifier(role)
            )
        )
        cur.execute(
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(role))
        )
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(
                sql.Identifier(OWNER_ROLE), sql.Identifier(role)
            )
        )
        cur.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(
                sql.Identifier(OWNER_ROLE), sql.Identifier(role)
            )
        )


def main() -> int:
    owner_env = read_env_file(OWNER_ENV_PATH)
    owner_direct = owner_env.get("OWNER_DIRECT_DATABASE_URL", "")
    owner_pooler = owner_env.get("OWNER_DATABASE_URL", "")
    if not owner_direct or not owner_pooler:
        print("ERROR: .env.owner must hold OWNER_DIRECT_DATABASE_URL "
              "and OWNER_DATABASE_URL")
        return 1

    # Hosts from the stored Neon DSNs; connections go through local
    # CONNECT tunnels (the pooler tunnel may already be running as a
    # systemd service — ensure_tunnel reuses it).
    owner_user, owner_pw, direct_host = parse_dsn(owner_direct)
    _pooler_user, _pooler_pw, pooler_host = parse_dsn(owner_pooler)
    ensure_tunnel(DIRECT_TUNNEL_PORT, direct_host)
    ensure_tunnel(POOLER_TUNNEL_PORT, pooler_host)
    owner_dsn = build_dsn(owner_user, owner_pw, direct_host, DIRECT_TUNNEL_PORT)

    # Passwords: reuse stored ones when present (idempotent re-runs),
    # otherwise generate fresh ones.
    muse_pw = owner_env.get("MUSE_DB_PASSWORD", "") or secrets.token_urlsafe(24)
    meem_pw = owner_env.get("MEEM_DB_PASSWORD", "") or secrets.token_urlsafe(24)

    print("== Step 1: migration (direct host, via tunnel) ==")
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            print("tables in public schema:", cur.fetchone()[0])
            cur.execute(
                "SELECT value FROM system_settings "
                "WHERE key = 'global_upload_enabled'"
            )
            print("kill switch global_upload_enabled:", cur.fetchone()[0])

        print("== Step 2: restricted roles ==")
        for role, pw in (("muse", muse_pw), ("meem", meem_pw)):
            created = ensure_role(conn, role, pw)
            grant_privileges(conn, role)
            print(f"role {role}: {'created' if created else 'password set + grants refreshed'}")

    # Step 3: store credentials (600, gitignored).
    muse_dsn = build_dsn("muse", muse_pw, pooler_host, POOLER_TUNNEL_PORT)
    meem_dsn = build_dsn("meem", meem_pw, pooler_host, POOLER_TUNNEL_PORT)
    set_env_value(ENV_PATH, "DATABASE_URL", muse_dsn)
    set_env_value(OWNER_ENV_PATH, "MUSE_DB_PASSWORD", muse_pw)
    set_env_value(OWNER_ENV_PATH, "MEEM_DB_PASSWORD", meem_pw)
    set_env_value(OWNER_ENV_PATH, "MUSE_DATABASE_URL", muse_dsn)
    set_env_value(OWNER_ENV_PATH, "MEEM_DATABASE_URL", meem_dsn)
    print("== Step 3: credentials stored ==")
    print(".env DATABASE_URL set (muse role, pooler): yes")
    print(".env.owner holds both agent passwords: yes")

    # Step 4: live verification as the muse role (pooler).
    print("== Step 4: live verification (muse role) ==")
    with psycopg.connect(muse_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM system_settings "
                "WHERE key = 'global_upload_enabled'"
            )
            print("muse can read kill switch:", cur.fetchone()[0])
            cur.execute(
                "INSERT INTO jobs (job_id, topic, source_file, file_sha256, title) "
                "VALUES ('setup-selftest', 'setup selftest', '/tmp/x.mp4', "
                "'00' || repeat('a', 62), 'setup selftest') "
                "ON CONFLICT (job_id) DO NOTHING"
            )
            cur.execute(
                "SELECT count(*) FROM jobs WHERE job_id = 'setup-selftest'"
            )
            print("muse can insert+read jobs:", cur.fetchone()[0] == 1)
            try:
                cur.execute(
                    "UPDATE system_settings SET value = 'false' "
                    "WHERE key = 'global_upload_enabled'"
                )
                print("muse can WRITE system_settings: YES (UNEXPECTED — grants wrong)")
            except psycopg.errors.InsufficientPrivilege:
                print("muse CANNOT write system_settings: confirmed (least privilege)")

    # Lock test: two connections race for the same claim; exactly one wins.
    print("== Step 5: claim-lock test (FOR UPDATE SKIP LOCKED) ==")
    conn_a = psycopg.connect(muse_dsn, autocommit=False)
    conn_b = psycopg.connect(muse_dsn, autocommit=False)
    try:
        with conn_a.cursor() as cur_a:
            cur_a.execute(
                "SELECT job_id FROM jobs WHERE job_id = 'setup-selftest' "
                "FOR UPDATE SKIP LOCKED"
            )
            won_a = cur_a.fetchone() is not None
        with conn_b.cursor() as cur_b:
            cur_b.execute(
                "SELECT job_id FROM jobs WHERE job_id = 'setup-selftest' "
                "FOR UPDATE SKIP LOCKED"
            )
            won_b = cur_b.fetchone() is not None
        print("connection A won:", won_a, "| connection B won:", won_b,
              "| exactly one winner:", (won_a ^ won_b))
        conn_a.rollback()
        conn_b.rollback()
    finally:
        conn_a.close()
        conn_b.close()

    # Cleanup the self-test row (owner connection, via tunnel).
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM jobs WHERE job_id = 'setup-selftest'")
            cur.execute("SELECT count(*) FROM jobs")
            print("jobs rows after cleanup:", cur.fetchone()[0])

    print("SETUP COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
