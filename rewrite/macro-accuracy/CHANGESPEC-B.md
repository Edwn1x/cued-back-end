# Phase B — User-History Prior — Change Spec

The first moat source: a repeat meal has a personal ground truth (the user's own
portions and prep). Internal only — no new external dependency, no schema change, no
`voice.md` change. Flag `MEAL_HISTORY_TOOL_ENABLED`, default off.

## Change 1 — `meal_history.py` (new module): the deterministic matcher

- **What's there now:** nothing — meals are written and rendered (today's window) but
  never queried as "has this user logged something like this before".
- **What it becomes:** `match_meal_history(user_id, description, ...) -> list[dict]`:
  - Fetch the user's recent ACTIVE meals (soft-delete chokepoint `models.active()`;
    windowed ~120 days, capped rows) — cross-user isolation is structural.
  - Normalize descriptions to content-token sets (lowercase; drop numbers/quantities,
    unit words, `~` portion annotations, stopwords; light plural fold), score Jaccard
    vs the query, threshold 0.6 (INVESTIGATION §2.2 calibration: rejects the
    chicken-rice→chicken-quinoa near-miss, accepts phrasing variants and Phase A's
    portioned descriptions).
  - Group matches by normalized token set → repeat `count`, `last_eaten_at`,
    **median** macros over macro-bearing rows, most-recent `description` as the
    representative; sorted best-first, top 3.
  - Empty/no-content query or no rows → `[]` (clean fallthrough, never a guess).
- **Why:** code owns the matching (deterministic, testable); the model owns the
  judgment of whether the surfaced prior fits THIS serving.

## Change 2 — `agent_tools.py`: `match_meal_history` tool

- Read-only tool in the `get_dining_menu` pattern. Description carries the Phase-B
  routing: check history BEFORE estimating a meal that plausibly repeats; a confident
  match → lean on the user's usual and SAY so ("using your usual"); ambiguous match →
  ask or estimate fresh, never silently assume the prior. Honesty invariant: claim
  history only when the tool returned it.
- Handler output: `ok:` lines per match — repeat count, last-eaten recency, usual
  macros; or `no history match for '<query>' — estimate fresh` (a clean tool answer
  for the no-match branch too, per the #18 tool-ergonomics lesson).
- Wired into `_HANDLERS` + the loop's tool assembly behind the flag.

## Change 3 — `config.py`: `MEAL_HISTORY_TOOL_ENABLED` (default false)

## Change 4 — tests

- **Tier-1 red-first** (`tests/tier1/test_meal_history.py`): the four spec cases —
  prior "chicken and rice" surfaces its macros for a matching entry; empty history →
  clean `[]`/fallthrough message; "chicken and quinoa" does NOT match "chicken and
  rice"; user A's history never seeds user B — plus soft-deleted rows excluded,
  portion-annotated phrasing still matches, median aggregation over repeat logs, and
  loop wiring (tool present iff flag on).
- **Tier-2** (`tests/tier2/test_meal_history.py`, NOT RUN here — no funded key): a
  repeat meal's estimate reflects the user's own prior and says so; a novel meal
  claims no history (honesty invariant).
