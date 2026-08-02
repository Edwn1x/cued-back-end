# Phase E — Escalation Routing + Confidence-as-Communication — Summary

**Status: built, tier-1 green (219 passed, 2 pre-existing skips; 5 new red-first).
Tier-2 written — the spec's five judged cases — NOT RUN here (funded key needed).
Phase E is the behavior gate for the whole ladder; it is emphatically not "done"
until those five pass live.** Flag `MEAL_ROUTING_PROMPT_ENABLED` defaults off.

## What shipped

- **`prompts/meal_routing.md`** (new): the type-of-uncertainty ladder — printed
  numbers → history → dining → USDA → web (skeptically) → ask (last, one short
  question) — with the cross-rules: stop early on easy cases, skip unavailable
  rungs, every rung fails safe (the meal still logs), confidence is communication
  ("logged ~650, rough on portion — one cup or two?"), and never claim an unused
  source.
- **`agent_loop.py`**: when flagged, system = [voice (cached), routing (**cached** —
  stable text extends the prefix), volatile context, Phase A image block (last,
  uncached)]. Routing rides every reactive turn because meals are mostly text and
  there is deliberately no pre-classifier; the cache keeps per-turn cost at
  read rates (INVESTIGATION §5.2). Heartbeat system untouched.
- **`config.py`**: `MEAL_ROUTING_PROMPT_ENABLED` (default false).

## Judgment calls

1. **Every-turn injection over a meal-classifier** — a classifier would reintroduce
   the pre-routing the rewrite deleted; caching makes the always-on block cheap.
2. **Two cache breakpoints** ([voice], [voice+routing]) — the heartbeat's separate
   [voice]-prefix entry stays warm; flipping the flag is one deliberate cache
   re-warm, not a per-turn cost.
3. **web_search asserted only positively** — it's a server-side tool; its dispatches
   aren't client-observable, so tier-2 records client tools only (noted in-file).

## Tests

- **Tier-1** (`tests/tier1/test_meal_routing_prompt.py`, 5, red-first): block
  present on text AND image turns iff flag, positioned voice→routing→context with
  its own cache_control, Phase A block still last/uncached, string-branch ordering,
  heartbeat isolation pin.
- **Tier-2** (`tests/tier2/test_meal_routing.py`, 5, **NOT RUN here**), client tool
  dispatches recorded via a passthrough `dispatch_tool` wrapper so escalation is
  observable: clear label → rung 0, ZERO database-tool dispatches (the
  don't-force-all-tools guard) and label math in the log; generic oatmeal → USDA
  reached, dining NOT consulted; seeded campus bowl → dining match used, menu
  macros logged; irreducible shared-dish portion → ends in one portion question;
  no-label photo → uncertainty communicated / correction invited.

## Post-run tuning expectation

These five are live-model behavior anchors — per the binary-anchor rule, 4/5 is not
done; tune the routing prompt until 5/5. The likeliest first failure is
over-eagerness (calling USDA on the label case) — if seen, sharpen ladder rule 1
("a clear label needs no database") rather than adding code gates.
