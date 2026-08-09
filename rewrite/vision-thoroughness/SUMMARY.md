# Vision Thoroughness + Background-Job Truncation — Summary

Spec: VISION_THOROUGHNESS_SPEC.md. Artifacts: INVESTIGATION.md (what the code did
before), CHANGESPEC.md (what it becomes and why). Both fixes shipped; companion
image-persistence/re-look tool NOT built (post-burn-in, per spec).

## Fix 1 — first-pass scene thoroughness (prompt-only; plumbing already existed)

- `prompts/meal_estimation.md`: new "Read the whole frame on the first pass"
  section — eaten → log_meal (portioned, as before); other visible
  food/consumables → remember(`food_on_hand`) THIS turn; brief natural
  acknowledgment; no inflation; honesty lines (never claim logged-what-you-saw,
  never claim an empty frame). Scoped to coaching-relevant consumables
  (over-capture guard). No new flag: rides MEAL_ESTIMATION_PROMPT_ENABLED
  (existing kill switch); one-file revert isolates it.
- No storage changes: food_on_hand TTL routing was built in PR #24; the gap was
  purely instruction.

## Fix 2 — background jobs: ceilings raised, stores gated on stop_reason (app.py)

| site | cap | gate |
|---|---|---|
| extract_and_store_decisions | 250 → 1000 | stop=max_tokens → `BG_JOB_TRUNCATED` warning, return before parse; next exchange re-extracts |
| extract_and_store_memory | 600 → 1500 | same |
| maybe_update_coaching_summary | 600 → 1500 (old cap sat INSIDE its own ≤400-word ask) | same — prior summary AND watermark kept, next cycle refolds the same cohort (self-healing) |

The gate runs BEFORE parsing: the danger case is truncated output that still
parses; parse-failure was the only (accidental) guard before. Judgment call: also
gated `extract_and_store_memory` (not named in the spec's evidence) — same file,
same pattern, writes durable memory; the audit's finding 4 named the whole class.

`MEMORY_EXTRACT invalid=1`: investigated, structurally NOT truncation —
`stats["invalid"]` increments only after a successful json.loads; a truncated
blob dies at the parse. It means a parsed fact with empty text or out-of-list
category, already logged by name (`MEMORY_INVALID_CATEGORY` / no-text). No change.

Not raised (surfaced, not built): episodic.digest (120) and
extract_coaching_points (300) — same class, no live stop= hits, inherit this
pattern if their lines ever fire; legacy-router extractors (dormant under
SINGLE_AGENT_LOOP_ENABLED).

## Test record

- **Tier-1**: 267 passed, 2 pre-existing skips, no new xfails (full suite,
  2026-08-08, this workspace). New: `test_bg_job_truncation.py` (9 — ceilings,
  parseable-truncation discard per site incl. summary watermark pin, clean-path
  regression), `test_vision_scene_routing.py` (loop-level: seen-not-eaten →
  food_on_hand, never constraints/meals/totals; TTL-mortal),
  `test_meal_estimation_prompt.py::test_estimation_block_includes_whole_frame_sweep`
  (red-first marker "whole frame"). All red-first except the routing guard
  (green regression guard on PR #24 plumbing, per playbook §II).
- **Tier-2** (funded key via sister-workspace .env, 2026-08-08): NEW
  `test_vision_thoroughness.py` **3/3 first run** — scene photo logged
  "5 fried eggs" 350cal/30g ONLY, food_on_hand captured all three labeled items
  (jam, egg whites, bread) in one dated entry, reply acknowledged the scene
  ("also see you've got jam, liquid egg whites, and a loaf of bread sitting
  there"); plain-plate control logged cleanly with ZERO spurious on-hand entries.
  Regression neighborhood `test_meal_estimation.py` 3/3 (portioned plate,
  label-math 480/12, flag-off path).
- New fixture: `breakfast_scene.png` (synthetic PIL, labeled packaging, mirrors
  the Aug 8 incident; realism caveat unchanged from the macro fixtures).

## Deploy checks (from the spec's definition of done)

1. Send a real multi-item food photo: expect eaten-only meal log + on-hand
   capture + scene acknowledgment.
2. `grep 'TOKENS .* stop=max_tokens'` — extract_and_store_decisions /
   maybe_update_coaching_summary should stop appearing; any `BG_JOB_TRUNCATED`
   line now names a genuinely oversized output.
3. Measure meal-turn latency before/after — some perceived slowness may ease.
4. If a `MEMORY_INVALID_CATEGORY` warning accompanies the next `invalid=1`,
   that's the (separate, already-logged) fact-quality event, not truncation.
