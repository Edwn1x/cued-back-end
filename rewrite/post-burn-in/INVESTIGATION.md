# Post-burn-in follow-ups — INVESTIGATION

Four items from the founder's spec, investigated against the current merged `main`
(PRs #13/#14 in), not against the spec's own framing of them.

## 0. Spec references that don't exist

The spec says "Read `ROADMAP.md` and `DESIGN_SPEC.md` Part I before starting." Neither
file exists anywhere in this repo (checked root and recursively). The equivalent
context lives in `README.md` (architecture, flags, rollout order) and the per-phase
`rewrite/phase-*/{INVESTIGATION,CHANGESPEC,SUMMARY}.md` docs — used those instead.
Flagging rather than guessing at stale filenames.

## 1. Deploy hardening: migrations at boot

No `railway.toml`/`railway.json` in the repo — Railway drives off the bare `Procfile`
(`web: python app.py`). So the only deploy-time lever is the `Procfile` itself.

The bigger finding: `migrate.py.run_migrations()` is not actually safe to gate boot on
today. It wraps every statement in try/except and treats ANY exception the same way
unless the message contains "already exists" — genuine failures are logged as
`FAILED: ...` and the loop just continues; the function returns normally and the
script exits 0. That means **today, right now, a broken migration would NOT block
boot** — it would silently half-apply and let the app start against a stale schema.
This is worse than the spec's own framing ("migrations run manually after an
auto-deploy") — it's a latent bug independent of the boot-migration work, just masked
because migrations have so far always succeeded in practice.

Fix has two independent parts: (a) make `run_migrations()` raise on genuine failures
while still swallowing "already exists" (the actual idempotency the whole design relies
on), and (b) chain it into the `Procfile` so a raise actually blocks boot. Doing only
(b) without (a) would have shipped a `&&` that can never fire.

## 2. Event edit — move to a different day

Checked whether a date change is already representable through `manage_log`'s edit
field-map, per the spec's own "investigate first, don't assume" instruction. It is
**not**. `agent_tools._EDIT_FIELDS["event"]`'s `event_time` kind (the burn-in-fixes
PR) explicitly derives the edit's target day from the row's **current** `occurred_at`
— "keep day, edit clock" was the deliberate, tested behavior (`test_event_edit_keeps_the_day`).
There is no code path that changes which day an event's `occurred_at`/`ends_at` fall on.
So this is the "small extension" fork the spec calls for, not a test-and-surface job.

Design constraint surfaced during implementation: a combined single-call edit like
`{"date": "2026-07-25", "starts_at": "15:30"}` only lands correctly if the date move is
applied to the row BEFORE the time move is computed, because the existing `event_time`
handling reads its target day off the row's current `occurred_at`. This ordering is
enforced with an explicit sort key (not dict-insertion order, which isn't guaranteed
semantics) and pinned with a dedicated test
(`test_event_combined_date_and_time_edit_lands_on_new_day`) — an invisible ordering
assumption should not be provable only by accident of iteration order.

## 3. One-time timestamp audit

Read-only by construction; the spec doesn't ask for a "wrongness" heuristic, just
visibility through the corrected `timefmt` boundary so a human can eyeball it. No
tier-1 test (reporting script, not app logic, matching the spec's own framing) —
smoke-tested against a disposable Postgres cluster instead (see SUMMARY).

## 4. Heartbeat burn-in prep

Confirmed (not assumed) by reading `heartbeat.py` and `scheduler.py` directly:
- Guardrail order in `guardrail_reason`: `not_allowlisted → quiet_hours → daily_budget
  → active_conversation → unanswered_gap` (plus an additional `in_class` hard-gate the
  spec doesn't mention — additive, not a conflict).
- Tick-history anti-repetition (`HEARTBEAT_RECENT_TICKS`, today's outbound) is wired
  into `_proactive_context` and already has a dedicated test
  (`test_proactive_context_carries_tick_history_and_outbound`). Note: the actual
  "don't repeat yourself" enforcement is **prompt-driven** (the model reads TICK
  HISTORY and is instructed to stay silent on repeats), not code-enforced — tier-1 can
  only pin that the history reaches context; whether the model actually complies is
  tier-2 territory (`test_heartbeat_does_not_repeat_a_sent_nudge`).
- Jitter + `coalesce=True` + `max_instances=1` are already configured in
  `scheduler.start_scheduler` exactly as the spec asks — no change needed, just
  confirmed by reading.
- `now`/totals/today's-events already reach `build_loop_context` (burn-in-fixes PR),
  and `heartbeat._proactive_context` calls `build_loop_context` directly — so it
  already inherits them. What was missing was a *regression test pinning this for the
  proactive path specifically* (the spec's explicit ask), since the existing burn-in
  tests only exercised the reactive loop.
- The flag in question is `HEARTBEAT_WEB_SEARCH` (the spec calls it
  `HEARTBEAT_SEARCH_ENABLED` — same knob under its real name), currently defaulting to
  `"true"` with a comment that read "ships ON during burn-in; measure speak rate,
  then decide a budget." Per the spec's standing directive, flipped the default to
  `false` with an updated comment marking it deliberate.
- `test_heartbeat_sees_a_dated_event` already exists, in
  `tests/tier2/test_schedule_events_and_verify.py` (tier-2) — confirmed it's still
  passing-shaped code post-merge (couldn't execute it live; see SUMMARY's tier-2 gap).

**Out of scope, confirmed from the spec's own text:** flipping `HEARTBEAT_ENABLED` in
prod and setting the allowlist are the founder's deploy-time actions ("Deploy notes for
the founder"), not something built here. Did not touch `HEARTBEAT_ENABLED`'s default or
widen `HEARTBEAT_ALLOWLIST`.
