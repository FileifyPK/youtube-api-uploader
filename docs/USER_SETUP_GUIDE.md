# Owner Setup Guide — YouTube API Uploads

A step-by-step guide for the owner of the Sacred Knowledge channel.
Everything here is a **one-time** setup. Once it is done, uploads run
without asking you for any code or click.

**Total time: about 45–60 minutes**, in one sitting or split across
two days.

**Cost: zero.** Every service used here is free. If any page asks for
a payment card, stop there and tell Muse — do not add a card.

**One safety rule for the whole guide:** an authorization code, a
refresh token, or a client secret must **never** be pasted into a
chat message, an email, or any file that gets shared. Tokens go
straight into the Secure Vault through the secure flow. If you are
ever unsure where a code should go, ask Muse before pasting it
anywhere.

Words used in this guide:

- **Google Cloud project** — a container in Google's console that
  holds the app's settings and quota. Think of it as the app's home.
- **API (YouTube Data API v3)** — the official way for a program to
  upload videos instead of a person clicking in a browser.
- **OAuth consent screen** — the Google page where you, the channel
  owner, approve what the app is allowed to do.
- **Scope** — a permission. We ask for only two narrow ones (upload
  videos; manage captions/playlists). We never ask for full account
  control.
- **Refresh token** — a long-lived key that lets the uploader act on
  your behalf after you approve it once. Each agent gets its own.
- **Neon** — a free hosted database service. It holds the shared job
  queue so Muse and Meem never upload the same video twice.
- **Secure Vault** — Muse's secure storage for secrets. Secrets live
  there and nowhere else.

---

## Phase 1 — Google Cloud project + API + consent screen

Do this in your own browser, signed in as the Google account that
owns the Sacred Knowledge channel.

### Step 1 — Create the project

1. Go to the Google Cloud Console (console.cloud.google.com).
2. Click the project dropdown at the top and choose **New Project**.
3. Name it exactly: **Sacred Knowledge Uploads**.
4. Click **Create** and wait for it to be selected.

### Step 2 — Enable the YouTube Data API

1. In the left menu, open **APIs & Services → Library**.
2. Search for **YouTube Data API v3** and open it.
3. Click **Enable**.
4. (Optional check: **APIs & Services → Dashboard** should now list
   YouTube Data API v3 as enabled.)

### Step 3 — OAuth consent screen

1. Open **APIs & Services → OAuth consent screen**.
2. Choose **External** as the user type and click **Create**.
   (External means any Google account can consent — the app is not
   limited to one organisation.)
3. Fill in the app details:
   - App name: **Sacred Knowledge Uploads**
   - User support email: your email
   - Developer contact email: your email
