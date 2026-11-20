# Security Policy — Sacred Knowledge YouTube API Uploader

## 1. Secret handling

Secrets in this project are: the OAuth **client secret**, each
agent's **refresh token**, each agent's **Neon database
credentials** (connection string), and any **authorization code**
issued during consent.

Rules — no exceptions:

1. **Secure Vault only.** Secrets live in each agent's Secure Vault
   and are injected as environment variables at runtime. They never
   appear in:
   - chat messages (including the authorization code — it goes
     straight to the Secure Vault via the secure flow),
   - files in this repository (`.env.example` holds placeholders
     only; `.env` is git-ignored and must never be committed),
   - logs or error messages (`config.redact()` masks secret keys;
     `Settings.__repr__` shows only set/unset),
   - GitHub (code, issues, coordination messages, or any file),
   - the Neon database (it holds job metadata, never credentials),
   - backups (job/quota data only).
2. **Per-agent secrets.** Each agent has its own refresh token and
   its own restricted Neon credentials. Agents never share, borrow,
   or copy each other's secrets.
3. **No secret is ever typed into a chat box.** If a setup step
   produces a code or token, it goes through the secure capture
   flow. If you are unsure, ask before pasting anything anywhere.
4. **`.gitignore` is the last line of defence**, not the first:
   `.env`, `*.pem`, `*.key`, `client_secret*.json`, `token*.json`,
   and `credentials*.json` are all ignored. The first defence is
   never creating such a file.

## 2. Least privilege

- **YouTube scopes:** `youtube.upload` + `youtube.force-ssl` only.
  The broad `youtube` scope (full account management) is never
  requested — the two narrow scopes cover uploads, thumbnails,
  captions, playlists, and `videos.update`.
- **Database users:** each agent's Neon user can only read and
  write the queue tables. No admin rights, no access to other
  databases.
- **OAuth client:** one Desktop client, shared project. Each agent's
  token is scoped to that client and revocable independently.

## 3. Kill switch and fail-closed behaviour

- `global_upload_enabled` in `system_settings` is the kill switch.
  **Default OFF.** Only the owner turns it ON.
- **Database unreachable = kill switch OFF.** Any failure to read
  the kill switch is treated as OFF. Uploads stop; nothing is
  retried blindly.
- `UPLOADS_FORCE_OFF=true` in the environment can only turn uploads
  OFF, never ON. There is no local override that enables uploads.
- The owner's **stop order** (15 November 2026) sits above the kill
  switch: while it stands, no upload of any kind happens, even if
  the kill switch were ON.
- `DRY_RUN` defaults to `true`. A dry-run never calls
  `videos.insert` or any other YouTube write endpoint.

## 4. Private-by-default uploads

- Every upload request carries `privacyStatus: private`
  explicitly. If `privacyStatus` were omitted, YouTube could default
  the video to Public — so omission is treated as a bug, not a
  default.
- Upload ≠ publish as steps, but publishing is automatic: once
  assets verify, the video goes Public per the owner's standing
  rule (owner order 20 Nov 2026) — no per-video publishing
  approval, no scheduled publishing. The safety gate is at upload
  time (kill switch + stop order), not at publish time.

## 5. Duplicate-upload honesty

YouTube provides no client idempotency key for `videos.insert`, so
duplicates cannot be made impossible by locking alone. This project
is honest about that:

- Atomic claiming + permanent Job IDs + file SHA-256 fingerprints
  reduce the risk.
- If an upload response is lost, the job goes to
  `upload_outcome_unknown` and is **never blindly re-uploaded**.
- Reconciliation checks YouTube for an existing video before any
  retry. If the outcome stays uncertain, a human decides.
- The 🎬 Uploaded Videos page is the authority on "never upload
  twice": the Notion sync refuses to record a video URL that is
  already there.

## 6. Audit logging

Every state transition, job claim, upload attempt, quota
reservation, and kill-switch read is recorded in the `audit_log`
table with the agent ID and timestamp. Logs contain job IDs and
video IDs only — never secrets, never credentials.

## 7. If a token is suspected leaked

Act immediately — a leaked token can upload to the channel until
revoked.

1. **Revoke it.** In the Google account that owns the channel:
   Security → Third-party access → remove the "Sacred Knowledge
   Uploads" access for the affected agent. This invalidates that
   agent's refresh token at once. Other agents' tokens are
   unaffected.
2. **Stop the affected agent's uploads** until it has a fresh token
   (the kill switch stays available as a global stop).
3. **Re-consent.** The owner clicks Allow again in their own browser
   for that agent; the new token goes straight into that agent's
   Secure Vault (see `docs/USER_SETUP_GUIDE.md` Phase 2).
4. **Rotate database credentials too** if there is any chance the
   leak included them (create a new restricted Neon user for the
   agent, update its Secure Vault, delete the old user).
5. **Report it.** Tell the owner what leaked, what was revoked, and
   what was rotated. Check the audit log for any upload attempts
   made with the leaked token.
6. **Remove the leaked copy** wherever it appeared (chat message,
   file, screenshot) as far as possible — and treat the secret as
   compromised even after removal.

## 8. Reporting security issues

Report any suspected vulnerability, leak, or unexpected upload
behaviour to the owner immediately, with:

- what happened and when,
- which agent / job ID / video ID is involved,
- the exact log lines (redacted — never include a secret),
- what has already been done (revoked, paused, rotated).

Do not test a suspected vulnerability against the live channel or
the production database. Use dry-run mode and mock tests.

## 9. Zero-spend rule

No paid service is used anywhere in this project. If any service
asks for a payment card, stop and report it — do not add a card.
Neon is used on its free plan only (limits re-verified on setup
day); the YouTube Data API is free within its quota.
