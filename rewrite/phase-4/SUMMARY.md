# Phase 4 — SUMMARY (heartbeat)

**Status:** built + green; flags default off. The coach can now reach out on its own —
a dumb clock, a smart decision, **default silent**. Guardrails run in code (a violating
tick never reaches the model); every tick is logged, and the log feeds the next tick so
the coach can't re-send the same nudge. Burn-in runs on the founder's number only
(allowlist), on top of the live Phase 2/3 loop.

## Success criteria (roadmap Phase 4)
- ✅ A self-directed "should I say anything?" decision, not a template — one voice,
  reusing `build_loop_context` (the same world the reactive loop sees).
- ✅ Default silent; speak only on a real signal (accountability / open thread / timely
  check-in). Enforced by prompt + the `stay_silent` tool + measured live (tier-2).
- ✅ Guardrails in code before any model call — allowlist, quiet hours, daily cap,
  active-conversation, unanswered-outbound. Blocked ticks cost $0.
- ✅ The tick sees its own history (flag #1), the clock is jittered (flag #2), web_search
  is on the proactive path for burn-in (flag #3).
- ⏳ Speak-rate / per-day search budget — **measured in burn-in**, then set
  `HEARTBEAT_MAX_PER_DAY` from the real rate (tier-2 harness prints it).

## Result
```
tests/tier1: 91 passed, 2 skipped, 0 xfailed
```
Ten new heartbeat tests + the migration table assertion. **Zero xfails** held.

## What shipped
| Piece | Flag / where | Notes |
|---|---|---|
| tick engine | `heartbeat.py` | guardrails → `decide` → log (+ send) |
| guardrails | `heartbeat.guardrail_reason` | allowlist / quiet / daily-cap / active-convo / unanswered; in code |
| decision | `heartbeat.decide` | `build_loop_context` + proactive header; `stay_silent` tool; text = the SMS |
| tick log | `models.HeartbeatTick`, `migrate.py` | one row/tick; feeds next tick (anti-repetition) |
| scheduler | `scheduler.start_scheduler` | `IntervalTrigger` + jitter, coalesce, max_instances=1 |
| config | `config.py` | `HEARTBEAT_*` — all default off/safe |

## Design notes honored (founder directives)
- **Flag #1 — the tick sees its own history or it nags.** Every tick → `HeartbeatTick`;
  `_proactive_context` feeds the last 8 decisions **and** today's outbound back, with a
  prompt rule: silence is presumed if the thought was already sent or recently declined.
  Repetition control, **separate from** the daily cap (volume control).
- **Flag #2 — jitter the clock.** `IntervalTrigger(jitter=600s)` + message-shape variety
  in the prompt; `coalesce`/`max_instances=1` so a slow tick can't stack.
- **Flag #3 — web_search on the proactive path (burn-in).** Reactive search unchanged;
  measure speak rate first, then set a budget.
- **Guardrails are limits, not behavior.** The model composes freely when it speaks but
  can never talk past a guardrail (they run first, in code).

## Timestamp convention (verified)
`Message.created_at`'s aware-UTC default round-trips as naive **UTC** on prod's UTC
server (what all readers assume) but as naive **local** on the local disposable PG — a
test-only artifact. Heartbeat follows the prod/UTC convention; `HeartbeatTick.decided_at`
is naive UTC by construction; tier-1 seeds explicit naive-UTC timestamps. See INVESTIGATION.

## Cost
- Guardrail-blocked tick: **$0** (no model call).
- A silent tick that reaches the model ≈ one standard input pass (voice cached).
- A speaking/searching tick prices ~**$0.08** (Phase 3 search measurement), not $0.007.
- The daily cap + guardrails bound spend; **burn-in measures the real speak rate** —
  `tests/tier2/test_heartbeat_proactive.py` prints speak rate + avg cost/tick.

## Handover checklist (founder actions — build is done, burn-in needs these)
1. Merge PRs #4 / #5 (Phase 3 tools) and this Phase 4 PR.
2. `python migrate.py` on prod (creates `heartbeat_ticks`).
3. Flip flags: `SINGLE_AGENT_LOOP_ENABLED` + tool flags, then `HEARTBEAT_ENABLED=true`.
4. Set `HEARTBEAT_ALLOWLIST` to the **founder's phone number** (burn-in on one number).
5. Provide the heartbeat sending number in env if distinct from the main Twilio line.
6. After a few days: read the tick transcript, quote the speak rate, set
   `HEARTBEAT_MAX_PER_DAY` + a per-day search budget from it.
7. Still parked: rotate the Railway Postgres password; 5–10 real screenshots for the
   read_image non-food schema.
