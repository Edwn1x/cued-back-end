# Phase D — USDA FoodData Central — Summary

**Status: built, tier-1 green (214 passed, 2 pre-existing skips; 7 new red-first,
mocked HTTP). Tier-2 written, NOT RUN here — case 1 needs only network+USDA key
(DEMO_KEY fallback), case 2 needs the funded Anthropic key. Phase D is not "done"
until they pass.** Flag `USDA_LOOKUP_TOOL_ENABLED` defaults off; `USDA_API_KEY`
defaults empty (tool answers "not configured" — fails safe even flag-on).

## Investigation was live, not from memory (playbook §III)

Docs fetched + one real DEMO_KEY call made (INVESTIGATION §4): endpoint/params
confirmed, response shape captured, macro nutrient ids verified (1003/1004/1005 and
1008-KCAL-only — kJ impostors exist), per-100g basis confirmed, **latency measured
~1.0 s** (not estimated). Rate limits: 1,000/hr production, 429 + 1-hour block over.
Match quality probe was sane (ranked FNDDS variants) — no blocking finding.

## What shipped

- **`usda.py`**: `search_usda(query)` → top-3 `{description, data_type, per-100g
  macros}`; dataType Foundation + SR Legacy + FNDDS (**Branded excluded** — that
  long tail is the web rung); 5 s hard timeout; every failure → `UsdaUnavailable`;
  every call metered (`USDA_LOOKUP q= ms= hits=` log).
- **`agent_tools.py`**: `usda_food_lookup` read-only tool. Description scopes it to
  identifiable-but-GENERIC foods and pairs it with Phase A ("scale per-100g by your
  portion estimate"). Four degrade branches, all clean "estimate normally" answers:
  no key / no match / unavailable (timeout, 429, HTTP) / empty query error.
- **`config.py`**: `USDA_LOOKUP_TOOL_ENABLED` (false), `USDA_API_KEY` (""),
  `USDA_TIMEOUT_S = 5`; loop wiring.

## Judgment calls

1. **Per-100g passthrough, no serving math in code** — code can't see the plate;
   the model owns portion (Phase A technique), code owns faithful reference data.
2. **Trust the API's relevance score** — probe showed sane ordering; re-ranking
   locally would add opinion, not information. Top-3 candidates, model judges
   (same shape as Phase C).
3. **DEMO_KEY not defaulted in prod config** — an invisible 30/hr ceiling that
   starts failing under real use is worse than an explicit "not configured"
   fallthrough. Founder provisions the free key when flipping the flag.
4. **Latency finding surfaced, not buried:** ~1.0 s per lookup on the SMS path is
   acceptable for a rung the model climbs only when internal sources miss —
   which is exactly the Phase E routing rule.

## Tests

- **Tier-1** (`tests/tier1/test_usda_lookup.py`, 7, red-first, mocked HTTP against
  the live-verified shape): parse-by-nutrient-id (KCAL only, kJ row ignored;
  absent nutrients stay None); no-match, timeout, and 429 all degrade to clean
  answers; missing key makes **zero** HTTP calls; a timeout param is always passed;
  loop wiring iff flag.
- **Tier-2** (`tests/tier2/test_usda_lookup.py`, 2, **NOT RUN here**): live-API
  contract check (plausible entry for a staple, guards shape drift — runnable with
  just network+DEMO_KEY); full-loop weighed-food case ("200g plain grilled chicken
  breast" → 260–420 cal / 50–75 g), with the caveat printed in-test that the band
  corroborates rather than proves USDA use.
