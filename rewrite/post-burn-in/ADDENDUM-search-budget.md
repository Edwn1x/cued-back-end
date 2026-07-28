# Addendum — heartbeat web search: budgeted, ON for burn-in

**Reverses one decision from the post-burn-in spec** (item 4 set `HEARTBEAT_WEB_SEARCH`
off). Founder's call: proactive search is part of the product claim burn-in exists to
validate — a coach that can check hours/availability before texting. The managed risk
was never search-on-proactive; it was search **un-budgeted and un-instrumented**. This
addendum lands the budget + instrumentation so the flag can default on. Landed on the
same branch as the post-burn-in work, before anything merged — so the tree never ships
the off-default.

## What shipped

| Piece | Where | Result |
|---|---|---|
| Default ON + budget knob | `config.py` | `HEARTBEAT_WEB_SEARCH` → `true` (still the kill switch); new `HEARTBEAT_SEARCH_MAX_PER_DAY=3` |
| Code-enforced budget | `heartbeat.py::_search_available` | checked before the tool is offered — guardrail class, never a prompt rule; at/over budget the tick runs WITHOUT the tool (search scarcity never suppresses a message) |
| Budget counts spend | same | counts ticks where the model **invoked** search (`search_used`), not ticks where it was offered — derived from `HeartbeatTick` rows over `timefmt.local_day_bounds` (user-local day; no denormalized counter to drift) |
| Search-decision instrumentation | `models.py`, `migrate.py` | `heartbeat_ticks` gains `search_available` / `search_used` / `search_query` — the decision, not just the outcome |
| Two-track reporting | `tests/tier2/test_heartbeat_proactive.py` | cost + speak rate split searched vs unsearched, plus search share — the numbers a 50-user extrapolation needs (a blended average hides an ~5x line item) |

`decide()` now returns `(spoke, payload, search)`; every tick row records the search
decision, including guardrail-blocked ticks (recorded as not-offered).

## Tests

Tier-1 (all six from the addendum spec, red-first): under-budget offers the tool;
at-budget omits it **and the tick still speaks**; budget counts searched-not-offered;
decision fields recorded on searched, unsearched, and silent ticks; budget window is
the user-LOCAL day (an 11pm-local searched tick lands in today's UTC day — regression
against the timestamp fix); `HEARTBEAT_WEB_SEARCH=false` kill switch survives the
budget layer. Suite: **155 passed, 2 pre-existing skips.**

Tier-2 (run live 2026-07-28, funded key):
- `test_heartbeat_search_by_need_not_reflex` — the necessity claim, checked. The
  nothing-to-look-up tick did NOT search (hard assertion — the "used because present"
  leak mode). The search-worthy tick (user planning a late RSF session "if it's open")
  DID search (`query='RSF recreational sports facility hours tonight'`), got real
  hours, and made a judgment call about whether to text. Scenario users are pinned to
  an evening-local timezone — at 2am local the opening self-resolves and nothing
  searches, which is a run-time artifact, not a finding.
- `test_heartbeat_speak_rate_and_cost_summary` — two-track numbers below; the
  lookup-worthy tick searched and sent a genuinely load-bearing heads-up ("closes at
  11pm, not midnight … aim for like 9:30 instead").

## Measured (2026-07-28 run)

- Search share: **1/4** proactive ticks invoked search (one scenario deliberately
  lookup-worthy; 0 reflex searches across the no-lookup ticks in both tests).
- Searched tick: **$0.03694**, speak rate 1/1. Unsearched: **$0.00803** avg, 0/3.
  (~4.6x per-tick multiplier, before the ~$0.01/search line item — which is why the
  budget caps searched ticks and reporting stays two-track.)
- Worst-case search spend/user/day is bounded in code:
  `HEARTBEAT_SEARCH_MAX_PER_DAY (3) × (searched-tick cost + ~$0.01)` ≈ **$0.14/user/day**
  on top of the base decision loop, regardless of model behavior.
- These are smoke-level samples; the burn-in's own `heartbeat_ticks` rows
  (`search_available`/`search_used`/`search_query`) are the real dataset for the
  September 50-user extrapolation.

## Deploy notes (founder) — ORDER MATTERS

1. **This deploys BEFORE `HEARTBEAT_ENABLED` is flipped.** The first proactive tick
   must already be running budgeted — never uncapped. The migration
   (`heartbeat_ticks` search columns) applies automatically at boot (Procfile).
2. Activation checklist, updated: `HEARTBEAT_ENABLED=true`,
   `HEARTBEAT_ALLOWLIST=<your number>`, leave `HEARTBEAT_WEB_SEARCH` unset (defaults
   **on** now, budgeted) and `HEARTBEAT_SEARCH_MAX_PER_DAY` unset (defaults 3).
   Kill switch: `HEARTBEAT_WEB_SEARCH=false`.
3. During burn-in, read `search_query` on searched ticks — the "was this necessary or
   just available?" review is a one-line SQL away and is the evidence the founder's
   claim gets judged on.
