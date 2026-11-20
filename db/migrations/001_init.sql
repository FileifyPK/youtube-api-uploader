-- Migration 001 — initial schema for the Sacred Knowledge YouTube API uploader.
--
-- Plan v2, Phase C (Neon PostgreSQL, Free plan).
-- PostgreSQL 15+ compatible. No extensions required.
--
-- SAFETY NOTES (read before changing anything):
--   * Every upload starts PRIVATE (privacyStatus: private, set by the
--     uploader on every request). Upload and publishing are separate:
--     nothing in this schema can publish a video on its own.
--   * `global_upload_enabled` (system_settings) is the kill switch and
--     defaults to OFF ('false'). The queue client treats ANY database
--     problem as OFF (fail closed). It may only be turned ON on the
--     owner's explicit instruction, and never while his stop order stands.
--   * `upload_outcome_unknown` means YouTube may already have accepted
--     an upload whose response (video ID) was lost. Jobs in that state
--     must be reconciled against YouTube before any retry — never
--     blindly re-uploaded.
--   * This database stores job metadata ONLY: no video files, no OAuth
--     tokens, no client secrets. Tokens live in each agent's Secure
--     Vault, never here.
--
-- Idempotent where practical: re-running this file is safe
-- (IF NOT EXISTS / ON CONFLICT DO NOTHING / OR REPLACE / DROP IF EXISTS).

-- ---------------------------------------------------------------------------
-- jobs — one row per video upload job, from queueing to publishing.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    agent_id TEXT,
    source_file TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN (
            'queued',
            'approved_for_upload',
            'uploading',
            'upload_outcome_unknown',
            'uploaded_private',
            'assets_verified',
            'ready_for_review',
            'published',
            'failed',
            'cancelled'
        )),
    video_id TEXT,
    upload_session_uri TEXT,
    bytes_uploaded BIGINT NOT NULL DEFAULT 0,
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_claimed_by_idx ON jobs (claimed_by);
CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at);

-- One live job per file: a second job for the same file fingerprint is
-- rejected while any non-terminal job for it exists. Failed/cancelled
-- jobs do not block a fresh job for the same file.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_file_sha256_live_uniq
    ON jobs (file_sha256)
    WHERE status NOT IN ('failed', 'cancelled');

COMMENT ON TABLE jobs IS 'One row per video upload job. Status follows the Plan v2 state machine; upload and publishing are separate concerns — no transition goes from an upload state straight to published.';
COMMENT ON COLUMN jobs.file_sha256 IS 'SHA-256 fingerprint of the source video file. The partial unique index jobs_file_sha256_live_uniq blocks a second live job for the same file.';
COMMENT ON COLUMN jobs.status IS 'Job state. upload_outcome_unknown means YouTube may already have the video but the response was lost: reconcile before any retry, never re-upload blindly.';
COMMENT ON COLUMN jobs.video_id IS 'YouTube video ID, set only once the upload response is received. NULL until then — a NULL video_id on a non-terminal job proves nothing about whether YouTube has the video.';
COMMENT ON COLUMN jobs.upload_session_uri IS 'Resumable-upload session URI, saved immediately on creation so an interrupted upload can resume. Sessions may expire after ~1 week of inactivity.';
COMMENT ON COLUMN jobs.claimed_by IS 'Agent holding the current lease (atomic claim via SELECT ... FOR UPDATE SKIP LOCKED). NULL when unclaimed.';
COMMENT ON COLUMN jobs.lease_expires_at IS 'Lease deadline. A job whose lease expired while uploading is released back to approved_for_upload — a lease expiry alone does NOT prove an upload happened.';

-- Keep jobs.updated_at fresh on every UPDATE.
CREATE OR REPLACE FUNCTION set_jobs_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS jobs_updated_at_trigger ON jobs;
CREATE TRIGGER jobs_updated_at_trigger
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION set_jobs_updated_at();

-- ---------------------------------------------------------------------------
-- upload_attempts — one row per videos.insert attempt for a job.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upload_attempts (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    attempt_no INT NOT NULL,
    outcome TEXT NOT NULL,
    video_id TEXT,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    UNIQUE (job_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS upload_attempts_job_id_idx
    ON upload_attempts (job_id);

COMMENT ON TABLE upload_attempts IS 'Audit trail of every upload attempt (outcome: e.g. success, failed, outcome_unknown, session_expired). Never delete rows except by cascading job deletion.';

-- ---------------------------------------------------------------------------
-- upload_assets — captions, thumbnails, playlist memberships per job.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS upload_assets (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    asset_type TEXT NOT NULL
        CHECK (asset_type IN ('caption', 'thumbnail', 'playlist')),
    asset_id TEXT NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, asset_type, asset_id)
);

COMMENT ON TABLE upload_assets IS 'Per-job upload assets. A job may only reach assets_verified once every required asset row exists AND is verified against YouTube (a video ID alone is never enough).';

-- ---------------------------------------------------------------------------
-- quota_usage — daily YouTube API quota consumption, Pacific calendar days.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quota_usage (
    id BIGSERIAL PRIMARY KEY,
    pacific_day DATE NOT NULL,
    endpoint TEXT NOT NULL,
    units INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pacific_day, endpoint)
);

COMMENT ON TABLE quota_usage IS 'Quota accounting. pacific_day is the America/Los_Angeles calendar day (quota resets at Pacific midnight, not PKT midnight). videos.insert has its own 100/day bucket; all other endpoints share the 10,000 units/day bucket.';

-- ---------------------------------------------------------------------------
-- agent_registry — agents allowed to claim jobs. A third agent registers
-- later with a plain INSERT; no schema change is needed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_registry (
    agent_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ
);

INSERT INTO agent_registry (agent_id, display_name) VALUES
    ('muse', 'Muse'),
    ('meem', 'Meem')
ON CONFLICT (agent_id) DO NOTHING;

COMMENT ON TABLE agent_registry IS 'Known agents. New agents register with an INSERT (register_agent); no migration is needed to add one.';

-- ---------------------------------------------------------------------------
-- audit_log — who did what to which job, append-only.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT REFERENCES jobs (job_id) ON DELETE SET NULL,
    agent_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_job_id_idx ON audit_log (job_id);
CREATE INDEX IF NOT EXISTS audit_log_created_at_idx ON audit_log (created_at);

COMMENT ON TABLE audit_log IS 'Append-only audit trail. job_id is NULL for system-level actions; rows survive job deletion (ON DELETE SET NULL).';

-- ---------------------------------------------------------------------------
-- system_settings — global switches. The kill switch lives here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Kill switch: OFF by default. Only the owner may turn it ON, and only
-- after lifting the stop order. The queue client treats a missing row,
-- a missing table, or an unreachable database as OFF (fail closed).
INSERT INTO system_settings (key, value) VALUES
    ('global_upload_enabled', 'false'),
    ('quota_safety_fraction', '0.90')
ON CONFLICT (key) DO NOTHING;

COMMENT ON TABLE system_settings IS 'Global switches. global_upload_enabled=false (kill switch, OFF by default) means NO agent may upload — including when this database is unreachable. quota_safety_fraction (0.90) is the share of the daily quota at which new uploads pause.';
