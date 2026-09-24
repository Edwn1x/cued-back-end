# Integrations Part 2a — Fitbit / Pixel Watch via the Google Health API (wearable read)

**Status:** BUILT, flag-gated OFF. 2026-09-24.
**Branch:** `fitbit-integration` (PR #111). Predecessors: Part 0 (framework, #106), Part 1 (gcal), Part 1.4 (bCourses/Canvas, #107).

## 0. Why, and why the API changed mid-build

The founder asked "now that i have google connected, can i connect my google fitbit air?"
The first cut of this PR was built on the legacy Fitbit Web API (dev.fitbit.com). The
founder then pasted a note that it is being turned down; verified the same day:

- dev.fitbit.com's Web API landing page now says: "We will be deprecating the legacy
  Fitbit Web API in September 2026" (turndown 2026-09-30, no new developer accounts).
- Its successor is the **Google Health API** (developers.google.com/health, launched
  2026-03-24, GA): same data, REST at `health.googleapis.com/v4`, **Google OAuth 2.0** —
  which means the SAME Cloud project + OAuth client the calendar connect already uses.
- Google Fit's REST API is also gone; Health Connect is on-device only. So the Google
  Health API is the only server path for a Fitbit Air / Pixel Watch, and it is the one
  this PR now targets. The Fitbit Web API code was deleted, not kept as a fallback —
  it dies in six days.

What the coach gets, in priority order: last night's sleep (+7-day avg), steps today
(+7-day avg), resting HR + HRV vs a 7-day baseline, and scale readings into
`weight_logs` so the adaptive-targets loop sees them without a text.

## 1. Facts verified against developers.google.com/health (2026-09-24)

- Auth: standard Google OAuth (accounts.google.com/o/oauth2/v2/auth, oauth2.googleapis.com/token).
  Same client id/secret as gcal; the Health API must be **enabled on that Cloud project**
  and its scopes added on the consent screen (Data Access page). Refresh tokens do not rotate.
- Scopes (RESTRICTED — testing mode with listed test users ≤100 works unverified; a public
  launch needs OAuth verification + an annual CASA assessment, $500–4,500):
  `googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`,
  `googlehealth.sleep.readonly`. Not nutrition / location / profile / settings.
- **Never send `include_granted_scopes=true`** on this provider: any legacy `fitness.*`
  grant on the client gets unioned into the token and Health calls 403. (gcal keeps it.)
- Workspace (school/work) Google accounts are blocked; personal gmail only.
- Identity: `GET /v4/users/me/identity` → `healthUserId` (the webhook payload key) and
  `legacyUserId` (old Fitbit id).
- Reads (all `users/me`):
  - `POST …/dataTypes/{steps|total-calories|active-zone-minutes}/dataPoints:dailyRollUp`
    body `{range:{start:{date},end:{date}}, windowSizeDays:1, pageSize:N, dataSourceFamily}`;
    `end` is EXCLUSIVE; duration = windowSizeDays × pageSize ≤ 90 days (14 for total-calories,
    heart-rate, active-minutes) else `INVALID_ROLLUP_QUERY_DURATION`. Response
    `rollupDataPoints[].{civilStartTime.date, steps.countSum | totalCalories.kcalSum |
    activeZoneMinutes.sumIn{FatBurn,Cardio,Peak}HeartZone}`; int64s arrive as strings.
  - `GET …/dataTypes/daily-resting-heart-rate/dataPoints?filter=daily_resting_heart_rate.date >= "YYYY-MM-DD"`
    → `dataPoints[].dailyRestingHeartRate.{date, beatsPerMinute}`; same shape for
    `daily-heart-rate-variability` → `averageHeartRateVariabilityMilliseconds`.
  - `GET …/dataTypes/sleep/dataPoints:reconcile?dataSourceFamily=…/google-wearables&filter=sleep.interval.civil_end_time >= "…"`
    → `dataPoints[].sleep.{interval:{startTime,endTime,startUtcOffset,endUtcOffset}, type: MAIN_SLEEP|NAP, summary:{minutesAsleep,minutesAwake}}`.
  - `GET …/dataTypes/weight/dataPoints?filter=weight.sample_time.physical_time >= "…Z"`
    → `dataPoints[].{name, weight:{sampleTime:{physicalTime}, weightKg}}` (the REST page
    also documents `weightGrams`; the client accepts either).
- Webhooks: PROJECT-level subscriber (`POST /v4/projects/{number}/subscribers`, needs
  `cloud-platform` scope + `health.subscribers.create` IAM — the project owner, not a
  user token). Google verifies the endpoint during the call: POST `{"type":"verification"}`
  WITH the `endpointAuthorization.secret` in `Authorization` → expect 200/201, and one
  WITHOUT → expect 401/403. Notifications are `{"data":{healthUserId, dataType, operation,
  intervals}}` or a JSON array of them; must answer **204** or Google retries with backoff
  for 7 days. `subscriptionCreatePolicy: AUTOMATIC` subscribes every consenting user.
  Signed with ECDSA P-256 (`GOOGLE-HEALTH-API-SIGNATURE`, public keyset at gstatic) — we
  rely on the secret header, not the signature, in this part.
- Rate limits: not documented. A sync is 7 calls.

## 2. User-visible behavior

- The coach offers it the same way it offers gcal: one clause, once, when the user
  mentions their Fitbit / watch / sleep / steps and INTEGRATIONS doesn't show it. On the
  ask it fires `send_connect_link(provider="google_health")` → link bubble → Google
  consent screen (health scopes) → "connected. i'll see ur sleep, steps and heart rate
  from here". The user says "fitbit"; the tool value is `google_health` (live anchor 3/3).
- The coach's context gains a `## WEARABLE (fitbit)` block (loop AND heartbeat):

  ```
  ## WEARABLE (fitbit)
  last night: 6h12m (11:48pm–6:31am), 7-day avg 6h40m
  steps today: 4,210 so far · 7-day avg 8,900
  resting HR: 58 (7-day avg 55) · HRV 34ms (7-day avg 41ms) — HR and HRV worse than their baseline
  synced 12 min ago
  Context to act on, never a readout: … Never diagnose from HR/HRV … Scale weight is in WEIGHT.
  ```

  Rendered only when there is a row from the last 3 days; a stale connection says so in
  one line. Nothing is texted on sync.
- Scale readings become `weight_logs` rows (note `ghealth:<dataPoint name>`, idempotent)
  and update `users.weight_lbs` with the same latest-wins + protein-follow rule as `log_weight`.

## 3. Design

### 3.1 Provider (`integrations/google_health.py`)
`GoogleHealthProvider(Provider)`: name `google_health`, label `fitbit`. gcal's OAuth shape
minus `include_granted_scopes`. `external_id` = `healthUserId`. `sync_now` = the first
backfill pull. Plus the thin read client (§1) with typed `HealthAPIError(status)`.

### 3.2 Sync (`integrations/google_health_sync.py`)
Table `wearable_days` — one row per (user, provider, local day): steps, calories_out,
active_minutes (AZM), resting_hr, hrv_rmssd, sleep_minutes, sleep_start/end (naive UTC),
sleep_efficiency (asleep/(asleep+awake)), synced_at. `UNIQUE(user_id, provider, day)`.
Sleep is keyed by the LOCAL date the session ENDS (using the API's `endUtcOffset`); only
`MAIN_SLEEP` counts (a NAP never overwrites the night).

`sync_user(user_id, *, days=None)`: window = last `days` local days (default 2; first sync
= `GOOGLE_HEALTH_BACKFILL_DAYS`, 14). 401 → `mark_revoked` (status line shows
"google_health disconnected" so the coach can re-offer); anything else →
`note_sync_failure` (3 misses → `error`, heals on the next good pull). `sync_all()` every
30 min over connected + error rows.

### 3.3 Webhook (`integrations/routes.py`)
`POST /oauth/google_health/webhook`: `Authorization` must equal
`GOOGLE_HEALTH_WEBHOOK_SECRET` (else 401 — that IS the verification handshake's second
half); `{"type":"verification"}` → 201; anything else → 204 immediately and
`handle_notifications` maps healthUserId(s) → users and syncs off-thread. Registration
is a one-off founder script: `scripts/register_google_health_subscriber.py`.

### 3.4 Coach surface
`send_connect_link` enum `google_health`; `## WEARABLE` block next to WEIGHT in
`agent_loop` (heartbeat inherits); INTEGRATIONS status-line gate includes the flag;
`capabilities.connect_accounts` names fitbit / pixel watch; `prompts/voice.md` Wearable
rule (act on it, never recite, never diagnose; "can i connect my fitbit?" = send the
link; personal gmail only); admin `/admin/system` job row.

### 3.5 Flags / env (all default off/empty)
`GOOGLE_HEALTH_ENABLED`, `GOOGLE_HEALTH_WEBHOOK_SECRET`, `GOOGLE_HEALTH_BACKFILL_DAYS` (14),
`GOOGLE_HEALTH_SYNC_DAYS` (2). Reuses `GOOGLE_OAUTH_CLIENT_ID/SECRET` and the existing
`SEND_CONNECT_LINK_TOOL_ENABLED`. Migration: `CREATE TABLE IF NOT EXISTS wearable_days` only.

## 4. Not in this part
- Webhook signature verification (ECDSA) — the shared secret gates the route; add Tink
  verification if the endpoint ever becomes public knowledge.
- Intraday / per-minute data, exercise sessions (`exercise` data type → a workout log
  hook is the obvious Part 2c), nutrition.
- OAuth verification + CASA — required before the 101st user. Testing mode covers the beta.
- Strava (Part 2b). A "poor sleep" heartbeat standing condition (the block already
  reaches the heartbeat; add code only if live ticks ignore it).

## 5. Verification
- Tier-1 (DONE): provider (Google endpoint, health scopes, no include_granted_scopes,
  identity → external_id, refresh keeps token), client (rollup exclusive end + 14-day cap,
  string int64s, pagination, sleep reconcile, weight kg/grams), sync (backfill vs steady
  window, MAIN_SLEEP only, efficiency, idempotent weight, 3-miss error + heal, 401 →
  revoked), webhook (handshake 201/401, batched + single payloads, 204 always, unknown
  ids ignored), tool enum + flag gate, capability rule, context block + loop context.
- Live anchor (DONE, 3/3 × 2 fixtures, `tests/tier2/test_google_health_connect_live.py`):
  with gcal already connected, "can i connect my google fitbit air?" fires
  `send_connect_link(google_health)` in the same turn; the model unprompted added
  "personal gmail only" once — correct, from the voice rule.
- Live (owed): founder connects as a listed test user; check `GOOGLE_HEALTH_SYNC user=31`,
  `wearable_days` rows, WEARABLE block in the next reply's context, then the webhook
  registration script → a notification within minutes of a watch sync.

## 6. Founder steps
In the PR handoff message (Cloud console: enable Google Health API, add the three scopes,
add yourself as a test user; Railway: two vars; merge; text "connect my fitbit"; later:
run the subscriber script).
