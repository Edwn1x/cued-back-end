# Phase 6 — SUMMARY (deletion + launch)

**Status:** the evidence-independent half is built + green. Phase 6 is the first phase
that **cannot complete on build alone** — its two deletions are gated on wall-clock
evidence from the live system, and neither clock has started (no flag flipped). So this
PR ships everything that doesn't need evidence; the deletions are prepared commits that
land when their gates go green.

## The split
- **Built now (this PR):** deletion inventory, the new-path isolation test (the safety
  proof), the extraction→remember parity eval (Gate B's automated verdict), the README
  rewrite — plus one refactor the isolation test forced.
- **Staged (land when green):** the deletions themselves, in order — Gate A (templated
  schedulers), Gate B (per-turn extraction), pipeline removal.

## The two gates (fed by time on the live system)
- **Gate A — templated schedulers die after 5–7 days of heartbeat burn-in.** Clock
  starts at `HEARTBEAT_ENABLED`.
- **Gate B — extraction dies after recall parity.** `remember` writes in parallel with
  extraction; the parity eval decides. Clock starts at `SINGLE_AGENT_LOOP_ENABLED` +
  `REMEMBER_TOOL_ENABLED` on a live user.

## Result
```
tests/tier1: 106 passed, 2 skipped, 0 xfails
```
+3 isolation tests; the parity eval skips cleanly until its dataset exists.

## What shipped
| Piece | Where | Notes |
|---|---|---|
| deletion inventory | `rewrite/phase-6/INVESTIGATION.md` | doomed vs retained, verified by import graph; gate order |
| isolation proof | `tests/tier1/test_new_path_isolation.py` | AST closure: brain ∩ doomed = ∅ (catches lazy imports) |
| isolation refactor | `engagement_tracker.py` + `scheduler.py` + `heartbeat.py` | `has_unanswered_outbound` moved to a coach-free home |
| parity eval | `tests/tier2/test_extraction_parity.py` | Gate B verdict; skips until `CUED_PARITY_DATASET` |
| README | `README.md` | single-agent architecture + flag order + tiers |

## What the isolation test caught
Framing the "the brain doesn't need the legacy pipeline" claim as an executable test
immediately falsified it: `heartbeat → scheduler → coach` (scheduler top-level-imports
`coach.generate_scheduled_message`, the heartbeat pulled `has_unanswered_outbound` from
scheduler). Fixed by moving that one coach-free gate to `engagement_tracker.py`. The
closure is now genuinely disjoint — the deletion isn't blocked on the Gate A commit.

## Prepared deletion commits (apply when a gate goes green)
1. **Gate A** — remove `scheduler` templated touchpoints (`send_scheduled_message`,
   `schedule_user`, `schedule_all_users`, `check_meal_adherence`, `_is_training_day`,
   `_get_wake_time_for_day`) + `from coach import generate_scheduled_message`; trim
   `start_scheduler`. Extend the isolation test: scheduler no longer imports coach.
2. **Gate B** — remove `app.extract_and_store_memory` / `extract_and_store_decisions` /
   `extract_and_store_coaching_points_task` + their daemon spawns. **Retain the
   deterministic safety pre-pass floor.** Extend the isolation roots to include `app`.
3. **Pipeline** — delete `orchestrator.py`, `agents/`, `coach.py`, `skill_loader.py`,
   `tone_analyzer.py`; remove the app fallback branch + legacy top-level imports. The
   loop becomes the sole responder (the Phase-2 fallback retires here, post-burn-in).
   Flip the isolation test to assert the modules are GONE.

## Launch path (founder)
1. Merge the stack #4 → #7 → this; retarget bases to `main` as each lands.
2. `python migrate.py` on prod.
3. Flip `SINGLE_AGENT_LOOP_ENABLED` + the tool flags → `HEARTBEAT_ENABLED` +
   `HEARTBEAT_ALLOWLIST` = your number → `CONSOLIDATION_ENABLED` + `EPISODIC_ENABLED`.
4. Live for a week; read the morning consolidation audit lines (the drift check).
5. As each gate goes green — heartbeat burn-in (A), parity dataset + passing eval (B) —
   apply that prepared deletion commit.
6. Still parked: **rotate the Railway Postgres password**; 5–10 real screenshots for the
   read_image non-food schema.

The rewrite went from "look at my memory architecture" to a fully-tested single-agent
system in seven PRs. What's left between it and the beta cohort is flag flips and days
elapsing — not code.
