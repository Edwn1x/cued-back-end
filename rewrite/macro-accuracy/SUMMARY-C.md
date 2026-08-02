# Phase C — Dining-Hall Menu Match — Summary

**Status: built, tier-1 green (207 passed, 2 pre-existing skips; 7 new red-first).
Tier-2 written, NOT RUN — no funded key in this workspace; 1 live case awaits the
funded run. Phase C is not "done" until it passes.** Flag `DINING_MATCH_TOOL_ENABLED`
defaults off. Merge held post-burn-in with the branch.

## What shipped

- **`dining_scraper.match_dining_items(description, hall?, meal_period?)`**: today's
  (America/LA) scraped rows, optional canonicalized-hall / period filters; score =
  containment of the query's content tokens in the item's (verbose menu names vs
  compressed user phrasing — INVESTIGATION §3.2), floor 0.6, Jaccard tie-break, top
  3 candidates. Returns `(matches, had_data)` so "hall closed / not scraped" and
  "data present, no match" are distinct fallthroughs; stale `scraped_date` rows
  never serve as today's menu.
- **`agent_tools.py`**: `match_dining_item` read-only tool — candidates with full
  macros + serving size; both empty branches answer "… estimate normally" (clean
  tool answers for every branch). `get_dining_menu` (recommendation direction)
  untouched.
- **`meal_history.py`**: `_normalize`/`_jaccard` → public
  `normalize_tokens`/`jaccard` (shared normalization, no private cross-imports).
- **`config.py`** `DINING_MATCH_TOOL_ENABLED` (default false) + loop wiring.

## Judgment calls

1. **Containment, not Jaccard, for menu names** — "halal chicken bowl" vs "Roasted
   Garlic Halal Chicken Rice Bowl" is 3/3 containment but only 3/6 Jaccard; the
   asymmetry is inherent to menu verbosity. Too-loose risk is bounded: the tool
   returns named candidates the model (or user) can see, never a silently applied
   number.
- 2. **New tool over extending `get_dining_menu`** — inverted direction (description →
   item vs hall → list), different result shape (3 candidates, full macros), and the
   recommendation tool keeps its required-hall contract.
3. **No auto-detection of "is this dining food"** — hall/campus inference is model
   judgment (it has the conversation); code owns the lookup. Phase E's routing prompt
   will sharpen when to reach for it.

## Tests

- **Tier-1** (`tests/tier1/test_dining_match.py`, 7 cases, red-first): compressed
  phrasing finds the verbose item with menu macros; non-dining food doesn't
  spuriously match; absent AND stale data fall through cleanly (halls really close —
  scraper logs show 0-item days); hall/period filters + hall canonicalization;
  ranking among distractors; handler's three branches; loop wiring iff flag.
- **Tier-2** (`tests/tier2/test_dining_match.py`, 1 case, **NOT RUN — no key
  here**): "just had the halal chicken bowl at crossroads" logs within tight bands
  of the seeded menu truth (736 cal / 47g protein) — numbers a generic guess
  wouldn't land on.
