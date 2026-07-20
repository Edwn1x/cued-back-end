# Phase 3 — INVESTIGATION (tools)

**Goal:** the coach can *act*, not just talk. Six tools, one at a time, each behind
its own flag, tests red-first: remember, log_workout, manage_log, get_dining_menu,
web_search, read_image.

## Tool-execution loop (built with tool 1)

`agent_loop.run_agent_loop` became agentic: it assembles the enabled tool set (per
flag), and while `resp.stop_reason == "tool_use"` it dispatches each `tool_use` block
to `agent_tools.dispatch_tool`, appends the assistant turn (incl. thinking blocks,
unchanged) + `tool_result` blocks, and re-calls — bounded by `AGENT_LOOP_MAX_TOOL_ITERS`.
State writes stay code-mediated: the model requests, `agent_tools` validates + writes
under a row lock. Harness: `tests/_fake_anthropic.ToolUse` scripts a tool_use response;
tests route the fake by the loop's `tools` kwarg so it doesn't collide with the
webhook classifier / extraction calls (which carry no tools).

## Tool designs (wrap Phase-1 primitives where possible)

- **remember (DONE):** add/update → `memory.apply_facts`; invalidate → `memory.invalidate_entry`
  (the safety-trigger guard applies — a safety close needs a recorded trigger). Category-
  validated. Runs in PARALLEL with the legacy per-turn `extract_and_store_memory` until the
  recall eval shows parity; only then is extraction retired (do not delete it yet).
- **log_workout (next):** create a `Workout` + `split_pointer.advance_split_pointer` under the
  Phase-1 policy (named day → confirmed; else inferred). Read today's workouts first (idempotency).
- **manage_log:** list / edit / soft-delete meals, workouts, events by SHORT STABLE ID. See below.
- **get_dining_menu:** wrap `dining_scraper`; investigate call sites (currently injected into
  context — move to on-demand).
- **web_search:** Anthropic server-side tool `web_search_20260209` (verified via claude-api skill;
  dynamic filtering, Sonnet 5 supported). Constrained in the prompt. **Needs the funded key** for tier-2.
- **read_image:** vision routing — food → meal path, calendar → events+schedule, whiteboard →
  workout. Build routing + food path fully; ship a **PROVISIONAL** conservative non-food schema
  (dates/times/named commitments only), marked PROVISIONAL in code + docs, finalized against 5–10
  real screenshots. **Needs the key** (vision) + the screenshots (parked input).

## manage_log soft-delete — reader inventory (trace before adding the flag)

A `deleted` flag/timestamp on meals/workouts/events; **every** reader must filter it, or a
"deleted" row still counts. Meal readers found (grep): `admin_dashboard.py:476`, `app.py`
(totals/lists ~1710/2061), `coach.py:163` (legacy context), `scheduler.py:449`
(`check_meal_adherence`), `agents/meal_extractor.py:187`, `agents/nutrition.py:65/74/341`.
**Also the unified loop context** (`agent_loop.build_loop_context` — recent workouts; add meals
if surfaced) and **daily totals** (the correction round-trip requires a deleted meal to leave
today's cals/protein — trace `ensure_todays_totals` and wherever meals sum into `calories_today`).
Deletes are SOFT so corrections are reversible/testable. Edits/deletes target short stable IDs the
agent sees in context or via a list action — never a free-text row description.

## Meal-dup fix (read-before-write) — the verified Phase-3 change

Per [[meal-dup-root-cause]] (in memory): duplicates come from per-turn extraction with no
idempotency against today's log (verified in prod — 3 rows from one meal across text→text→photo
turns). `manage_log` soft-delete is necessary but NOT sufficient: the meal-logging path (text
extractor + photo handler) must **read today's meals before writing** and resolve a described/
photographed meal against existing same-day entries (update-or-recognize). Trace the
photo-handler + text-extraction double-write while building this.

## Honesty invariant (fully satisfiable once manage_log exists)

"the agent never claims an action it didn't take" — a state-change confirmation ("deleted it",
"logged it") may only be sent if a tool returned success in the same turn. `manage_log` makes
"delete the duplicate meal" a real action. Tier-1: state-change language co-occurs with a
successful tool_result; if the tool is unavailable/fails, the agent says it couldn't. The tool
handlers already return explicit `error:` strings on failure (never a false success).

## External facts (verify against docs, not memory)
- Anthropic tool-use request/response shape + `web_search_20260209` + vision — verified via the
  claude-api skill (current). Twilio MMS media arrival — from this repo's webhook only (Phase 0 §2
  step 1: inline base64 download); re-confirm shapes when read_image lands.
