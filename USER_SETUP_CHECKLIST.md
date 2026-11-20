# USER SETUP CHECKLIST — Sacred Knowledge YouTube API

Owner setup for the YouTube API upload system. Statuses: NOT STARTED /
IN PROGRESS / COMPLETED / BLOCKED. Updated by Muse as each step is
verified — never assume a step is done until it is checked.

Last updated: 20 Nov 2026 ~22:40 PKT (Google side done except Production
publish — needs homepage + privacy policy URLs; Muse created them).

## Google Cloud

| # | Task | Status | Notes |
|---|------|--------|-------|
| 1 | Google Cloud project "Sacred Knowledge Uploads" created | COMPLETED | Owner created it; verified in Console 20 Nov. Project ID: sacredknowledgeuploads, number 685982142557 |
| 2 | YouTube Data API v3 enabled | COMPLETED | Already enabled (owner); verified in Enabled APIs list |
| 3 | OAuth consent screen (External), scopes youtube.upload + youtube.force-ssl only | COMPLETED | Screen already existed (app name "SacredKnowledgeUploadManager", owner chose it); scopes were NONE — Muse added exactly the two required scopes, saved 20 Nov |
| 4 | Consent screen published to Production | COMPLETED | Published 20 Nov ~22:40. Homepage = repo URL, privacy policy = GitHub Pages URL (both live; github.com + fileifypk.github.io added as Authorized domains). Google shows a verification banner ("Your app requires verification") — NOT submitted; for personal single-owner use the consent warning is bypassable, verification is optional pending owner decision |
| 5 | Desktop OAuth client "Sacred Knowledge Uploader" created | COMPLETED | Created 20 Nov via Console; Enabled. Client ID: 685982142557-edlj1t7e5jman8u8uk81v75agl2jejf3.apps.googleusercontent.com |
| 6 | Client Secret stored securely (VM .env, 600, gitignored — never chat/GitHub) | COMPLETED | Stored 20 Nov in ~/workspace/youtube-api-upload/.env (mode 600, git-ignored, verified not tracked) |

## Authorization (per-agent consent)

| # | Task | Status | Notes |
|---|------|--------|-------|
| 7 | Muse authorization (consent Allow + refresh token) | COMPLETED | Consent 20 Nov ~22:41 under fileifypk@gmail.com (both scopes shown, Allow clicked); code exchanged via auth.py; refresh token stored in .env (600) |
| 8 | Channel verification (channels.list mine=True = Sacred Knowledge) | COMPLETED | Verified live 20 Nov ~22:55 via verify_channel(): Sacred Knowledge, UCxOd_poXW1Yn_geTd8AqL4A, 79 subscribers |
| 9 | Meem authorization (own refresh token, own vault) | NOT STARTED | Later — Meem is on a separate machine; method to be decided with owner |

## Neon PostgreSQL

| # | Task | Status | Notes |
|---|------|--------|-------|
| 10 | Neon free account + project + database created | UNKNOWN | Owner has not said; to check — if missing, Muse creates via browser (Free plan, no card) |
| 11 | Restricted per-agent database users created | NOT STARTED | SQL prepared by Muse; privileged SQL via Neon SQL Editor |
| 12 | Free-plan limits + no-card requirement re-verified on setup day | NOT STARTED | Whatever Neon shows on setup day governs |

## Verification

| # | Task | Status | Notes |
|---|------|--------|-------|
| 13 | Live token refresh works | COMPLETED | refresh_access_token() verified live 20 Nov (3 successful refreshes during setup). Proxy fix applied same night: build_youtube_service is now proxy-aware (PySocks + egress CA bundle from SSL_CERT_FILE); 165/165 tests pass |
| 14 | Live DB migration + locking against real Neon | NOT STARTED | Setup day, after Neon credentials |
| 15 | Kill switch OFF confirmed in live DB | NOT STARTED | Must be OFF before any further step |

## Approvals (later — each needs the owner's separate explicit yes)

| # | Task | Status | Notes |
|---|------|--------|-------|
| 16 | Test upload (one private video) | NOT STARTED | Not assumed |
| 17 | 2–3 real videos | NOT STARTED | Not assumed |
| 18 | Stop order lifted | NOT STARTED | Stop order of 15 Nov 2026 still in force |
| 19 | Backlog migration | NOT STARTED | Not assumed |

## Rules

- No secret (client secret, authorization code, refresh token, DB
  password) ever goes into chat, GitHub, logs, or any shared file.
- No real YouTube upload of any kind (test included) without the
  owner's separate explicit approval. Kill switch stays OFF.
- The stop order sits above everything.
