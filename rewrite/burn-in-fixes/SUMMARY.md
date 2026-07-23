# Burn-in fixes — SUMMARY

**Status:** built + green. Three correctness issues from live burn-in (Jul 21), all in the
model-shape ↔ code-assumption family. Storage untouched (naive-UTC); a rendering boundary,
one write-path conversion, and one additive audit column.

## What shipped
| Item | Where | Result |
|---|---|---|
| 1. Local time + `now` anchor | `timefmt.py` (new), `build_loop_context`, heartbeat (inherited) | model sees a local clock + local timestamps; `CONTEXT_LOCAL_TIME_ENABLED` (default on) |
| 1b. One local-day window | `timefmt.local_day_bounds` — `todays_events` / `recompute_daily_totals` / context §7 delegate | 3 inline copies consolidated |
| 2. Code-computed totals | `## TODAY'S TOTALS` in `build_loop_context §7` + `voice.md` quote-don't-add | one authoritative row-sum reaches the prompt; the model quotes, never re-adds |
| 3. Editable events + audit | `agent_tools._EDIT_FIELDS` + edit branch; `edits` JSONB on meals/workouts/events; `voice.md` edit-vs-delete | events editable (desc/start/end, local→UTC); every edit audited; recompute-never-adjust |

## Tests
```
tier1: 140 passed, 2 skipped, 0 xfailed
```
New: `test_timefmt.py` (7), `test_totals_block.py` (4), `test_manage_log_edit.py` (5),
migration `edits`-column assertion. Tier-2 `test_burn_in_fixes.py` (3) runs on the funded
key (`pytest --run-tier2 -s`): local-day naming, digit-exact totals, edit-not-relog.

## Design notes honored
- **Storage convention** documented once in `timefmt.py`; IANA name only (crosses Nov DST),
  never a fixed offset; default tz logged when used.
- **Single source of truth** for totals: the SUM over active meals already fetched in §7
  (soft-delete filtered via `active()`), not the denormalized counters — which stay for
  legacy readers but no longer reach the prompt.
- **Recompute-never-adjust:** meal edits re-sum source rows; the audit trail (`edits`) gives
  consolidation's diff something to surface and stops an edit from silently rewriting history.
- **Honesty invariant** unchanged: `manage_log` edit returns `ok:` only on real success; the
  `voice.md` rule forbids "mentally note it" non-actions once the tool can act.

## Deploy notes (founder's number)
1. `python migrate.py` on prod (adds `edits` JSONB to meals/workouts/events) **before** redeploy.
2. Context format is live behavior; `CONTEXT_LOCAL_TIME_ENABLED=true` is the on switch /
   rollback (no new required env var — defaults on).
3. **Baked-in prod data:** events/facts written with pre-fix UTC reasoning may hold values
   that are 7h/one-day off. Consolidation merges/closes but does **not** re-derive — these
   need direct correction, not a consolidation pass. Spot-check the founder's `events` rows
   (`occurred_at` vs the intended local time) and any dated `schedule`-category facts.

## Verification run
- `.venv/bin/python -m pytest tests/tier1 -q` → 140 passed, 2 skipped, 0 xfails.
- `python -c "import timefmt, agent_loop, agent_tools, heartbeat, events, models, migrate"` → ok.
- migrate.py applied cleanly against the disposable PG18 in the migration test.
