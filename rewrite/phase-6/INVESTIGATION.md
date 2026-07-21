# Phase 6 — INVESTIGATION (deletion + launch)

**Goal:** delete the legacy multi-agent pipeline + templated schedulers, flip the final
flags, launch. This is the **first phase that cannot complete on build alone** — its two
deletions are gated on wall-clock evidence from the live system, not on code being ready.

So Phase 6 **splits**:
- **Evidence-independent (build now, this PR):** the deletion inventory, the parity-eval
  scaffolding (runs automatically once data exists), the new-path isolation test (proves
  deletion is safe), the README rewrite.
- **Evidence-gated (prepared commits, land when green):** the deletions themselves,
  staged and tested, applied when their criteria go green — not when the code is ready.

## The two deletion gates (fed by time on the live system)

### Gate A — templated schedulers die after 5–7 days of heartbeat burn-in
The new proactive path (Phase 4) must prove itself on the founder's number before the
old cron touchpoints are removed. Clock starts when `HEARTBEAT_ENABLED` is flipped.

### Gate B — per-turn extraction dies after recall parity
`remember` (Phase 3) writes IN PARALLEL with legacy extraction. Extraction retires only
once the recall eval shows the loop (remember-populated memory) recalls stated facts at
least as well as extraction did. Clock starts when `SINGLE_AGENT_LOOP_ENABLED` +
`REMEMBER_TOOL_ENABLED` are flipped and a live user accumulates writes.

**Neither clock has started** — no flag has been flipped (as of this phase).

## Deletion inventory (verified by import graph, not guesswork)

### Doomed set — the legacy inbound pipeline
| Module | Imported by (non-test) | Notes |
|---|---|---|
| `orchestrator.py` | `app.py:501` (fallback only) | classifier → specialists router |
| `coach.py` | `app.py` (get_coach_response, parse_workout_log), `orchestrator.py`, `scheduler.py` (generate_scheduled_message) | the monolith |
| `agents/` (nutrition, training, readiness, personality, meal_extractor, weight_extractor) | **only** `orchestrator.py` | the specialists — orphaned the moment orchestrator dies |
| `skill_loader.py` | `coach.py`, `agents/*` | all legacy callers |
| `tone_analyzer.py` | `coach.py`, `app.py` (maybe_update_style), `agents/personality.py` | all legacy callers |

### Retained — shared or non-legacy (must NOT be deleted)
- **`macro_calculator.py`** — used by `onboarding_agent.py`. Onboarding stays; retain.
- **`onboarding_agent.py`** — the onboarding flow is untouched by this rewrite.
- **`scheduler.py`** — the module SURVIVES. The heartbeat/consolidation/episodic use
  `has_unanswered_outbound`, `user_local_to_utc`, `parse_time`, `start_scheduler`,
  `stop_scheduler`. Only the **templated touchpoint functions** die (Gate A):
  `send_scheduled_message`, `schedule_user`, `schedule_all_users`, `check_meal_adherence`,
  `_is_training_day`, `_get_wake_time_for_day`, and their `add_job` wiring +
  `from coach import generate_scheduled_message`.
- All new-path + primitive modules: `agent_loop`, `agent_tools`, `heartbeat`,
  `consolidation`, `episodic`, `memory`, `models`, `events`, `split_pointer`, `sms`,
  `dining_scraper`, `engagement_tracker`, `cost_tracking`, `message_buffer`.

### The isolation fact that makes deletion safe (provable NOW)
The single-agent brain (`agent_loop` + `agent_tools` + `heartbeat` + `consolidation` +
`episodic`) has a transitive import closure that is **disjoint from the doomed set** —
it never reaches orchestrator/coach/agents/skill_loader/tone_analyzer. Encoded as a
tier-1 test (`test_new_path_isolation.py`) that passes today and stays true after
deletion. Deletion is removing dead weight the brain already doesn't touch, not surgery.

## Prepared deletion commits (staged; land when a gate goes green)

1. **Gate A commit** — remove templated scheduler touchpoints + `coach.generate_scheduled_message`;
   trim `scheduler.start_scheduler`. Isolation test adds: scheduler no longer imports coach.
2. **Gate B commit** — remove `app.extract_and_store_memory` / `extract_and_store_decisions`
   / `extract_and_store_coaching_points_task` + their daemon spawns. **Retain the
   deterministic safety pre-pass floor** (`detect_safety_signals` / `apply_safety_signals_task`)
   — conservative insurance, not LLM extraction. Isolation test extends to `app`.
3. **Pipeline commit** — delete `orchestrator.py`, `agents/`, `coach.py`, `skill_loader.py`,
   `tone_analyzer.py`; remove the app.py fallback branch + legacy top-level imports. The
   loop becomes the sole responder (the Phase-2 fallback is retired only here, post-burn-in).
   Isolation test flips to also assert the modules are GONE (`import orchestrator` raises).

Order: Gate A and Gate B are independent; the pipeline commit requires the loop trusted
(post-burn-in) and Gate B done (extraction gone) so app.py has no legacy callers left.

## Parity-eval scaffolding (evidence-independent; runs when data exists)
`tests/tier2/test_extraction_parity.py` — the recall comparison, built now so it runs
automatically once a live user has accumulated parallel writes. It reads a parity dataset
(scrubbed live export or a live DB URL via env); **skips with a clear reason when absent**
so CI stays green and the eval fires the moment data lands. Gate B's green light.

## Launch path (founder, after this PR)
Merge the stack (#4→#7 + this) → `python migrate.py` on prod → flip loop + tools →
allowlist founder number → heartbeat on → consolidation/episodic on → live a week,
read the morning consolidation lines → as each gate goes green, apply its prepared commit.
