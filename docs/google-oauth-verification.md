# Google OAuth app verification — runbook (2026-10-02)

Project: **Text-Calendar** (Cloud project `text-calendar-451108`), OAuth client
`237200916367-…apps.googleusercontent.com`, consent screen **External / Testing**.
Until it's verified: every Google account must be on the test-users list (≤100) and
refresh tokens die after 7 days (see `connect_offers.py` and the memory note
"oauth-testing-mode-7-day-refresh-expiry"). Once verified: set
`GOOGLE_OAUTH_TESTING_MODE=false` in Railway and every account-first gate disappears.

## Scopes we request, and their class

| scope | used for | class |
|---|---|---|
| `calendar.calendarlist.readonly` | list the user's calendars (so secondary calendars sync) | sensitive |
| `calendar.events` (read/write) — only while `CALENDAR_WRITE_ENABLED` | read upcoming events; create ONE event when the user asks ("put a gym block tmrw at 4") | sensitive |
| `calendar.events.readonly` — only when write is off | read upcoming events | sensitive |
| `googlehealth.activity_and_fitness.readonly` | steps, active minutes, calories | **restricted** |
| `googlehealth.health_metrics_and_measurements.readonly` | resting HR, HRV, weight | **restricted** |
| `googlehealth.sleep.readonly` | sleep sessions | **restricted** |

Sensitive scopes → brand verification + scope justification + demo video. Reviews
typically take days to a few weeks. Restricted scopes additionally require an **annual
CASA security assessment** (App Defense Alliance; AL1/AL2 tier set by Google; third-party
assessor; budget hundreds to a few thousand dollars; weeks). It is "the final step of the
restricted scopes review" — everything else must be done first.

**Recommendation:** submit the **calendar** scopes now; leave Google Health in Testing
(it only matters for people with a Fitbit / Pixel Watch, all of whom can stay on the
test-users list) and do CASA when there's budget. If Google insists the whole client is
reviewed at once, move Google Health to a second OAuth client in a second project
(`GOOGLE_HEALTH_OAUTH_CLIENT_ID/SECRET` is a ~20-line change in `integrations/google_health.py`).

## What's already done (site + backend)

