# Macro Estimation Accuracy — Investigation

Per-phase investigation notes for the phased spec (`Macro Estimation Accuracy — Phased
Spec`, handed with `ENGINEERING_PLAYBOOK.md`). Post-burn-in feature: built on this
branch, **merge held** until the heartbeat burn-in closes. Sections are appended as
each phase begins; §1 covers Phase A.

---

## §1 Phase A — the current meal-photo estimation path

### 1.1 The path, end to end (reactive, single agent loop)

1. **Webhook → loop.** Inbound MMS reaches `app.py`, which calls
   `agent_loop.run_agent_loop(user, body, type, image_data=...)`. With
   `READ_IMAGE_ENABLED`, the image goes straight into the model's vision as a content
   block ahead of the caption text (`agent_loop.py:253-257`). No pre-classifier — the
   model routes food/calendar/whiteboard/other in-call, per `voice.md` §Images (MMS).
2. **System prompt.** `system = [voice.md (cache_control: ephemeral), volatile context]`
   (`agent_loop.py:244-251`). The volatile context includes today's logged meals with
   ids + code-computed totals (read-before-write surface).
3. **Estimation guidance today.** The *entirety* of the photo-estimation instruction is
   one bullet in `voice.md` §Images: "**Food they're eating now** → estimate the meal +
   macros and log it with log_meal (read-before-write applies)." Plus the domain line
   in §Nutrition: "Give approximate cals + protein per meal." **There is no portion
   guidance at all** — nothing about reference objects, nothing naming portion/weight
   as the dominant uncertainty, and no instruction to read a *visible label* on food
   being eaten now (label-reading appears only in the *not-yet-eaten* bullet, routed to
   `remember`).
4. **Write.** The model calls `log_meal` (`agent_tools.py:207-288`): description +
   optional cal/protein/carbs/fat, multi-item form, `saw_similar` audit,
   `recompute_daily_totals` once after insert. Nothing portion-shaped is captured
   unless the model happens to put it in `description`.

### 1.2 What happens with portion today

Nothing structured. The model eyeballs a plate with zero technique guidance; the
estimate's dominant error term (portion/weight) is unaddressed. The tenders burn-in
case showed label text IS reliably readable when the prompt directs attention to it
(image-persistence tier-2: "NET WT 1.5 LB (680 g)" read and persisted) — but for
eaten-now food no rule directs the model to prefer printed numbers over eyeballing.

### 1.3 Spec-vs-code discrepancy (surfaced, per playbook §III)

The spec's cross-cutting guardrail says **"No `voice.md`/heartbeat change"**, but the
meal-estimation prompt Phase A targets *is* `voice.md` — there is no separate
estimation prompt file. And `voice.md` is genuinely shared: `heartbeat.py:260-262`
sends the same `_voice_prompt()` as its cached system prefix, so any `voice.md` edit
reaches the proactive surface mid-burn-in (exactly what the spec forbids), and would
also invalidate the heartbeat's prompt cache.

**Resolution (not silent reconciliation):** Phase A ships as a **separate prompt file**
(`prompts/meal_estimation.md`) injected by `run_agent_loop` as an additional system
block only when the turn actually carries an image (`image_data` present,
`READ_IMAGE_ENABLED`, and the new phase flag). This satisfies every constraint the
plain edit could not:

- `voice.md` byte-untouched → heartbeat surface and its prompt cache undisturbed.
- The phase gets a real flag (`MEAL_ESTIMATION_PROMPT_ENABLED`, default off) — a pure
  `voice.md` edit is not flaggable.
- Non-image turns pay zero tokens for it; the cached voice prefix is unaffected
  (the block is appended after the cache breakpoint).

### 1.4 Legacy path (out of scope, confirmed)

The pre-rewrite photo flow (`orchestrator.py:164` → `agents/nutrition.py` →
`user.pending_photo_meal` JSON hand-off) still exists behind
`SINGLE_AGENT_LOOP_ENABLED=false` fallback. It is not the live path and the spec
scopes this work to the meal-estimation path of the loop; legacy stays untouched.

### 1.5 Test harness facts (for the phase's tests)

- Tier-1: `anthropic_stub` (conftest) intercepts `Messages.create`; a handler receives
  the raw kwargs, so system-block composition is directly assertable
  (`tests/tier1/test_read_image.py` is the pattern). Heartbeat ticks are invocable
  as `heartbeat.heartbeat_tick(user_id)` under the same stub → the isolation pin
  (guidance never in the heartbeat system) is cheap.
- Tier-2: live-model image cases exist (`tests/tier2/test_image_fact_persistence.py`)
  with a committed synthetic label fixture (`tests/fixtures/tenders_label.png`) that
  the model read correctly on the funded run. Phase A adds fixtures in the same style.
  **Tier-2 cannot run in this workspace (no funded key — known constraint); cases are
  written and recorded NOT RUN in the summary.**

