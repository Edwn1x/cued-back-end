# Phase 4 — CHANGE SPEC (heartbeat)

Numbered *what's-there-now / what-it-becomes / why / where*. The heartbeat is a new
module behind `HEARTBEAT_ENABLED` (default off) + an allowlist; nothing legacy is
removed. Guardrails in code, decision in the model, every tick logged.

### 1. Heartbeat tick engine

- **Now:** proactive messages are templated (`coach.generate_scheduled_message`) fired
  by per-user `CronTrigger`s; no self-directed "should I say anything?" decision.
- **Becomes:** `heartbeat.py`. `heartbeat_tick(user_id)` runs guardrails in code, then
  (if clear) a single `decide(user_id)` call: reuse `build_loop_context` + a proactive
  header, offer a `stay_silent` tool, and treat any text as the SMS. `heartbeat_all()`
  iterates active allowlisted users on the tick. Legacy scheduler untouched.
- **Why:** one voice, one decision — "would a good coach text now?" — not a template.
  Default silent; speak only on something real (accountability, an open thread, a
  timely check-in).
- **Where:** `heartbeat.py` (new).

### 2. Guardrails (limits, not behavior)

- **Now:** `engagement_tracker` throttles legacy nudges by open-rate tier; `quiet_until`
  and `has_unanswered_outbound` already exist.
- **Becomes:** `guardrail_reason(user, session)` returns the first block or None, in
  code, before any model call: `not_allowlisted` → `quiet_hours` → `daily_budget`
  (`HEARTBEAT_MAX_PER_DAY` spoken ticks today) → `active_conversation`
  (`HEARTBEAT_ACTIVE_CONVO_MINUTES`) → `unanswered_gap` (`has_unanswered_outbound`).
  Blocked ticks log `guardrail:<reason>` and spend nothing.
- **Why:** a violating tick must never reach the model; the model composes freely when
  it speaks but can't talk past a limit. Cheapest checks first.
- **Where:** `heartbeat.py`, reusing `scheduler.has_unanswered_outbound`, `User.quiet_until`.

### 3. Tick log + anti-repetition (founder flag #1)

- **Now:** no record of proactive decisions; a re-derived thought would re-send.
- **Becomes:** `HeartbeatTick` (user_id, decided_at, spoke, reason, message) — one row
  per tick. `_proactive_context` feeds the last `HEARTBEAT_RECENT_TICKS` decisions **and**
  today's outbound back into the next tick, with a prompt rule: *silence is presumed if
  the thought was already sent or recently declined; don't open like your last texts.*
- **Why:** the tick must see its own history or it will nag. This is repetition control,
  separate from the daily cap (volume control).
- **Where:** `models.HeartbeatTick`, `migrate.py` (heartbeat_ticks + index), `heartbeat.py`.

### 4. Jittered scheduler wiring (founder flag #2)

- **Now:** `start_scheduler` adds cron touchpoints + adherence + dining scrape.
- **Becomes:** when `HEARTBEAT_ENABLED`, add `heartbeat_all` on
  `IntervalTrigger(minutes=HEARTBEAT_TICK_MINUTES)` with `jitter=HEARTBEAT_JITTER_SECONDS`
  (0–10 min), `coalesce=True`, `max_instances=1`.
- **Why:** an un-jittered interval lands on a predictable boundary — a bot tell. Jitter
  + message-shape variety (prompt) hide the clock. Coalesce/max_instances prevent stacking.
- **Where:** `scheduler.start_scheduler`.

### 5. web_search on the proactive path (founder flag #3)

- **Now:** web_search is reactive-only (Phase 3, inbound).
- **Becomes:** when `HEARTBEAT_WEB_SEARCH`, the decision call also carries the
  `web_search_20260209` server tool (`pause_turn` handled). Reactive search unchanged.
- **Why:** a proactive check-in sometimes needs a fresh fact ("your team plays tonight").
  Burn-in ships it ON to measure speak rate, then set a per-day budget; a searching tick
  prices ~$0.08 (Phase 3), not $0.007.
- **Where:** `heartbeat.decide`, `config.HEARTBEAT_WEB_SEARCH`.

### 6. Config flags (all `config.py`, default off/safe)

`HEARTBEAT_ENABLED` (off), `HEARTBEAT_ALLOWLIST` (empty → burn-in on founder number),
`HEARTBEAT_TICK_MINUTES` (45), `HEARTBEAT_JITTER_SECONDS` (600),
`HEARTBEAT_MAX_PER_DAY` (5), `HEARTBEAT_ACTIVE_CONVO_MINUTES` (30),
`HEARTBEAT_RECENT_TICKS` (8), `HEARTBEAT_WEB_SEARCH` (true, proactive burn-in only).

## Tests (tier-1, red-first → green)

`tests/tier1/test_heartbeat.py` — each guardrail blocks with no model call and no SMS
(allowlist / quiet / daily-cap / active-convo / unanswered); `decide` sends+logs on
text and stays silent+logs on the `stay_silent` tool; the proactive context carries
tick history + today's outbound + the stay_silent/web_search tools; `heartbeat_all`
is a no-op when disabled and ticks only allowlisted users. `test_migrations.py` gains
the `heartbeat_ticks` table + column assertions. Result: **91 passed, 0 xfails**.
