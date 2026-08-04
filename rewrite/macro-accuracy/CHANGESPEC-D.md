# Phase D — USDA FoodData Central — Change Spec

First external rung. Facts live-verified against the current API (INVESTIGATION §4):
`GET https://api.nal.usda.gov/fdc/v1/foods/search`, `api_key` query param, per-100g
values for Foundation/SR Legacy/FNDDS, macro nutrient ids 1003/1004/1005/1008(KCAL),
429 on rate limit, **measured ~1.0 s latency**. Flag `USDA_LOOKUP_TOOL_ENABLED`,
default off; key `USDA_API_KEY`, default empty.

## Change 1 — `usda.py` (new module)

- `search_usda(query, page_size=5) -> list[dict]`: GET foods/search with
  `dataType=Foundation,SR Legacy,Survey (FNDDS)` (Branded excluded — that long tail
  is the web rung), 5 s timeout, API's own `score` ordering trusted, top 3 parsed to
  `{description, data_type, calories, protein_g, carbs_g, fat_g}` — macros extracted
  BY NUTRIENT ID (1008 must also be KCAL; kJ variants exist), values per 100 g.
- Raises `UsdaUnavailable(reason)` for timeout / 429 / HTTP error / bad payload; the
  handler turns it into a clean fallthrough. Every call metered:
  `USDA_LOOKUP q=… ms=… results=…` info log; failures log loudly with the reason.

## Change 2 — `agent_tools.py`: `usda_food_lookup` tool

- Read-only. Description: for an identifiable-but-GENERIC food (no label, no
  history, not dining-hall) — get per-100g reference macros and scale by the
  estimated portion (Phase A technique). Not for branded/restaurant items. Say USDA
  is the source only when the tool returned the entry used.
- Handler branches, all clean tool answers: no key configured → "usda lookup not
  configured — estimate normally"; no match → "no usda match…"; unavailable
  (timeout/429/HTTP) → "usda lookup unavailable…". A meal must always log — the
  lookup can only ever add information, never block.

## Change 3 — `config.py`: `USDA_LOOKUP_TOOL_ENABLED` (default false), `USDA_API_KEY`
(default ""), `USDA_TIMEOUT_S = 5` + loop wiring.

## Change 4 — tests

- **Tier-1 red-first, mocked HTTP** (`tests/tier1/test_usda_lookup.py`): parse of the
  live-verified shape (per-100g macros by id, KCAL-only energy); no-match → clean
  fallthrough; timeout and 429 → graceful degraded answers (loud, not fatal); missing
  key → no HTTP call at all; loop wiring iff flag.
- **Tier-2 live-gated** (`tests/tier2/test_usda_lookup.py`, NOT RUN here): a common
  generic food end-to-end returns a plausible matched entry and the logged meal
  reflects a scaled version of it.
