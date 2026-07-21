# Phase 6 — DELETION PLAYBOOK (the prepared commits)

The deletions are gated on live evidence, and any branch cut now would drift from `main`
before the gates green (weeks out). So the prepared form is this **exact, line-anchored
playbook** — each gate becomes a mechanical change when its clock turns green, verified
by the isolation test + the tier-1 suite. Anchors are current as of the Phase 6 build PR;
re-grep before applying (the codebase is ground truth).

**Order:** Gate A and Gate B are independent. The **Pipeline** commit requires the loop
trusted (post-burn-in) AND Gate B done (so `app.py` has no legacy callers left).

---

## Commit A — Gate A green (5–7 days heartbeat burn-in): delete templated schedulers

**Remove from `scheduler.py`:** `send_scheduled_message`, `_is_training_day`,
`_get_wake_time_for_day`, `schedule_user`, `schedule_all_users`, `check_meal_adherence`,
and `from coach import generate_scheduled_message` (line 6). In `start_scheduler`, drop
the `schedule_all_users()` call + the `check_meal_adherence` `add_job`. **Keep:** the tz
helpers (`parse_time`, `add_minutes`, `user_local_to_utc`), the `has_unanswered_outbound`
re-export, the dining scrape, and the heartbeat/consolidation/episodic wiring.

**Reconcile callers:**
- `app.py:14` — `from scheduler import start_scheduler, schedule_user` → drop `schedule_user`.
- `app.py:1388` — the signup `schedule_user(user)` call → remove (the heartbeat is the
  proactive path now; no per-user templated cron).
- `onboarding_agent.py:853,874` — `schedule_user` import + call → remove.
- `app.py` — any `check_meal_adherence` / `_is_training_day` references → remove.

**Isolation test:** `scheduler` no longer imports `coach`. (Optionally add `scheduler`
to `ROOTS` in `test_new_path_isolation.py` — it's now coach-free.)

**Verify:** `pytest tests/tier1` green; `python -c "import scheduler, app, onboarding_agent"`.

---

## Commit B — Gate B green (recall parity eval passes): delete per-turn extraction

**Precondition:** `tests/tier2/test_extraction_parity.py` has run on real parallel data
(`CUED_PARITY_DATASET`) and PASSED (remember recall ≥ extraction − ε).

**Remove:**
- `app.py` `extract_and_store_decisions` (≈63–183) + its only helper
  `_render_existing_profile_for_prompt` (≈185–201).
- `app.py` `extract_and_store_memory` (≈203–357).
- `memory.py` `extract_and_store_coaching_points_task` (≈1003+) — and `merge_coaching_points`
  if it has no other caller (grep first).
- `app.py:21` import — drop `extract_and_store_coaching_points_task`.
- The three daemon spawns in `process_buffered_message` (≈512–538): `extract_and_store_decisions`,
  `extract_and_store_memory`, `extract_and_store_coaching_points_task`.
- **Reconcile the A4 uses-bump** (≈540–558): it iterates LEGACY per-agent memory blocks
  (`"nutrition","training","readiness","coach"` via `build_memory_block_with_ids`). Under
  the single loop, `render_categories` already returns injected ids and the loop bumps
  uses in-path — so this whole block is dead. Remove it.
- Delete `tests/tier2/test_extraction_parity.py` (its verdict has been rendered; it
  imports the now-deleted `extract_and_store_memory`).

**RETAIN (not extraction):** `apply_safety_signals_task` / `detect_safety_signals` (the
deterministic safety pre-pass floor — conservative insurance), and `maybe_update_coaching_summary`
(the Phase-B watermark summarizer — distinct from per-turn extraction).

**Verify:** `pytest tests/tier1` green; grep confirms no dangling `extract_and_store_*` refs.

---

## Commit C — Pipeline removal (loop trusted + Commit B done): delete the legacy brain

**Delete files:** `orchestrator.py`, `agents/` (whole package), `coach.py`,
`skill_loader.py`, `tone_analyzer.py`.

**Reconcile `app.py`:**
- Remove the fallback branch in `process_buffered_message` (≈500–502: `from orchestrator
  import route_message` + the `if response_text is None:` legacy call).
- **Make the loop the sole responder.** The Phase-2 fallback retires here (post-burn-in):
  on `run_agent_loop` failure, send a safe minimal message ("give me a sec, try that
  again in a moment") + log at ERROR — do NOT reach for legacy (there is none). Update
  `tests/tier1/test_agent_loop_fallback.py` to assert the new behavior (safe reply + loud
  log), renaming it to the loop-is-sole-responder invariant.
- Line 13 — `from coach import get_coach_response, parse_workout_log` → remove. The
  logging-mode `parse_workout_log` call sites (≈902, 1116) retire with the pipeline;
  workout logging is the `log_workout` tool now. Excise the legacy logging-mode intercept
  or route it through the tool.
- Line 19 — `from tone_analyzer import maybe_update_style` → remove; drop the call at ≈1068.

**Isolation test (`tests/tier1/test_new_path_isolation.py`):**
- Add `app` (and `scheduler`) to `ROOTS`.
- Flip `test_doomed_set_still_present_pre_deletion` → assert the doomed modules are GONE
  (`_module_path(m)` is None for each; `import orchestrator` raises).

**Verify:** `pytest tests/tier1` green; `python -c "import app"` with no legacy modules on
disk; grep the repo for any surviving `orchestrator|coach|agents\.|skill_loader|tone_analyzer`
import.

---

## After all three
- The single agent is the whole system. `rewrite/` stays as the build record.
- Update `README.md`'s "legacy (behind flags…)" footer — the legacy is gone.
- Launch gate: beta cohort on.
