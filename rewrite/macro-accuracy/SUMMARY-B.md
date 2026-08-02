# Phase B — User-History Prior — Summary

**Status: built, tier-1 green (200 passed, 2 pre-existing skips; 10 new red-first).
Tier-2 written, NOT RUN — no funded key in this workspace; 2 live cases await the
funded run. Phase B is not "done" until they pass.** Flag `MEAL_HISTORY_TOOL_ENABLED`
defaults off. Merge held post-burn-in with the branch.

## What shipped

- **`meal_history.py`** (new): `match_meal_history(user_id, description)` —
  normalize (numbers/units/stopwords out, light plural fold) → Jaccard ≥ 0.6 →
  group by normalized token set → repeat count, last-eaten, **median** macros over
  macro-bearing rows; top 3, best-first. Reads only ACTIVE meals via the
  `models.active()` chokepoint, windowed 120d/400 rows; `[]` on no match.
- **`agent_tools.py`**: `match_meal_history` read-only tool + handler. No-match is a
  clean tool answer ("no history match … — estimate fresh") so BOTH branches have a
  tool result (the #18 ergonomics lesson). Match lines carry count, recency, usual
  macros; the description carries the honesty rule ("using your usual" only when
  true, never assume an ambiguous prior).
- **`config.py`**: `MEAL_HISTORY_TOOL_ENABLED` (default false); **`agent_loop.py`**:
  tool offered iff flag on.

## Judgment calls

1. **Python-side token matching over pg_trgm/FTS** — a user's history is tiny;
   in-process Jaccard is deterministic, dependency-free (no prod extension enable),
   and unit-testable. INVESTIGATION §2.2 has the threshold calibration: 0.6 rejects
   "chicken and quinoa" vs "chicken and rice" (1/3), accepts phrasing variants (2/3+)
   and Phase A's portion-annotated descriptions (units normalize away).
2. **Median, not mean** — one 1400-cal mislog in ten 650s must not drag the prior
   (pinned in tier-1).
3. **Tool, not context injection** — the query key (the meal description) only exists
   once the model has read the photo/text, so a pre-injected block can't target it;
   on-demand read mirrors `get_dining_menu`. When-to-prefer-which-source routing
   stays Phase E; Phase B's routing surface is the tool description.
4. **Threshold/window as module constants**, not config — tuning knobs for the
   funded-run calibration, not deploy-time flags.

## Tests

- **Tier-1** (`tests/tier1/test_meal_history.py`, 10 cases, red-first): the four
  spec cases (match surfaces prior macros; empty history → `[]`; near-but-different
  rejected; cross-user isolation) + soft-delete exclusion, portion-annotation
  robustness, median-over-repeats grouping, empty/portion-only queries, handler
  formatting incl. the no-match branch, loop wiring iff flag.
- **Tier-2** (`tests/tier2/test_meal_history.py`, 2 cases, **NOT RUN — no key
  here**): repeat meal lands within ±15% of the user's own median AND the reply owns
  the source ("your usual"); novel meal (with other-meal history present as a
  distractor) claims no history and still logs.
