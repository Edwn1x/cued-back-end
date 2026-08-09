# Vision Thoroughness + Background-Job Truncation — Change Spec

Ground truth for what's-there-now: INVESTIGATION.md.

## Fix 1 — first-pass scene thoroughness on food photos

**What's there now.** `prompts/meal_estimation.md` (image turns only, gated by
MEAL_ESTIMATION_PROMPT_ENABLED, after the cache breakpoint) is pure
portion-accuracy guidance. Nothing instructs a frame sweep; the model tunnels on
the plated dish (Aug 8 live incident). Storage plumbing for the fix already
exists: remember→food_on_hand (TTL-aged, never constraints, never totals).

**What it becomes.** A new section in `meal_estimation.md` — read the WHOLE frame
on the first pass:

- Eaten (plated/consumed) → `log_meal`, portioned, as today.
- Other food/consumables visible (packaging, ingredients, groceries in the
  background) → `remember` with category `food_on_hand`, THIS turn. Scoped to
  coaching-relevant consumables — not every object in frame (over-capture guard).
- Say what you saw, briefly and naturally ("eggs logged — and I see you've got
  jam and bread there too"), not an inventory recitation.
- Never inflate: eaten macros reflect only what was eaten. Never claim to have
  logged what you only saw (honesty invariant), never claim the frame was empty
  when it wasn't.

Stable tier-1 marker phrase in the new section: **"whole frame"** (composition
tests key on it, same pattern as "reference object").

**Why no new flag.** The block is already kill-switched by
MEAL_ESTIMATION_PROMPT_ENABLED; the section is one file, revertible alone. A
per-paragraph flag is process noise for a prompt edit.

**Where.** `prompts/meal_estimation.md`; new fixture scene in
`tests/fixtures/generate_macro_fixtures.py` (+ committed PNG);
tier-1 additions in `tests/tier1/test_meal_estimation_prompt.py` (marker present
on image turn / absent off-flag — red-first) and a deterministic routing guard in
`tests/tier1/test_read_image.py` or a new file (scripted remember(food_on_hand) +
log_meal through the loop on an image turn: profile entry TTL'd, Meal row only the
eaten item — green regression guard, per playbook §II "not everything should be
red"); tier-2 `tests/tier2/test_vision_thoroughness.py` (judged, red-first):

1. Multi-item scene → meal logged AND on-hand captured (assert both).
2. Reply acknowledges the scene beyond the plate.
3. No inflation: uneaten items absent from the meal description; eaten calories in
   an eggs-only band.
4. Regression: plain plate_meal.png logs cleanly, zero food_on_hand writes.

## Fix 2 — background jobs: raise ceilings, gate stores on stop_reason

**What's there now.** Three background jobs store durable state with no
stop_reason check, at caps live logs show being hit (decisions 250, memory 600,
summary 600 — summary's cap sits INSIDE its asked-for output range, and it
advances the watermark even on a truncated store). Parse-failure is the only
accidental guard; a truncated-but-parseable output stores today.

**What it becomes.** In `app.py`, per site:

- Caps: decisions 250→1000, memory-extract 600→1500, summary 600→1500
  (worst-case output + headroom; rationale in INVESTIGATION.md). Plain constants
  at the call sites, mirroring how these sites are written today — no env knobs
  for one-shot background extractors.
- Immediately after `track_usage(...)`: if `response.stop_reason == "max_tokens"`,
  log `BG_JOB_TRUNCATED site=<name> user=<id> max_tokens=<cap>` (warning) and
  RETURN — nothing parsed, nothing stored. Summary: prior summary + watermark
  untouched, so the next cycle refolds the same cohort (self-healing retry).
  Extractors: the turn's extraction is discarded; the next exchange re-extracts.
  Discard-not-store is the spec's floor; in-process retry adds a second paid call
  on a path that self-heals — not built.
- `invalid=1`: investigated, structurally NOT truncation (increments only
  post-parse); already logged by name at the offending fact. No change.

**Where.** `app.py:113/115` (decisions), `app.py:310/312` (memory),
`app.py:472/474` (summary). Tier-1 `tests/tier1/test_bg_job_truncation.py`
(red-first):

1. Cap floors: stub records max_tokens ≥ 1000/1500/1500 per site.
2. `Truncated(<valid parseable output>)` per site → distinct BG_JOB_TRUNCATED
   log; decisions: no profile fields written; memory: user_profile_memory
   unchanged; summary: coaching_summary keeps prior value AND watermark does not
   advance.
3. Normal completion per site stores as before (regression).

## Out of scope (surfaced, not built)

Image re-look tool (companion spec); episodic.digest / extract_coaching_points /
legacy-router caps (no live stop= hits; episodic inherits this pattern if its
line ever fires); coach-reply first-block parsing in the summary job.
