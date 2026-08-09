# Vision Thoroughness + Background-Job Truncation — Investigation

Spec: VISION_THOROUGHNESS_SPEC.md (two fixes, ship now). This records what the code
actually does before any change. Companion image-persistence/re-look tool is
explicitly NOT built here.

## Fix 1 — where first-pass thoroughness lives (and doesn't)

### The image turn's prompt surface

An inbound MMS becomes a vision block inline in the webhook (app.py:1008-1033) and
reaches `run_agent_loop` as `image_data`. On an image turn with the flags on
(READ_IMAGE_ENABLED + MEAL_ESTIMATION_PROMPT_ENABLED), agent_loop.py:316-318 appends
`prompts/meal_estimation.md` as a separate system block AFTER the cache breakpoint —
image turns only, never voice.md, heartbeat isolated (pinned by
tests/tier1/test_meal_estimation_prompt.py).

`prompts/meal_estimation.md` today is 100% portion-accuracy guidance: reference-object
scaling, label reading, portion-in-the-log. Its ONLY nod to the rest of the frame is
one parenthetical: "(Food not yet eaten still follows the normal routing: save the
details with remember, don't log it.)" — a routing rule for food the model already
decided to care about, not an instruction to sweep the frame. Nothing anywhere tells
the model to enumerate what else is visible. That is the whole gap: the Aug 8 live
incident (eggs logged; jam/egg whites/bread silently ignored, correctly read on
"look again") is under-instruction, not under-capability.

`prompts/meal_routing.md` (every reactive turn) is a source-of-macros ladder —
orthogonal, no scene-coverage language. Not the right home: thoroughness guidance is
vision-only and belongs with the image-turn block.

### The storage plumbing already exists (nothing to build, only to invoke)

- `REMEMBER_TOOL` (agent_tools.py:28-65) already: routes "groceries / food on hand
  not yet eaten" → `food_on_hand`, NEVER constraints; says a durable detail read off
  an image must be saved THIS turn (image gone next turn).
- `food_on_hand` is TTL-aged (config.FOOD_ON_HAND_TTL_DAYS=14; memory.py:736-738,
  swept at the top of apply_facts) — the non-immortal category from PR #24, pinned by
  tests/tier1/test_food_on_hand_ttl.py.
- Eaten totals come ONLY from Meal rows (`log_meal` → recompute_daily_totals);
  remember writes touch `user_profile_memory` only. Seen-but-not-eaten physically
  cannot inflate totals unless the model calls log_meal for it — which is exactly
  what the prompt must forbid and tier-2 must judge.

So Fix 1 is a prompt change on `meal_estimation.md` plus tests. No new flag: the
block is already gated by MEAL_ESTIMATION_PROMPT_ENABLED (kill switch), and a
one-file revert isolates the thoroughness section if it ever needs to come out alone.

## Fix 2 — the three background jobs and their ceilings

From the heartbeat-truncation call-site audit (rewrite/heartbeat-truncation/
INVESTIGATION.md §2, finding 4): the bounded-output extractors were left at their
caps ON PURPOSE, pending live stop= data — "the tick log shows which sites actually
hit their caps, which is the data a future raise would need." The Aug 7-8 logs are
that data. This is the planned follow-through, not a revert.

| site | model | max_tokens | asked-for output | truncation behavior today |
|---|---|---|---|---|
| app.py:63 `extract_and_store_decisions` | haiku-4-5 | **250** | 14-field flat JSON incl. a free-text `food_context` | `rindex("}")` can't repair a cut flat object → json.loads raises → generic "Decision extraction failed" → **silent total loss** of a one-shot capture (fields write only-if-empty) |
| app.py:203 `extract_and_store_memory` | haiku-4-5 | **600** | `{"facts":[...]}`, ~50-80 tok/fact, multi-fact on rich turns | same parse-fail path → silent loss of the turn's facts |
| app.py:382 `maybe_update_coaching_summary` | sonnet-4-6 | **600** | "keep under 400 words" structured summary — 400 words + markdown headers is ~550-700 tokens: **the cap sits inside the asked-for range** | `content[0].text` stores the PARTIAL summary AND advances `last_compressed_message_id` → the folded raw messages are permanently behind a corrupted summary. Worst of the three. |

None of the three inspects `stop_reason`. All three route through `track()` (so the
Aug 7-8 `stop=max_tokens` lines are theirs — the observability fix working), but
*handling* is still agent_loop + heartbeat only, exactly as the memory note says.

**Danger case the tests must pin:** a truncated response whose text still parses
(cut at a block boundary, or mid-closing-fence). Today all three would store it.
The rule to encode: check `stop_reason` BEFORE trusting the output, not after
parsing happens to fail.

### The `MEMORY_EXTRACT ... invalid=1`

Not truncation-caused, structurally: `stats["invalid"]` (memory.py:549-556) can only
increment AFTER `json.loads` succeeds — a truncated blob dies at the parse and never
reaches apply_facts. `invalid` means a parsed fact with empty `text` or an
out-of-list `category`, and both paths already log by name
(`MEMORY_INVALID_CATEGORY` / the no-text branch). Separate, minor, already
observable; the specific prod instance can be identified from those warning lines.
No code change; noted for the deploy check.

### Sizing the raises (worst-case output + headroom; output unbilled unless emitted)

- `extract_and_store_decisions` 250 → **1000**: all 14 fields populated + fences +
  a chatty food_context is ~300-400 tokens; 1000 is comfortable and still bounds a
  runaway.
- `extract_and_store_memory` 600 → **1500**: a dense first-real-conversation turn
  can legitimately emit 6-8 facts (~600+ tokens with fences).
- `maybe_update_coaching_summary` 600 → **1500**: the prompt's own ask (≤400 words
  structured) tops out ~700 tokens; 1500 covers a model that runs long, and the
  400-word instruction stays the real length governor.

### At-risk sites NOT raised here (scope guardrail — surfaced, not built)

- `memory.extract_coaching_points` (300) and `episodic.digest` (120): same class;
  no live `stop=max_tokens` observed. episodic's digest also advances a watermark
  on store — if its stop= line ever fires, it inherits this fix's pattern.
- Legacy-router extractors (meal/weight/onboarding, 200-400): dormant when
  SINGLE_AGENT_LOOP_ENABLED routes traffic to the loop.

## Test-infrastructure notes

- Tier-1 stubs via `tests/_fake_anthropic.py`; `Truncated(text)` already models
  stop=max_tokens with optional partial text — and crucially `Truncated("<valid
  json>")` models the parseable-truncation danger case.
- `maybe_update_coaching_summary` needs ≥16 post-watermark messages
  (_RAW_BUFFER_BEFORE_COMPRESS=8 + _MIN_NEW_TO_COMPRESS=8) before it calls the
  model — test setup seeds Message rows.
- Tier-2 fixtures are synthetic PIL scenes (tests/fixtures/generate_macro_fixtures.py);
  Fix 1 needs a new multi-item breakfast scene mirroring the live incident (plated
  eggs + jam jar + egg-white carton + bread bag, packaging labeled so identification
  is deterministic). Fixture realism remains the known first-run risk — swap a real
  photo before concluding the prompt failed.
- No `.env` in this workspace; tier-2 runs after copying one from a sister workspace
  (founder-approved procedure, 2026-08-06).