---

## §2 Phase B — the meal table's queryability for history matching

### 2.1 What a user's history looks like

`Meal` rows (`models.py:206`): free-text `description` ("chicken burrito bowl from
chipotle"; post-Phase-A photos add portions — "chicken breast ~6oz, rice ~2 cups"),
nullable int macros, `eaten_at` naive-UTC, soft-delete via `deleted_at` (every reader
MUST go through `models.active()` — the chokepoint), `log_type`, `edits` audit. There
is **no normalized food-name column and no exact-string repeatability** — the same
meal arrives phrased differently turn to turn ("chicken and rice", "chicken + rice
bowl", "chicken breast ~6oz, white rice ~2 cups"). Exact-string matching would miss
nearly all repeats → fuzzy matching is required, and the too-loose direction is the
dangerous one (spec: wrong prior silently applied).

### 2.2 Match mechanism decision

Candidates: Postgres `pg_trgm` similarity (needs a prod extension enable — a
migration risk for marginal gain), Postgres FTS (stemming tuned for prose, opaque
thresholds), or **Python-side token matching over the user's own rows**. A single
user's history is tiny (hundreds of rows at most), so fetching their recent active
meals and scoring in Python is O(small), deterministic, dependency-free, and
unit-testable — that's the choice.

Mechanism: normalize each description to a content-token set (lowercase; drop
quantities/numbers, unit words (oz/cup/g/tbsp/serving/…), portion tildes, stopwords
(and/with/of/…); light plural fold) and score **Jaccard overlap**, threshold 0.6.
Calibration against the spec's guard cases:
- "chicken and rice" vs "chicken and **quinoa**" → {chicken,rice} ∩ {chicken,quinoa}
  = 1/3 → **rejected** (the too-loose failure the spec names).
- "chicken and rice bowl" vs "chicken and rice" → 2/3 → matched.
- "chicken breast ~6oz, white rice ~2 cups" vs "chicken breast with rice" → 3/4 →
  matched (portion annotations normalize away; Phase A's portioned descriptions
  don't defeat matching).

Aggregation: matched rows group by normalized token set; each group reports repeat
count, last-eaten time, and **median** macros over macro-bearing rows (robust to a
one-off mislog). Cross-user isolation is structural (query filters `user_id`).

### 2.3 How the estimation path consults it

A read-only tool (`match_meal_history`, pattern of `get_dining_menu`), flag-gated,
wired into the loop's tool set. The model calls it when a meal plausibly repeats;
the no-match branch returns a clean "no history match — estimate fresh" (a tool
answer for BOTH branches — the #18 ergonomics lesson). Full escalation routing
(when to prefer history over dining/USDA/web) is Phase E; Phase B's routing surface
is the tool description only. The tool result carries data + the honesty hook
("say 'using your usual' only when you actually use it").

---

## §3 Phase C — dining data storage, freshness, and the matching gap

### 3.1 What's stored and how it's queried today

`DiningMenuItem` (`models.py:257`): per `scraped_date` (YYYY-MM-DD, America/LA) ×
hall × meal_period rows with `item_name`, full macros (cal/protein/carbs/fat/fiber),
`serving_size`, allergens, dietary tags. The scraper (`dining_scraper.scrape_all_halls`)
refreshes daily; halls genuinely return 0 items when closed (summer/holidays) — the
spec's stale/absent case is real and the existing `get_dining_menu` handler already
answers it with an error string.

The only query surface is `get_dining_menu` (hall required → up to 80 items, cal +
protein only): shaped for "what should I eat at crossroads", i.e. recommendation.
For estimation the direction is inverted — the model has a *description* ("halal
chicken bowl at crossroads") and needs the matching item's macros. Reading an
80-item dump to find one item is token-heavy, drops carbs/fat/serving size, and
requires guessing the meal period.

### 3.2 Matching mechanism

Menu item names are verbose ("Roasted Garlic Halal Chicken Rice Bowl") while users
compress ("halal chicken bowl") — the containment direction is asymmetric, so plain
Jaccard under-scores true matches. Score = **containment of the query's content
tokens in the item's** (|q∩i|/|q|, same normalization as Phase B), floor 0.6,
Jaccard as tie-break; return top 3 as *candidates* — the model (or the user) picks,
so recall matters more than a single hard verdict, and the too-loose risk is
bounded by the tool returning names alongside macros (a wrong candidate is visible,
not silently applied). Hall and meal-period are optional filters (hall names
canonicalize via the scraper's `_canonical_hall`).

### 3.3 Placement

Matcher `match_dining_items(...)` lives in `dining_scraper.py` (the menu-data
domain), reusing Phase B's normalization — `meal_history._normalize/_jaccard` become
public (`normalize_tokens`/`jaccard`) rather than cross-importing privates. New
read-only tool `match_dining_item` behind `DINING_MATCH_TOOL_ENABLED`; the existing
`get_dining_menu` recommendation tool is untouched. Both fallthrough branches (no
data today / no match) return clean tool answers.

---

## §4 Phase D — USDA FoodData Central, verified against the live API (2026-08-01)

Per playbook §III this was checked against the CURRENT docs (fdc.nal.usda.gov/api-guide)
**and a live call**, not training data.

### 4.1 Verified facts

- **Auth:** data.gov API key as `api_key` query param. `DEMO_KEY` exists (30 req/IP/hr,
  50/day); a free production key defaults to **1,000 req/hr per IP**. Over-limit → HTTP
  429 + 1-hour block; `X-RateLimit-*` headers advertised (absent on the DEMO_KEY
  response we made — don't depend on them).
- **Endpoint:** `GET https://api.nal.usda.gov/fdc/v1/foods/search` with `query`,
  `dataType` (comma list works on GET), `pageSize`.
- **Response shape (live-verified):** `foods[]` each with `description`, `dataType`,
  relevance `score`, and `foodNutrients[]` of `{nutrientId, nutrientName, value,
  unitName}`. For Foundation / SR Legacy / Survey (FNDDS), values are **per 100 g**.
  Macro nutrient IDs: protein **1003**, fat **1004**, carbs **1005**, energy **1008**
  (KCAL — filter by id + unit; kJ variants exist under other ids).
- **Measured latency: ~1.0 s** for a search from this machine. Real but acceptable for
  an SMS turn (turns already span seconds); it argues for the Phase E rule — climb to
  USDA only when the internal rungs came up empty — and for a hard client timeout.
- **Match quality:** "grilled chicken breast" returned sensibly ranked FNDDS entries
  (sauce/skin variants, scores ~650–700). The fuzzy-match problem is real but the
  `score` ordering was sane on the probe; top-3 candidates + model judgment (the same
  shape as Phase C) is adequate — no finding that blocks building.

### 4.2 Design consequences

- **Per-100g basis pairs with Phase A**: the tool reports per-100g macros; the model
  scales by its portion estimate (reference objects). No serving math in code — code
  can't see the plate.
- **dataType filter:** Foundation + SR Legacy + Survey (FNDDS). Branded is excluded —
  the branded long tail is Phase E's web-search rung; including it floods generic
  queries with label noise.
- **Failure envelope:** missing key / timeout (5 s) / 429 / any HTTP error → a clean
  "estimate normally" tool answer + loud log, never an exception into the loop; the
  meal always logs. Every call metered (latency + result count) via the logger.
- **Key config:** `USDA_API_KEY` env var, empty by default → tool answers
  "not configured" fallthrough (fails safe even if the flag is on without a key).

---

## §5 Phase E — routing surface, web rung, and prompt-cache layout

### 5.1 Preconditions confirmed

- **web_search is already a reactive rung**: `agent_loop.py` appends the server-side
  `web_search_20260209` tool under `WEB_SEARCH_TOOL_ENABLED` (max 3 uses), with query
  hygiene/verify rules in `voice.md`. Nothing to build — the routing prompt names it
  as the branded/restaurant long-tail rung, skeptically.
- **Heartbeat isolation undisturbed**: the heartbeat builds its own `system` from
  `voice.md` + `HEARTBEAT_PROMPT`; nothing in this phase touches either. The routing
  block ships like Phase A's — a separate file injected only by `run_agent_loop`.

### 5.2 Where the routing prompt can live (and cache)

Unlike Phase A's portion block (image turns only), routing applies to EVERY meal
turn, and meals arrive mostly as text — and there is deliberately no pre-classifier
to detect "meal turn" in code. So the block must ride all reactive turns when
enabled. Cost containment comes from the cache: the block is STABLE text, so it is
injected between the voice prefix and the volatile context with its own
`cache_control` breakpoint — the cached prefix becomes [voice, routing] (the
heartbeat's separate [voice]-prefix cache entry is unaffected; caching allows up to
4 breakpoints). Phase A's image-only block stays appended after the volatile
context, uncached. With caching disabled the string form concatenates in the same
order.

### 5.3 Tool availability vs the ladder

Any rung's tool can be flag-disabled or fail; the routing prompt says "skip a rung
whose tool isn't available this turn" and every handler already returns clean
"estimate normally" fallthroughs — the ladder degrades rung by rung, and a meal
always logs (except when the deliberate last rung — asking — is the right move).
