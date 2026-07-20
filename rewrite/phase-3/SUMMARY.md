# Phase 3 — SUMMARY (tools)

**Status:** all six tools built + green; flags default off (loop + tools dormant
until enabled). The ratchet reached **zero xfails** this phase.

## Success criteria (roadmap Phase 3)
- ✅ Each tool has failing-first tests that now pass (unit + end-to-end via the loop).
- ✅ Screenshot symptom (failure 2) — routing built (model-routed vision); non-food
   schema PROVISIONAL, finalized against the founder's 5–10 real screenshots (tier-2).
- ⏳ Extraction parity before deletion: `remember` runs **in parallel** with legacy
   per-turn extraction; retire extraction only after the recall eval shows parity
   (Phase 6). Not deleted yet.

## Result
```
tests/tier1: 81 passed, 2 skipped, 0 xfailed
```
**Zero xfails** — every deterministically-encodable roadmap failure is a passing test.
The pork-chop duplicate specifically can no longer happen three ways: rare at source
(read-before-write), deletable (manage_log soft-delete), honestly narrated (honest results).

## Tools
| Tool | Flag | Notes |
|---|---|---|
| remember | `REMEMBER_TOOL_ENABLED` | wraps apply_facts / invalidate_entry; parallel to legacy extraction |
| log_workout | `LOG_WORKOUT_TOOL_ENABLED` | Workout + code-mediated pointer advance |
| manage_log | `MANAGE_LOG_TOOL_ENABLED` | list/edit/soft-delete by short id; honest results |
| log_meal | `LOG_MEAL_TOOL_ENABLED` | read-before-write; saw_similar audit; both branches proven live |
| get_dining_menu | `GET_DINING_MENU_TOOL_ENABLED` | on-demand hall menu |
| web_search | `WEB_SEARCH_TOOL_ENABLED` | server-side; output/query hygiene rules |
| read_image | `READ_IMAGE_ENABLED` | model-routed vision; provisional non-food schema |

## Design notes honored (founder directives)
- **Soft-delete chokepoint (note #1):** one `models.active` accessor, every reader
  retrofitted, leakage test — deletion is trustworthy before the tool that deletes.
- **Read-before-write (note #2):** deterministic visibility + model judgment, never a
  deterministic drop. **Validated live both branches** — duplicate re-mention not
  re-logged (1→1), genuine second serving logged (1→2) with `saw_similar`.
- **web_search hygiene:** speak findings naturally / no links; no user PII in queries.
- **read_image:** model routes in-call, no pre-classifier; "other" degrades gracefully.

## Measured cost (tier-2, live)
- Normal reply: **$0.0072** (standard) — from Phase 2.
- A reply that runs **web_search: ~$0.068 token + ~$0.01/search ≈ $0.08**. **Phase 4
  must price a searching tick at ~$0.08, not $0.007** before allowing heartbeat search.

## Gated / parked
- **read_image non-food schema** — finalize against the founder's 5–10 real screenshots
  (tier-2 vision).
- **Extraction retirement** — after `remember` recall parity (Phase 6).
- Rotate the Railway Postgres password.
