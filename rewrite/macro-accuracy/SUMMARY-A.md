# Phase A — Reference-Object Scaling — Summary

**Status: built, tier-1 green (196 passed, 2 pre-existing skips). Tier-2 written,
NOT RUN — no funded key in this workspace; 3 judged/live cases await the funded run.
Phase A is not "done" until they pass.** Flag `MEAL_ESTIMATION_PROMPT_ENABLED`
defaults off; merge of the whole feature branch held post-burn-in per the spec.

## What shipped

- **`prompts/meal_estimation.md`** (new): portion is the dominant uncertainty; size it
  from in-frame reference objects (plate ~10–11in, fork, fist/palm/thumb, cans,
  printed package sizes); read visible labels/packaging text FIRST (printed numbers
  are ground truth, scaled by amount eaten); put the estimated portion in the
  `log_meal` description so the entry is auditable and cheap to correct.
- **`config.py`**: `MEAL_ESTIMATION_PROMPT_ENABLED` (default false), by
  `READ_IMAGE_ENABLED`.
- **`agent_loop.py`**: the block is injected as an extra system element only when
  `image_data and READ_IMAGE_ENABLED and MEAL_ESTIMATION_PROMPT_ENABLED`, appended
  AFTER the cache breakpoint (voice-prefix cache unaffected); both caching branches
  handled. Loader mirrors `_voice_prompt` (module-cached).

## Judgment calls (findable, reversible)

1. **Not a `voice.md` edit — spec discrepancy surfaced.** The spec both targets "the
   meal-estimation prompt" and forbids `voice.md` changes; in the code those are the
   same file, and `heartbeat.py:260` ships `voice.md` as its cached prefix. Resolved
   with a separate injected block: `voice.md` byte-untouched, heartbeat surface and
   cache undisturbed, and the phase gets a real flag (a prompt edit alone couldn't).
   Details: INVESTIGATION.md §1.3.
2. **Image turns only.** Text-described meals could also use portion discipline, but
   Phase A per spec is the photo path; text turns pay zero tokens. Widening the block
   to text meals is a Phase E candidate, not silently added here.
3. **Portion-in-description** doubles as groundwork: Phase B history matching gets
   richer descriptions ("~2 cups rice") for free.
4. **Legacy `pending_photo_meal` path untouched** (not the live path; out of scope).

## Tests

- **Tier-1** (`tests/tier1/test_meal_estimation_prompt.py`, 6 cases, red-first —
  failed for the expected reason, flag absent, then green): block present on flagged
  image turns with voice block byte-identical to `voice.md` and only the voice block
  cached; absent when flag off / text-only / `READ_IMAGE_ENABLED` off; string-system
  branch covered; heartbeat tick's system never contains the block (isolation pin).
- **Tier-2** (`tests/tier2/test_meal_estimation.py`, 3 cases, **NOT RUN — no key
  here**): reference-object photo yields a *portioned* log (asserts portion basis in
  log/reply, not that the number is "right"); visible label beats eyeballing (two
  servings of the 240-cal granola → 430–530 cal, 10–15g protein logged); flag-off
  no-regression (plain photo still logs).
- **Fixtures**: `tests/fixtures/nutrition_label.png`, `plate_meal.png` — synthetic,
  committed, regenerable via `tests/fixtures/generate_macro_fixtures.py` (same trade
  as `tenders_label.png`, which the live model read correctly on the funded run).
  **First-funded-run risk:** the stylized plate may read as an illustration; if the
  reference-object case fails there, swap in a real photo before concluding the
  prompt failed.
