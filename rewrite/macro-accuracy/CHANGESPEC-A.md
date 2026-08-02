# Phase A — Reference-Object Scaling — Change Spec

Prompt technique only; no new deps, no new state, no `voice.md` change (INVESTIGATION
§1.3 — `voice.md` is shared with the heartbeat, so the estimation guidance ships as a
separate flag-gated system block on image turns). Merge held post-burn-in with the rest
of the feature branch.

## Change 1 — `prompts/meal_estimation.md` (new file)

- **What's there now:** the only photo-estimation instruction is one `voice.md` bullet
  ("estimate the meal + macros and log it"); portion — the dominant error term — has
  zero guidance, and label-reading is only directed for *not-yet-eaten* food.
- **What it becomes:** a short prompt block, in the coach's working register, stating:
  1. Portion/weight is the dominant uncertainty in a photo estimate — get portion
     right before worrying about anything else.
  2. Use in-frame reference objects as rulers: standard dinner plate ~10–11 in, fork,
     hand (fist ≈ 1 cup, palm ≈ 3–4 oz cooked protein, thumb ≈ 1 tbsp), standard
     cans/bottles, printed package sizes. Reason from the object to the portion.
  3. Read visible labels/packaging text FIRST when present — printed numbers (net
     weight, serving size, nutrition facts) are ground truth and beat any visual
     estimate; scale by how much was actually eaten. (Eaten-now complement of the
     existing not-yet-eaten `remember` routing, which is unchanged.)
  4. Make the portion visible in the log: put the estimated portion in the `log_meal`
     description ("chicken breast ~6oz, rice ~2 cups") so the entry is auditable and
     correctable — and so the user can correct the portion, not re-litigate macros.
- **Why:** spec Phase A — the cheapest high-value win; the technique runs *across* all
  later rungs (it's how portion is sized whatever source supplies per-unit macros).
- **Where:** new file `prompts/meal_estimation.md`. Deliberately NOT Phase E material:
  no escalation routing, no confidence-communication machinery.

## Change 2 — `config.py`: `MEAL_ESTIMATION_PROMPT_ENABLED`

- Default **false**. Placed with the image/tool flags next to `READ_IMAGE_ENABLED`.
- **Why:** per-phase flag (spec); a `voice.md` edit could not have one.

## Change 3 — `agent_loop.py`: inject the block on image turns only

- **What's there now:** `system = [voice (cached), context]`; image turns only differ
  in `user_content`.
- **What it becomes:** when `image_data and READ_IMAGE_ENABLED and
  MEAL_ESTIMATION_PROMPT_ENABLED`, append `{"type": "text", "text":
  <meal_estimation.md>}` as a third system block (module-cached read, same pattern as
  `_voice_prompt`). Appended AFTER the cache breakpoint → the voice prefix cache is
  untouched; non-image turns and the heartbeat (which composes its own system) never
  see it.
- **Why:** flag-gated, image-turn-only, zero heartbeat blast radius.

## Change 4 — tests

- **Tier-1 red-first** (`tests/tier1/test_meal_estimation_prompt.py`): block present as
  a system block on image turns when flagged on, with the voice block byte-identical to
  `voice.md`; absent when flag off / no image / `READ_IMAGE_ENABLED` off; heartbeat
  tick's system never contains it (isolation pin).
- **Tier-2 judged** (`tests/tier2/test_meal_estimation.py`), per spec:
  reference-object case (portion estimate reasons from the in-frame object — asserted
  on the logged description/reply referencing the object/portion basis, not on the
  number being "right"); visible-label case (label numbers used rather than eyeballed);
  no-regression case (plain food photo still logs via `log_meal`). New synthetic
  fixtures in the `tenders_label.png` style; fixture realism is a first-funded-run
  validation item. NOT RUN here (no funded key in this workspace).