4. Click **Save and Continue** through the scopes step, adding
   exactly these two scopes:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube.force-ssl`
   
   Do not add the broad `youtube` scope — it is not needed.
5. On the summary page, click **Publish App** to move the app to
   **Production**.
   
   **Why Production matters:** an app left in Testing mode has its
   tokens expire after 7 days. Production tokens do not.
   
   **If Google asks for verification:** complete the verification
   process — do not ignore it. Publishing the app and completing
   verification are two different things; both matter.

### Step 4 — Desktop OAuth client

1. Open **APIs & Services → Credentials**.
2. Click **Create Credentials → OAuth client ID**.
3. Application type: **Desktop app**. Name it
   **Sacred Knowledge Uploader**.
4. Click **Create**. Google shows a Client ID and a Client Secret.
5. Save the **Client Secret straight into the Secure Vault** (Muse
   will open the secure capture page for you). Do not paste it into
   chat, and do not save it in a file.

That is Phase 1 done — about 15–20 minutes.

---

## Phase 2 — Per-agent consent (one click per agent)

Each agent needs its own refresh token. The owner gives consent once
per agent, in the owner's own browser.

1. Muse will give you a sign-in link for the agent (first Muse,
   then Meem, and a third agent later if one joins).
2. Open the link **in your own browser**, signed in as the channel
   owner.
3. Google shows what the app is asking for (the two scopes from
   Phase 1). Click **Allow**.
4. Google returns an authorization code. **Do not copy it into
   chat.** The code goes through the secure flow straight into that
   agent's Secure Vault, where it is exchanged for the agent's
   refresh token.
5. Repeat for each agent. Meem's token lives in Meem's own Secure
   Vault — never in Muse's, never on GitHub, never in chat.

If consent ever needs to be redone (for example a token is revoked),
the same steps apply: owner clicks Allow in their own browser, token
goes straight to the Secure Vault.

---

## Phase 3 — Neon free account + database

1. Create a free account at Neon (neon.tech). Choose the **Free**
   plan.
2. Create a project and a database for the upload queue (name it
   something clear, e.g. `sacred-knowledge-uploads`).
3. Create **separate restricted database credentials for each
   agent** (one database user for Muse, one for Meem). "Restricted"
   means each user can only read and write the queue tables — no
   admin rights, no access to anything else.
4. Save each agent's connection details in **that agent's own
   Secure Vault** (Muse's in Muse's vault, Meem's in Meem's vault).
   Never paste connection strings into chat or GitHub.
5. **On setup day, re-verify the free plan:** confirm the Free plan
   still requires no payment card and note its current limits
   (storage, compute hours). Neon may change its free plan over
   time — whatever it is on setup day is what we plan around. There
   is no automatic paid upgrade, ever.

That is Phase 3 done — about 10 minutes.

---

## Phase 4 — Approvals that are still needed later

None of these is assumed. Each one needs your separate explicit
"yes" at the time — Muse will ask, and will not proceed without it.

1. **Test upload** — one private test video, only after the build is
   complete and all tests pass. It will be verified (metadata,
   caption, thumbnail, playlist) and then deleted or left private.
2. **2–3 real videos** — the first real uploads through the API,
   with the full flow verified end to end.
3. **Lifting the stop order** — the global stop order of 15 November
   2026 stays in force until you say otherwise. Nothing uploads
   while it stands, test included.
4. **Backlog migration** — the 80+ held videos, uploaded in queue
   order, a few per day (paced to the channel's daily limits — never
   all at once).

Until you give those approvals, the system can only run dry-runs
(simulated uploads that never touch YouTube).

---

## Checklist (from the plan, §3)

- [ ] 1. Google Cloud project **Sacred Knowledge Uploads** created.
- [ ] 2. YouTube Data API v3 enabled.
- [ ] 3. OAuth consent screen created (External), scopes
      `youtube.upload` + `youtube.force-ssl` only, published to
      **Production**; Google verification completed if asked.
- [ ] 4. Desktop OAuth client created; Client Secret in Secure Vault.
- [ ] 5. Consent Allow clicked for Muse (own browser; code/token
      straight to Secure Vault, never chat).
- [ ] 6. Consent Allow clicked for Meem (own browser; token to
      Meem's own Secure Vault, never GitHub/chat).
- [ ] 7. Neon free account + database created; restricted per-agent
      credentials saved in each agent's Secure Vault; free-plan
      limits and no-card requirement re-verified on setup day.
- [ ] 8. Test upload approved (later — separate explicit yes).
- [ ] 9. Stop order lifted (later — separate explicit yes).
- [ ] 10. Backlog migration approved (later — separate explicit yes).

## If something goes wrong

- **A page asks for a payment card:** stop, do not add it, tell Muse.
- **A token stops working:** tell Muse which agent is affected; the
  fix is a fresh consent click (Phase 2) for that agent only.
- **You pasted a code or token into chat by mistake:** tell Muse
  immediately so it can be revoked and replaced. Tokens can always
  be revoked in your Google account (Security → Third-party access)
  and re-issued.
- **You are unsure about any step:** ask Muse before clicking. There
  is no deadline on setup.
