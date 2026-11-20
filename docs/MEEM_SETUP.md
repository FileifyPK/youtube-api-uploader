# Meem Integration Package

How Meem (the second agent) joins the Sacred Knowledge YouTube API
uploader. Meem does the download / transcription / package / build
chain and the API upload step; Muse does NotebookLM Studio work and
the Notion trackers. Both agents share one Neon job queue and one
kill switch.

**The owner's stop order (15 November 2026) applies to Meem exactly
as it applies to Muse.** No upload of any kind — test, private,
unlisted or public — until the owner lifts it. The kill switch is
global: when it is OFF, every agent is OFF.

---

## 1. What Meem needs

| Item | Detail |
|---|---|
| Repo | Same repository as Muse (clone from GitHub — see `docs/INSTALL.md`). No separate fork. |
| Python | 3.11+; install with `pip install -r requirements.txt` (or `pip install -e .`). |
| Refresh token | **Meem's OWN.** Obtained by the owner giving consent in the owner's own browser for Meem's agent identity. Never Muse's token, never shared, never sent via GitHub or chat. Stored straight in **Meem's own Secure Vault**. |
| Neon credentials | **Meem's OWN restricted database credentials** (its own Neon database user, least privilege — read/write on the queue tables only, no admin rights). Stored in Meem's own Secure Vault. |
| Agent identity | `AGENT_ID=meem` in Meem's own `.env`. |

Meem must never use Muse's `.env`, Muse's token, or Muse's database
credentials. One token per agent is deliberate: if one token is
revoked, the other agent keeps working.

Setup steps for the owner (Google Cloud project, consent screen,
Desktop OAuth client, Neon account) are the same shared project —
see `docs/USER_SETUP_GUIDE.md`. The per-agent consent step is
repeated once per agent.

## 2. Claiming jobs

Jobs live in the Neon `jobs` table. Claiming is **atomic**: exactly
one agent can claim a given job. Meem must claim through the queue
client (`queue.py`) — never by reading the table and deciding
locally, and never by taking a job another agent has already claimed.

Rules:

- Only claim jobs in state `approved_for_upload`. Never claim a job
  in `queued` (not yet approved), `uploading` (another agent holds
  it), or any `VIDEO_MAY_EXIST` state (a video may already exist on
  YouTube for that job).
- A claim grants a **lease** (a limited time window). The lease must
  be renewed while the upload runs; if the lease expires, the job is
  released and another agent may claim it. A job whose lease expired
  mid-upload goes through reconciliation, not a blind re-upload
  (see §4).
- Claim one job at a time per worker. Do not claim a batch and hold
  jobs idle — quota and slots are shared.

## 3. Reporting completion

When a job reaches `assets_verified` (upload + caption + thumbnail +
playlist all verified on YouTube), Meem reports completion through
the normal channels:

1. The job row in Neon carries the final state, `video_id`, and
   timestamps — that is the shared record of truth.
2. A completion message goes to the GitHub coordination repo
   (`muse-coordination`) so Muse knows the upload is done and can
   flip the Notion trackers (Production Queue row → uploaded, ✅
   Uploaded row with the live link). Notion flipping is Muse's
   responsibility; Meem does not write to Notion.
3. If anything failed or is uncertain, report the exact state and the
   job ID — never a bare "done".

## 4. `upload_outcome_unknown` — the rule that must never be broken

If an upload call's response is lost (network cut, VM restart,
timeout after YouTube may have accepted the upload), the job goes to
`upload_outcome_unknown`. **Meem must never blindly re-upload that
job.** Re-uploading risks a duplicate video on the channel, and
duplicates are not "impossible" — the whole point of this state is
honesty about that.

The correct sequence:

1. Stop. Do not re-upload, do not claim a replacement job for the
   same topic.
2. Run reconciliation (`reconcile.py`): search YouTube for a video
   matching this job's title + file fingerprint and check whether a
   video already exists for the job.
3. If reconciliation **finds** the video: adopt its video ID, move
   the job to `uploaded_private`, and continue with asset
   verification.
4. If reconciliation **proves** nothing was uploaded: the job may
   return to `approved_for_upload` and be retried.
5. If reconciliation is **uncertain** (cannot prove either way):
   escalate to the human (the owner). Do not guess. The job stays in
   `upload_outcome_unknown` until a human decides.

## 5. The kill switch and the stop order

- `global_upload_enabled` in the `system_settings` table is the kill
  switch. Default OFF. Only the owner turns it ON.
- **Database unreachable = kill switch OFF.** If Meem cannot reach
  Neon, it must treat uploads as disabled and stop. Fail closed,
  always.
- The kill switch is global across agents: Meem cannot override it
  locally, and `UPLOADS_FORCE_OFF=true` in `.env` can only ever turn
  uploads OFF, never ON.
- The owner's stop order sits above everything, including the kill
  switch. If the stop order is in force, no upload happens even if
  the kill switch were ON.

## 6. Troubleshooting

### Token revoked

Symptom: every API call returns `invalid_grant` / 401.

- Stop uploading immediately. Do not retry with the same token.
- Report to the owner: Meem's refresh token needs re-consent (owner
  clicks Allow again in their own browser; the new token goes
  straight to Meem's Secure Vault).
- Muse's token is unaffected — Muse keeps working. Never "borrow"
  Muse's token.

### Lease expired

Symptom: the queue reports the job is no longer held by Meem, or
another agent has claimed it.

- Stop work on that job immediately. Do not finish the upload.
- If an upload was in flight, the job must go through
  reconciliation (§4) before anyone retries it.
- Check why the lease lapsed (VM sleep, long stall, missing
  renewal) before claiming the next job.

### Quota paused

Symptom: `QuotaExceeded`, or the quota tracker reports the safety
threshold reached.

- Pause new uploads. **Never blind-retry after a quota error.**
- Quota resets at midnight **Pacific Time**
  (America/Los_Angeles) — not Pakistan midnight. Wait for the reset.
- The two buckets: 100 `videos.insert`/day (own bucket) and 10,000
  shared units/day; a full pipeline video (caption 400 + thumbnail
  50 + playlist 50) costs 500 shared units → 20 videos/day.

### Database unreachable

Symptom: Neon connection fails or times out.

- **Fail closed: stop, do not upload.** Without the database there
  is no kill-switch reading, no atomic claiming, and no quota
  accounting — uploading anyway would risk duplicates and quota
  overruns.
- Jobs already in the queue are safe; they resume when the database
  returns.
- Report the outage to the owner; check the Neon status and the
  restricted credentials (never paste credentials into a report).

## 7. What Meem never does

- Never uploads while the stop order stands or the kill switch is OFF.
- Never re-uploads an `upload_outcome_unknown` job without reconciliation.
- Never shares tokens or database credentials with any agent, file, chat, or GitHub.
- Never publishes a video by hand — publishing is automatic after
  assets verify, per the owner's standing rule (every video Public;
  no per-video approval, no scheduled publishing). Never flip a
  video's privacy outside the pipeline.
- Never sets `madeForKids` or `containsSyntheticMedia` to a fixed value — every video gets its own content assessment.
- Never uploads the same topic twice — the 🎬 Uploaded Videos page is the authority; check before uploading.

## 8. Quick reference

```bash
# Install (once)
git clone <repo-url>
cd youtube-api-upload
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Meem's OWN values; never commit .env

# Tests
pytest

# Dry-run (never touches YouTube)
DRY_RUN=true python -m youtube_uploader.cli dry-run --job <job-id>
```

Full install detail: `docs/INSTALL.md`. Owner setup steps:
`docs/USER_SETUP_GUIDE.md`. Security rules: `SECURITY.md`.