- Homepage https://cued.fit/ describes the product and links Privacy + Terms in the footer.
- Privacy policy https://cued.fit/privacy.html has a **Google User Data** section:
  Calendar + Google Health uses, encrypted token storage, 30-day deletion after
  disconnect, processors, and the **Limited Use** disclosure sentence (cued-site #21).
- Terms https://cued.fit/terms.html.
- Logo (120×120 PNG): https://cued.fit/images/oauth-logo-120.png (cued-site #20).
- Authorized domains already include `cued.fit` and `web-production-90171c.up.railway.app`
  (the OAuth redirect URI host).
- Write mode requests `calendar.events` + `calendarlist.readonly` only (no redundant
  readonly events scope) — this PR.

## Console steps (founder, ~30 minutes)

1. **Search Console** — https://search.google.com/search-console: add property
   `cued.fit` (Domain property, DNS TXT record at the registrar) with the SAME Google
   account that owns the Cloud project. Google requires ownership of every authorized
   domain. The Railway domain can't be verified; that's fine — remove it from Authorized
   domains if the review complains (the redirect URI itself stays on the OAuth client).
2. **Branding page**:
   - App name: `Cued`. Support email: as is.
   - Logo: upload `oauth-logo-120.png`.
   - Home page: `https://cued.fit/`
   - Privacy policy: `https://cued.fit/privacy.html`
   - Terms: `https://cued.fit/terms.html`
   - Authorized domains: keep `cued.fit`; delete `cloudworkstations.dev` and
     `google.com` (they're not ours and reviewers ask about every domain listed).
   - Save.
3. **Data Access page**: confirm the scopes listed are exactly the ones in the table
   above (add/remove so nothing extra is declared).
4. **Audience page** → **Publish app** → confirm. Status becomes "In production" with
   "Needs verification" (unverified apps show the "unverified app" warning but are no
   longer capped at 100 users and tokens stop expiring at 7 days).
5. **Verification Center** → Start verification. You'll be asked for:
   - **Scope justifications** — paste from the section below.
   - **Demo video** — unlisted YouTube link, script below.
   - Confirmation the privacy policy carries the Limited Use disclosure (it does).
6. Reply to the reviewer's email within a few days each round; they usually ask for one
   or two clarifications.
7. When approved: Railway → `GOOGLE_OAUTH_TESTING_MODE=false`, redeploy.

## Scope justifications (paste)

**calendar.calendarlist.readonly** — Cued is an AI fitness coach delivered over text
message. To plan workouts and meal timing around a student's day, the coach reads the
user's upcoming events. Many students keep classes on a secondary calendar (a shared
course calendar or a second calendar in the same account); `calendar.events.readonly`
alone cannot list calendars (calendarList.list returns 403), so this is the narrowest
scope that lets us find which calendars to read. We request no calendar settings or ACLs
(`calendar.readonly` would expose those).

**calendar.events** — The coach reads the next 60 days of events to (1) schedule
workouts in free windows, (2) avoid texting during classes and exams, and (3) plan meals
and check-ins around deadlines. The write half is used for exactly one user-facing action:
when the user asks the coach to put a training block on their calendar ("add gym tomorrow
at 4"), the coach reflects it back, waits for a yes, and creates that single event on the
primary calendar. We never modify or delete existing events. The narrower
`calendar.events.readonly` cannot create events; `calendar.events.owned` would not read
shared course calendars the user doesn't own.

(If asked about Google Health scopes before CASA is done, answer that they are in use only
for test users and will be submitted separately with the security assessment.)

## Demo video script (3–4 minutes, English, unlisted YouTube)

Record a phone screen (or a Mac Messages window) plus a browser. Show the app name
"Cued" and the logo at least once (the consent screen does it).

1. Open https://cued.fit/ — scroll once so the reviewer sees what the product is and
   the Privacy link in the footer. (10 s)
2. iMessage thread with the coach. Type: "can u see my google calendar? my week is
   packed". The coach replies and sends a one-tap link. (15 s)
3. Tap the link. The browser shows the **full Google consent screen**: app name Cued,
   the account chooser, the two calendar permissions with their descriptions. Pause so
   every scope is readable. Tap Allow. Show the "connected — head back to Messages"
   page. (30 s)
4. Back in Messages: the coach's "connected. i'll pull ur calendar in and plan around
   it" text. Then ask "what's my week look like" — the coach lists the calendar events it
   read (this demonstrates read use). (30 s)
5. Ask "put a gym block tomorrow at 4". Show the coach reflecting it back ("want me to
   add gym 4–5pm tomorrow?"), reply "yes", then open Google Calendar in the browser and
   show the new event (this demonstrates the single write use). (45 s)
6. Show that nothing else changed in the calendar (scroll the day). (10 s)
7. Open https://cued.fit/privacy.html and scroll to **Google User Data** so the Limited
   Use paragraph is on screen. (15 s)
8. Optional: in Messages, text "disconnect my calendar" and show the coach confirming,
   or show https://myaccount.google.com/permissions with Cued listed. (15 s)

Keep the consent screen in English. Make sure the scopes shown on the consent screen
match exactly what the project declares (Data Access page).

## After verification

- `GOOGLE_OAUTH_TESTING_MODE=false` — the coach stops asking for the Google account
  first and sends links directly; the admin "needs allowlisting" marker disappears.
- Existing test users keep working; their tokens stop expiring at 7 days after their
  next reconnect (the reconnect nudge handles the one remaining expiry).
- Annual: Google re-verifies restricted-scope apps every year (only matters once Health
  is submitted).
