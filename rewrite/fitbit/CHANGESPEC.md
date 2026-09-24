# Integrations Part 2a — Fitbit (wearable read)

**Status:** BUILT, flag-gated OFF. 2026-09-24.
**Branch:** `fitbit-integration`. Predecessors: Part 0 (framework, #106), Part 1 (gcal), Part 1.4 (bCourses/Canvas, #107).

## 0. Why

The founder asked "now that i have google connected, can i connect my fitbit?" The Google
connect only granted calendar scopes. Fitbit accounts sign in with Google, but the Fitbit
Web API is its own OAuth server with its own scopes and its own registered app, so it is a
second one-tap link, not a scope bump on the gcal grant. Google Fit's REST API is shut
down and Health Connect is on-device only, so the Fitbit Web API is the only server path.

What the coach gets from it, in priority order:

1. **Sleep** — last night's duration + start/end, plus a 7-day average. Today the coach only
   has the sign-up self-report (`sleep_quality`) and whatever the user types.
2. **Steps** — today so far and the 7-day average. Replaces the onboarding guess in
   `users.avg_steps` as the activity input over time.
3. **Resting heart rate + HRV** — a 7-day baseline and today's deviation. This is the
   readiness signal the readiness skill already knows how to interpret ("HRV down 15%").
4. **Weight** — Fitbit scale readings (Aria, or manual entries in the Fitbit app) land in
   `weight_logs` so the adaptive-targets loop sees them without a text.

## 1. Facts verified against dev.fitbit.com (2026-09-24)

- Authorize: `https://www.fitbit.com/oauth2/authorize`. Token: `https://api.fitbit.com/oauth2/token`.
- PKCE (S256) is required for Personal/Client apps and recommended for Server apps → we
  always send it. Client auth on the token endpoint: HTTP Basic `client_id:client_secret`.
- Access token 8h. **Refresh tokens rotate: single-use, a new one comes back on every
  refresh.** (gcal's do not — base already persists a returned refresh token, so this
  works, but a lost write = a dead connection. See §3.4.)
- Scopes we request: `activity heartrate sleep weight profile`. Not `nutrition`
  (Cued is the food log), not `location`/`social`/`settings`.
- App types: **Personal** = only the developer's own account, intraday automatic.
  **Server** = other users, intraday only by application. We use DAILY summaries only
  (no intraday), which every type gets without approval.
- Rate limit: 150 requests/hour/user. A sync is ≤7 calls (§3.2).
- Subscriptions: verify GET `?verify=<code>` → 204 if it matches, 404 if not, within 5s.
  Notifications POST a JSON list of `{collectionType, date, ownerId, ownerType,
  subscriptionId}`; respond 204 within 5s and fetch later.
- Endpoints (all under `https://api.fitbit.com`, `user/-` = the token's owner):
  - steps series `GET /1/user/-/activities/steps/date/{start}/{end}.json` → `activities-steps[]{dateTime,value}`
  - RHR series `GET /1/user/-/activities/heart/date/{start}/{end}.json` → `activities-heart[]{dateTime,value.restingHeartRate}`
  - sleep range `GET /1.2/user/-/sleep/date/{start}/{end}.json` (≤100 days) → `sleep[]{dateOfSleep,isMainSleep,minutesAsleep,startTime,endTime,efficiency,levels.summary}`
  - HRV range `GET /1/user/-/hrv/date/{start}/{end}.json` → `hrv[]{dateTime,value.dailyRmssd}`
  - activity summary (today only) `GET /1/user/-/activities/date/{date}.json` → `summary.{caloriesOut,veryActiveMinutes,fairlyActiveMinutes}`
  - weight log (per day) `GET /1/user/-/body/log/weight/date/{date}.json` with
    `Accept-Language: en_US` → pounds; `weight[]{logId,weight,date,time,source}`
  - subscribe `POST /1/user/-/{activities|sleep|body}/apiSubscriptions/{id}.json` → 201 new / 200 exists / 409 conflict

## 2. User-visible behavior

- The coach offers Fitbit the same way it offers gcal: one clause, once, when the user
  mentions sleep/steps/their watch/Fitbit and INTEGRATIONS doesn't show it. On "yeah" it
  fires `send_connect_link(provider="fitbit")` → link bubble → Fitbit consent screen
  (Google sign-in) → "connected. i'll see ur sleep, steps and heart rate from here".
- The coach's context gains a `## WEARABLE (fitbit)` block (agent loop AND heartbeat,
  since the heartbeat wraps the loop context):

  ```
  ## WEARABLE (fitbit)
  last night: 6h12m (11:48pm–6:31am), 7-day avg 6h40m
  steps today: 4,210 so far · 7-day avg 8,900
  resting HR: 58 (7-day avg 55) · HRV 34ms (7-day avg 41ms) — both worse than baseline
  synced 12 min ago. Context to act on, never a readout: one number only when it changes
  the plan (short night → lighter session / earlier bed; low steps on a rest day → walk).
  Never diagnose from HR/HRV. Weight from the scale is in WEIGHT.
  ```

  Rendered only when there is at least one row from the last 3 days; a stale connection
  says so in one line.
- Fitbit scale readings become `weight_logs` rows (note `fitbit:<logId>`, idempotent) and
  update `users.weight_lbs` via the same latest-wins rule as `log_weight`. The WEIGHT block
  and the adaptive-targets cycle pick them up with no special-casing.
- Nothing is texted on sync. Sleep/steps/HR are context for the heartbeat's existing
  standing conditions, not a new nag.

## 3. Design

### 3.1 Provider (`integrations/fitbit.py`)
`FitbitProvider(Provider)`: name `fitbit`, label `fitbit`, scopes above. PKCE verifier is
derived, not stored: `verifier = b64url(HMAC(connect_secret, "pkce:" + state))` (43 chars),
so `authorize_url(state, redirect_uri)` and `exchange_code(code, redirect_uri, state=…)`
recompute it from the same `state` without a new column. **Framework change:** base
`Provider.exchange_code` gains an optional `state` kwarg; routes pass it. gcal ignores it.
`external_id` = Fitbit `user_id` from the token response (the subscription `ownerId`).
`sync_now` = subscribe to activities/sleep/body (best-effort) + first pull (backfill).

### 3.2 Sync (`integrations/fitbit_sync.py`)
New table `wearable_days` — one row per (user, provider, local day):
steps, calories_out, active_minutes, resting_hr, hrv_rmssd, sleep_minutes, sleep_start,
sleep_end (naive UTC), sleep_efficiency, synced_at. `UNIQUE(user_id, provider, day)`.
Sleep is keyed by `dateOfSleep` (the morning it ends) and only `isMainSleep` logs count.

`sync_user(user_id, *, days=None)`: window = last `days` local days (default 2:
today + yesterday; first sync = `FITBIT_BACKFILL_DAYS`, default 14). Calls: steps series,
RHR series, sleep range, HRV range (4), today's activity summary (1), weight log per
day in the window (2 normally; 14 on the first sync). Upserts rows; inserts WeightLog for
unseen `logId`s. `note_sync_success/failure` from base (3 misses → error, heals on next).

`sync_all()`: every 30 min for every connected user (poll floor; subscriptions make it
fresher). Per-user ≤7 calls/30 min = well under 150/h.

### 3.3 Subscriptions (`integrations/routes.py`)
`GET /oauth/fitbit/subscriber?verify=…` → 204/404 against `FITBIT_SUBSCRIBER_VERIFY_CODE`.
`POST /oauth/fitbit/subscriber` → parse the list, map `ownerId` → `integrations.external_id`,
kick `sync_user` for each distinct user on a daemon thread, return 204 immediately.
Unknown owners are ignored. The route is outside `/admin` so basic auth doesn't gate it.

### 3.4 Refresh-token rotation
Fitbit invalidates the old refresh token on use. `base.get_valid_access_token` already
persists a returned refresh token. Two guards: `sync_all` runs `max_instances=1` so two
refreshes can't race in one process, and a refresh HTTP failure marks the row revoked
(existing behaviour) so the coach says "fitbit disconnected" once and can offer the link
again rather than silently pulling stale data.

### 3.5 Coach surface
- `send_connect_link` enum + flag map gain `fitbit`.
- `agent_loop` context: `## WEARABLE (fitbit)` block next to WEIGHT (§2), and the 5c
  INTEGRATIONS status line's flag gate includes `FITBIT_ENABLED`.
- `capabilities.connect_accounts` names Fitbit; reveal_when adds sleep/steps/watch.
- `prompts/voice.md`: a Wearable rule under Calendar — act on it, don't recite it; the
  connect offer line names the one-tap link; no diagnosing from HR.
- admin `/admin/system` job row "Fitbit Sync".

### 3.6 Flags / env (all default off/empty)
`FITBIT_ENABLED`, `FITBIT_CLIENT_ID`, `FITBIT_CLIENT_SECRET`, `FITBIT_SUBSCRIBER_VERIFY_CODE`,
`FITBIT_BACKFILL_DAYS` (14), `FITBIT_SYNC_DAYS` (2). The connect tool needs the existing
`SEND_CONNECT_LINK_TOOL_ENABLED` (already on in prod for gcal).

## 4. Not in this part
- Intraday (minute-level HR/steps) — needs Fitbit approval for a Server app; daily is enough
  for coaching decisions.
- Writing to Fitbit (meals, weight) — Cued is the source of truth for food; no reason.
- Strava (Part 2b) — separate provider, same framework.
- A "poor sleep" heartbeat standing condition — the block reaches the heartbeat already;
  add a code-computed signal only if live ticks show the model ignoring it.
- Readiness legacy agent (`agents/readiness.py`) — unchanged; the loop is the live surface.

## 5. Verification
- Tier-1 (DONE, 2026-09-24): provider (PKCE, Basic auth, rotation persisted through
  base), sync (mocked API → rows, idempotent weight logs, main-sleep-only, backfill vs
  steady window, 3-miss error + heal, 401 → revoked), subscriber routes (verify 204/404,
  POST 204 + fan-out by ownerId), tool enum + flag gate, capability coverage rule, context
  block rendering + reaches build_loop_context. Full tier-1: 1114 passed.
- Live anchor (DONE, 3/3 × 2 fixtures): `tests/tier2/test_fitbit_connect_live.py` — with
  gcal already connected, the founder's verbatim ask fires send_connect_link(fitbit) in
  the same turn. First run was 2/3: one reply answered "want me to send it" instead of
  sending; fixed by naming that case in the tool description ("can i connect my fitbit?"
  IS the ask) + the voice rule. 3/3 after.
- Live: founder connects with a Personal-type Fitbit app first (§6). Check
  `FITBIT_SYNC user=31 …` in logs, `select * from wearable_days where user_id=31`, and that
  the next reply's context carries the WEARABLE block (admin context preview).

## 6. Founder steps (exact)
See the handoff message; summarized: create the app at dev.fitbit.com/apps (Personal type
for the first run; OAuth 2.0 Application Type = Server if others should connect later),
callback `https://web-production-90171c.up.railway.app/oauth/fitbit/callback`, subscriber
endpoint `https://web-production-90171c.up.railway.app/oauth/fitbit/subscriber` with a
verification code you choose; set the five Railway vars; redeploy; text "connect my fitbit".
