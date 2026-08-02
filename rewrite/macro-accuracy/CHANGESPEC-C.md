# Phase C — Dining-Hall Menu Match — Change Spec

Second moat source: campus food is looked up, not estimated. Internal only; reuses
the scraped `DiningMenuItem` data and Phase B's normalizer. Flag
`DINING_MATCH_TOOL_ENABLED`, default off. `get_dining_menu` (recommendation
direction) is untouched.

## Change 1 — `meal_history.py`: normalizer goes public

`_normalize`/`_jaccard` → `normalize_tokens`/`jaccard` (same behavior; Phase B
callers updated). Why: Phase C matches menu items with the same normalization;
cross-importing another module's privates is the alternative.

## Change 2 — `dining_scraper.py`: `match_dining_items(description, hall=None, meal_period=None)`

- Queries TODAY's rows (America/LA date — same convention as `get_dining_menu`),
  optional canonicalized hall / meal-period filters.
- Score: containment |query∩item|/|query| (menu names are verbose, user phrasing is
  compressed — INVESTIGATION §3.2), floor 0.6, Jaccard tie-break; top 3 candidates
  with full macros + serving size + hall/period.
- Distinguishes its two empty cases: `(matches, had_data)` — no data scraped today
  (hall closed / scraper gap) vs data present but nothing matched.

## Change 3 — `agent_tools.py`: `match_dining_item` tool

- Read-only, `get_dining_menu` handler pattern. Description: when a meal is
  plausibly dining-hall food (user names a hall, or context implies campus), look
  the item up instead of estimating; log from the menu macros scaled by how much
  they ate; say the menu is the source only when a match was returned and used.
- Handler: `ok:` candidate lines (name, hall, period, serving, cal/protein/carbs/
  fat); `no menu data for today …` / `no menu match for '<q>' …` — both fallthrough
  branches are clean tool answers ("estimate normally").

## Change 4 — `config.py`: `DINING_MATCH_TOOL_ENABLED` (default false) + loop wiring

## Change 5 — tests

- **Tier-1 red-first** (`tests/tier1/test_dining_match.py`): the spec cases — a
  logged item matching a scraped Crossroads item uses the menu macros; a non-dining
  meal doesn't spuriously match; absent/stale data → clean fallthrough, no crash —
  plus hall/meal-period filtering, candidate ranking among distractors, stale-date
  exclusion, loop wiring iff flag, and Phase B regression (renamed normalizer).
- **Tier-2** (`tests/tier2/test_dining_match.py`, NOT RUN here): "just had the halal
  chicken bowl at crossroads" reflects menu-derived macros, not a generic estimate.
