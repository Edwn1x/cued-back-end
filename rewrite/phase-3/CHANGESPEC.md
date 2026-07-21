# Phase 3 — CHANGE SPEC (tools)

Numbered *what's-there-now / what-it-becomes / why / where*. Each tool behind its
own flag (default off); the loop assembles the enabled set. Commits:
`3b3f790` remember · `cad27cd` log_workout · `0bbd25b` soft-delete chokepoint ·
`b7f4a1d` manage_log · `69cd59a` log_meal · `356159a` F5a flip · `5794ea8`
get_dining_menu · `329eea5` web_search · `ed76d69` read_image.

### 1. Agentic loop
- **Now:** one model call, no tools.
- **Becomes:** `run_agent_loop` assembles flag-enabled tools and, while
  `stop_reason == tool_use`, dispatches each block to `agent_tools.dispatch_tool`
  (code-mediated, row-locked), feeds `tool_result` back, re-calls (bounded).
  Handles `pause_turn` for the server-side search. Harness gains `ToolUse` scripting.
- **Where:** `agent_loop.py`, `agent_tools.py`, `tests/_fake_anthropic.py`.

### 2. remember / log_workout / manage_log / log_meal / get_dining_menu (client tools)
- **remember:** add/update → `apply_facts`, invalidate → `invalidate_entry` (safety
  guard). Parallel to legacy extraction until recall parity.
- **log_workout:** create `Workout` + `advance_split_pointer` (named=confirmed, else inferred).
- **manage_log:** list/edit/soft-delete by short id; honest `ok`/`error` results.
- **log_meal:** read-before-write — today's meals are injected into context (the read),
  the model decides log/skip/second-serving, records `saw_similar` for audit.
- **get_dining_menu:** on-demand read of today's `DiningMenuItem` (was context injection).
- **Where:** `agent_tools.py`, `config.py`, `agent_loop.py`.

### 3. Soft delete (note #1)
- **Now:** hard deletes / no delete; denormalized daily counters.
- **Becomes:** `deleted_at` on meals/workouts/events; **`models.active` chokepoint**
  every reader goes through; `recompute_daily_totals` from active meals (correction
  round-trip). Leakage test asserts zero leakage via the accessor, totals, and context.
- **Where:** `models.py`, `migrate.py`, `coach.py`, `nutrition.py`, `meal_extractor.py`,
  `scheduler.py`, `agent_loop.py`.

### 4. web_search (server-side) + read_image (vision)
- **web_search:** `web_search_20260209` server tool; voice.md output/query hygiene rules.
- **read_image:** MMS → model vision; the MODEL routes food/calendar/whiteboard/other
  in-call (no pre-classifier); non-food schema PROVISIONAL; flag-off notes the image
  without inventing.
- **Where:** `agent_loop.py`, `prompts/voice.md`, `config.py`.
