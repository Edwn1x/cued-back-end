# Heartbeat Burn-In Calibration — Change Spec

Two changes, sequenced Item 2 → Item 1 (clear the confound, then diagnose/fix the
gap with it gone). Both are reversible and flag-gated; the founder remains the only
allowlisted heartbeat user. No deletion, no allowlist widening. See INVESTIGATION.md
for the trace this spec is built on.

---

## Decision (founder ratifies; proceeding on the default) — legacy vs heartbeat

Two proactive systems were live at once: the new heartbeat and the legacy templated
scheduler (morning briefing, pre/post-workout, evening wrap, weigh-in, meal
adherence). They double-message and make the burn-in speak-rate/cost data
meaningless (proactive texts the founder received were likely the *legacy* briefing,
not the heartbeat).

**Default recommendation, implemented:** disable the legacy proactive scheduler so
the heartbeat owns *all* proactive contact — that is the architecture the burn-in
exists to validate and the thing the retention bet rides on.

**Alternative (not taken):** consciously keep the morning briefing and scope the
heartbeat around non-briefing moments. If the founder prefers this, flip
`LEGACY_SCHEDULER_ENABLED=true` and we scope the heartbeat instead — surface it and
we adjust. Proceeding on disable unless redirected.

---

## Item 2 — disable the legacy proactive scheduler (reversible)

**What's there now.** `scheduler.py` registers, on boot and on every profile
update / onboarding-complete, per-user templated touchpoints via
`send_scheduled_message` (`coach.generate_scheduled_message`, legacy
`claude-sonnet-4-6`) plus a global `check_meal_adherence` job. All still fire.

**What it becomes.** New flag `LEGACY_SCHEDULER_ENABLED` (default `false`).
Defense-in-depth gating so no legacy proactive outbound can fire and re-enabling is
one flip:
- `schedule_user()` early-returns when off — registers no per-user jobs (covers all
  three callers).
- `send_scheduled_message()` early-returns when off — any legacy send is a no-op.
- `check_meal_adherence()` early-returns when off.
- `start_scheduler()` registers the global adherence job only when on.

**Kept running:** `daily_dining_scrape` (feeds context, no user outbound), the
heartbeat, nightly consolidation, episodic digest — each already on its own flag.

**Why.** One proactive system during burn-in; clean, meaningful speak-rate/cost
data; a single reversible switch (deletion is Phase 6, gated on a clean burn-in).

**Where.** `config.py` (flag), `scheduler.py` (four gates).

---

## Item 1 — fix the `unanswered_gap` deadlock

**What's there now.** `heartbeat.guardrail_reason` blocks a tick on
`engagement_tracker.has_unanswered_outbound`, which is **type-blind** and clears
only on a user inbound (or UTC-midnight rollover). So the coach's own *reactive
reply* — and every morning's legacy briefing — leaves an "unanswered outbound" that
mutes all proactive initiation until the user speaks. The heartbeat structurally
cannot initiate; it has spoken zero times. (Full trace: INVESTIGATION.md Item 1.)

**What it becomes.** A purpose-built anti-**stack** gate,
`engagement_tracker.has_unanswered_proactive(user_id, window_minutes)`: block only
when the **most recent** outbound is a **proactive** nudge
(`PROACTIVE_MESSAGE_TYPES = {"heartbeat"}`) that is **unanswered** and **within the
window**. The heartbeat guardrail uses it and returns reason `proactive_stack`.
- **Clears on time-elapse** (window expiry) with no user reply required — the
  deadlock is broken.
- **Reactive replies never gate** initiation (live-exchange freshness is already
  the `active_conversation` guardrail's job, on last-inbound age).
- **Legacy briefings never gate** it either (correct even if legacy is re-enabled).
- **Anti-stack preserved:** a second nudge within the window on an unanswered first
  is still suppressed.

Stays a code-enforced guardrail (never a prompt rule). `has_unanswered_outbound` is
**kept unchanged** for the legacy scheduler (reversibility).

**New knob.** `HEARTBEAT_STACK_WINDOW_MINUTES` (default 180 = 3h).

**Where.** `config.py` (flag), `engagement_tracker.py` (`has_unanswered_proactive`
+ `PROACTIVE_MESSAGE_TYPES`), `heartbeat.py` (guardrail swap; reason rename).

---

## Tests

**Tier 1 — Item 1** (`tests/tier1/test_heartbeat.py`): anti-stack blocks within
window (`proactive_stack`); gap clears after window with no user reply
(deadlock-regression + reachable clear condition); reactive reply doesn't wedge;
legacy briefing doesn't wedge; five `has_unanswered_proactive` unit cases
(within/past window, answered, reactive-type, no-outbound). Existing guardrails
(`active_conversation`, `quiet_hours`, `daily_budget`, `not_allowlisted`) still
green.

**Tier 1 — Item 2** (`tests/tier1/test_legacy_scheduler.py`): default off;
`send_scheduled_message` / `check_meal_adherence` no-op when off; `schedule_user`
registers no jobs when off and the expected job when on; `start_scheduler`
registers the heartbeat but neither the per-user nor the global legacy job when off.

**Tier 2** (`tests/tier2/test_heartbeat_proactive.py`):
`test_heartbeat_speaks_on_quiet_tick_after_deadlock_fix` — a scripted quiet period
with a real open thread and no stacking must let the coach speak. This is the proof
the burn-in has been missing.

---

## Deploy / rollout

1. Ship with `LEGACY_SCHEDULER_ENABLED=false` (default). Confirm on the founder's
   number: (a) an actual proactive heartbeat message on a quiet tick, (b) zero
   legacy briefings.
2. The burn-in speak-rate/cost clock meaningfully restarts here — prior data was
   confounded.
3. No allowlist widening and no deletion until the heartbeat has demonstrably
   initiated on its own.

**Reverse:** `LEGACY_SCHEDULER_ENABLED=true` restores the legacy scheduler;
`HEARTBEAT_STACK_WINDOW_MINUTES` tunes the anti-stack window. No data migration.
