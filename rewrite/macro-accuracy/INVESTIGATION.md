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
