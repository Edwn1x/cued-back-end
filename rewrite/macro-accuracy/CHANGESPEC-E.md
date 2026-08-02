# Phase E — Escalation Routing + Confidence-as-Communication — Change Spec

Ties the rungs together, now that they exist (A: label/portion technique, B:
history, C: dining, D: USDA, plus the pre-existing web_search rung). Prompt-only;
no new tools, no state. Flag `MEAL_ROUTING_PROMPT_ENABLED`, default off. No
`voice.md`/heartbeat change (INVESTIGATION §5.1).

## Change 1 — `prompts/meal_routing.md` (new file)

The type-of-uncertainty ladder, stated for the model:
- Escalate by what KIND of information is missing — never by a self-scored
  confidence number driving behavior. Stop climbing the moment the remaining
  uncertainty is something no data source can resolve.
- The rungs: printed numbers in view (label/menu/package — ground truth, rung 0) →
  their own history (`match_meal_history`) → dining hall (`match_dining_item`) →
  generic reference (`usda_food_lookup`, per-100g × portion) → web search (branded/
  restaurant long tail, skeptically) → ask the user (LAST — only for what only they
  know: amount actually eaten, hidden ingredients; one short question).
- Easy cases stop early (a clear label needs no database — don't run every source;
  latency and noise both cost). Hard cases may legitimately climb several rungs
  before asking. A rung whose tool isn't available this turn is skipped.
- Confidence is for COMMUNICATION: when the estimate is rough, say so and make the
  correction cheap ("logged ~650, rough on the portion — one cup or two?"). Never
  invent precision; never claim a source that wasn't used this turn.
- Every rung fails safe: tool errors/no-matches fall through; the meal still logs
  (unless asking-first is the honest move).

## Change 2 — `agent_loop.py`: inject as a CACHED stable block

When `MEAL_ROUTING_PROMPT_ENABLED`: system becomes [voice (cached), routing
(cached), volatile context, (Phase A image block when applicable)] — the routing
block is stable text and extends the cached prefix (INVESTIGATION §5.2), so every
reactive turn carries it at cache-read cost. String branch concatenates in the
same order. Heartbeat untouched.

## Change 3 — `config.py`: `MEAL_ROUTING_PROMPT_ENABLED` (default false)

## Change 4 — tests

- **Tier-1 red-first** (`tests/tier1/test_meal_routing_prompt.py`): block present on
  text AND image turns iff flag on, positioned between voice and context, with its
  own cache_control; voice block byte-identical; Phase A block still last on image
  turns; absent flag-off; string branch ordering; heartbeat isolation pin.
- **Tier-2 judged** (`tests/tier2/test_meal_routing.py`, NOT RUN here) — the spec's
  five cases, with client-tool dispatches RECORDED via a passthrough wrapper around
  `dispatch_tool` (live model, observable escalation): clear label resolves at rung
  0 with zero database-tool dispatches; generic food reaches USDA but not dining;
  campus item uses the dining match; irreducible portion ends in one short question;
  a rough estimate's final message communicates roughness/invites correction.
