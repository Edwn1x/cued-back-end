# Post-burn-in follow-ups — SUMMARY

**Status:** built + tier-1 green + tier-2 gate green (all four gating tests run live
2026-07-28 — see "Tier-2 gate" below). Item 4 does **not** flip any prod flag;
`HEARTBEAT_ENABLED` stays default-off per the spec's own framing of that as the
founder's step.

## What shipped

| Item | Where | Result |
|---|---|---|
| 1. Migrations at boot | `migrate.py`, `Procfile` | genuine failures now raise (idempotency still swallowed); `Procfile` chains `migrate.py && app.py` so a failure blocks boot |
| 2. Event date-move edit | `agent_tools.py`, `prompts/voice.md` | `manage_log edit` gains a `date` field; moves `occurred_at`/`ends_at` to a new local day, keeps time-of-day, audited, ordered correctly with a same-call time edit |
| 3. Timestamp audit | `audit_timestamps.py` (new) | read-only report of events + dated schedule facts through the corrected `timefmt` boundary |
| 4. Heartbeat burn-in prep | `config.py`, `tests/tier1/test_heartbeat.py` | regression tests pin now/totals/events reach the proactive context. The original "search off for burn-in" default was REVERSED by the founder's addendum before merge — search is on, budgeted in code; see `ADDENDUM-search-budget.md` |

## Tests

```
tier1: 149 passed, 2 skipped (pre-existing), 0 xfailed
```
New: `test_migrations.py` (+2), `test_manage_log_edit.py` (+6), `test_heartbeat.py` (+2).
`audit_timestamps.py` has no tier-1 test (reporting script, not app logic) — smoke-tested
against a disposable Postgres cluster (see Verification run below).

### Tier-2 gate — run live 2026-07-28, 4/4 passed

Run with a funded key (`pytest --run-tier2 -s`, 17s wall):
- `test_summit_pushed_to_friday_is_an_edit_not_delete_and_relog` (new, item 2's
  tier-2 case) — model chose **edit** with `date`; row kept `deleted_at=None`, both
  `occurred_at` and `ends_at` moved to Friday with audit entries, time-of-day intact.
- `test_heartbeat_sees_a_dated_event` — summit present in proactive context.
- `test_heartbeat_does_not_repeat_a_sent_nudge` — stayed silent, reasoning cited the
  prior nudge.
- `test_heartbeat_speak_rate_and_cost_summary` — 0/3 spoke; costs below.

## Cost (item 4)

Formula from the spec: `ticks/user/day × measured decision cost × 50 users`. Measured
live 2026-07-28 by `test_heartbeat_speak_rate_and_cost_summary`: per-tick decision
costs of $0.04570 / $0.00809 / $0.00715 (avg $0.02031 over 3 ticks — the average is
skewed by the first, cold-cache tick; steady-state ≈ $0.0076/tick, matching the
Phase 2/4 base of $0.0072). Speak rate in the test scenarios: 0/3 (all correctly
silent). With `HEARTBEAT_TICK_MINUTES=45` that's ~32 ticks/user/day; at steady-state
cost, 32 × $0.0076 × 50 ≈ $12.2/day for the decision loop, or ≈ $32.5/day if every
tick paid the cold-cache rate (it won't in a long-running process). Spoken-message
cost on top is bounded by `HEARTBEAT_MAX_PER_DAY=5`. Searched ticks are a separate,
~4.6x-per-tick line item, budgeted and reported two-track — see
`ADDENDUM-search-budget.md` for those numbers. A three-tick sample is a smoke-level
measurement — treat the real burn-in's observed ticks as the number to size budgets
against before widening past the founder's allowlist.

## Deploy notes (founder)

1. **Migrations:** nothing to run manually anymore — `Procfile` now runs `python
   migrate.py` before `python app.py` on every boot. A failed migration will now show
   up as a failed deploy (exit non-zero) instead of a silently half-migrated app.
2. **Event date moves:** no flag — ships with `MANAGE_LOG_TOOL_ENABLED` (already on).
3. **Timestamp audit:** run `python audit_timestamps.py` (optionally with your phone
   or user id) against prod once, by hand, to see which pre-fix `events`/`schedule`
   facts look wrong; correct conversationally (`manage_log edit`, now including `date`)
   or by hand for schedule memory text. Read-only — I did not run this against prod
   (no prod credentials in this workspace).
4. **Heartbeat:** still your call to flip. When you do: `HEARTBEAT_ENABLED=true`,
   `HEARTBEAT_ALLOWLIST=<your number>`. Per your addendum, `HEARTBEAT_WEB_SEARCH`
   now defaults ON with a code-enforced budget (`HEARTBEAT_SEARCH_MAX_PER_DAY=3`
   searched ticks per user-local day); leave both unset, or set
   `HEARTBEAT_WEB_SEARCH=false` as the kill switch — see
   `ADDENDUM-search-budget.md`, which deploys BEFORE this flag flips. Watch
   `heartbeat_ticks` (including `search_used`/`search_query`) for the first day; the
   first real proactive message is the thing to read for voice. The tier-2 gate above
   is green (2026-07-28); still start with your own number only.

## A documentation call I made without asking

The "remove `python migrate.py` from every future checklist" instruction — I updated
`README.md`'s Deployment section (the current-facing doc) but left the manual step
inside `rewrite/phase-4/SUMMARY.md` and `rewrite/burn-in-fixes/SUMMARY.md` alone; those
are dated historical records of what was true when each PR shipped, not live checklists,
and rewriting them would misrepresent what was actually done at the time. Say the word
if you'd rather those touched too.

## Verification run

- `.venv/bin/python -m pytest tests/tier1 -q` → 149 passed, 2 skipped, 0 xfails.
- `audit_timestamps.py` smoke-tested against a disposable Postgres 18 cluster
  (`tests/_pgcluster.py`) with a seeded user/event/schedule-fact, in all three lookup
  modes (by id, by phone, no-arg/all-users) and the no-match case — output above.
- Tier-2 gate (funded key added 2026-07-28): the four gating tests → 4 passed in
  17.07s — see "Tier-2 gate" section above for per-test evidence.
