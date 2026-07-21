# Phase 4 — INVESTIGATION (heartbeat)

**Goal:** the coach can reach out on its own — a *dumb clock, a smart decision,
default silent*. Not a templated scheduler. One interval fires; per user a decision
call answers "would a genuinely good coach say something right now, or stay silent?"
Guardrails run in code first (a violating tick never reaches the model); every tick
is logged, and the log feeds the next tick so the coach can't re-send the same nudge.

Burn-in runs on the founder's number only (allowlist), on top of the live Phase 2/3
loop, with web_search ON on the proactive path — measure the speak rate, then set a
per-day budget.

## What exists today (legacy, stays until Phase 6)

- **`scheduler.py`** — APScheduler `BackgroundScheduler`, `CronTrigger` per-user
  templated touchpoints (`generate_scheduled_message` in `coach.py`), a global
  20:00 `check_meal_adherence`, a 05:30 dining scrape. This is the templated-nudge
  machinery the rewrite replaces; it is NOT wired to the new decision.
- **`engagement_tracker.py`** — `should_send` / `get_tier` / `increment_unanswered`
  (open-rate tiers throttle legacy nudges).
- **`has_unanswered_outbound(user_id)`** (scheduler.py:56) — True if the last
  outbound *today* has no reply. Reused directly as a guardrail.
- **`User.quiet_until`** (naive UTC) — set when the user says goodnight; suppresses
  outbound. Reused directly.
- **`send_sms`** (sms.py:57) — the chokepoint; GSM-7 normalize + split + outbound
  `Message` logging. The heartbeat sends through it (`message_type="heartbeat"`).

## Timestamp convention (verified against the disposable PG)

`Message.created_at` defaults to an **aware** `datetime.now(timezone.utc)`; the
column is `TIMESTAMP` (naive). psycopg converts the aware value to the DB session
TZ and strips it — so on a **UTC** server (prod/Railway) it stores naive **UTC**,
which is exactly what the existing readers assume (`has_unanswered_outbound`,
`get_active_meal` both do `.replace(tzinfo=utc)`). The local disposable cluster runs
in the machine TZ, so the same default round-trips as naive **local** — a test-only
artifact. Heartbeat follows the prod/UTC convention; `HeartbeatTick.decided_at`
defaults to `datetime.now(utc).replace(tzinfo=None)` (naive UTC, no conversion), and
tier-1 seeds explicit naive-UTC timestamps for time-sensitive rows.

## Design — three founder flags

1. **The tick must see its own history or it will nag.** Every tick writes a
   `HeartbeatTick` row (spoke + message, or silent + reason). The next tick's context
   carries the last N tick decisions (`HEARTBEAT_RECENT_TICKS=8`) **and** today's
   outbound messages, with an explicit prompt rule: *silence is presumed if the thought
   was already sent or recently declined-with-reason; never open like your last texts.*
   This is the anti-repetition signal, **distinct from** the daily cap (a hard
   guardrail). The cap bounds volume; the history bounds repetition.
2. **Jitter the clock.** APScheduler `IntervalTrigger(minutes=HEARTBEAT_TICK_MINUTES)`
   with `jitter=HEARTBEAT_JITTER_SECONDS` (0–10 min) so ticks never land on a
   predictable :00/:45 boundary — the message-shape tell. `coalesce=True`,
   `max_instances=1` so a slow tick can't stack.
3. **web_search on the proactive path (burn-in).** `HEARTBEAT_WEB_SEARCH=true` adds
   the server-side `web_search_20260209` tool to the decision call. Reactive
   (inbound) search is unchanged. Measure the speak rate live, then decide a per-day
   search budget. A searching tick prices at ~$0.08 (Phase 3 measurement), not $0.007.

## Guardrails (in code, before any model call)

Ordered, cheapest-first; the model can never talk past them:
`not_allowlisted` → `quiet_hours` (quiet_until) → `daily_budget`
(`HEARTBEAT_MAX_PER_DAY` spoken ticks today) → `active_conversation` (an inbound in
the last `HEARTBEAT_ACTIVE_CONVO_MINUTES` — obvious silence, don't interrupt a live
chat) → `unanswered_gap` (`has_unanswered_outbound` — don't pile on). A blocked tick
logs `guardrail:<reason>` and returns; no tokens spent.

## Decision call

Reuses `agent_loop.build_loop_context` (unified memory/events/split/meals — the same
world the reactive loop sees) plus a proactive header: time since last message,
today's outbound, tick history. System = cached voice prefix + volatile proactive
context. Tools: a `stay_silent` client tool (model calls it → silence + reason) and,
under the flag, web_search. **Speaking is the absence of stay_silent**: any text block
is the SMS to send. Bounded by `AGENT_LOOP_MAX_TOOL_ITERS`; `pause_turn` handled for
server search.

## Cost

A silent tick that reaches the model ≈ one standard input pass (voice cached);
guardrail-blocked ticks cost $0. Speaking/searching ticks price like Phase 3 search
(~$0.08). The daily cap + guardrails bound spend; burn-in measures the real speak rate.
