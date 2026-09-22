# Aislinn macro accuracy — photo path fixes (change spec, 2026-09-22)

Founder question: "why are her macros so inaccurate?" Answer from the prod rows (user 32,
Sep 18–22): every gram-weighed TEXT log she typed checks out against USDA within a few
percent. Every miss came from a photo, and she is a weigh-everything MyNetDiary user who
sees each one. This PR fixes the photo path and the reconciliation with her own app. It
does NOT build the logger bridge (`rewrite/logger-bridge/CHANGESPEC.md` §1/§2/§4/§5
graduation): §3 of that spec IS fix 2 here and is implemented to its shape, minus the
`food_logger` columns, so the bridge PR builds on this instead of replacing it.

Per the playbook: what's-there-now / what-it-becomes / why / where; tests red-first
(tier-1 `tests/tier1/test_aislinn_macro_photo.py`, tier-2 anchors
`tests/tier2/test_aislinn_macro_photo_live.py`, binary 3/3).

## 0. The three misses (prod rows, Pacific time)

1. **Sep 18, "your app says 1000/77", coach had 800/50.** The coach guessed which item was
   off (the McMuffin), was wrong (the gap was the "protein oats" it never asked about), told
   her a new muffin number and never wrote it (row 180 still 260/22). The write-back guard
   (PR #79) now covers the "I said a number" case; it does not cover "your day total is
   wrong" — there is no rule for reconciling against the user's own tracker.
2. **Sep 19, the sandwich double-log.** Photo → "chicken + egg sandwich 430/43" (row 184).
   Her MyNetDiary screenshot of the SAME sandwich → logged as a second meal (row 185,
   `saw_similar=[184]`), day read ~980, "over 1400" false alarm, four turns of arguing,
   184 deleted by hand. The read-before-write judgment was the model's alone; the code
   wrote whatever it decided.
3. **Sep 22, breakfast photo.** "~2 eggs, ~3/4 cup yogurt, whole grain toast, 740/38" →
   truth after THREE corrections: 1 egg, 145g yogurt, 90g egg whites (missed entirely),
   sourdough. The row descriptions carried the guessed portions (meal_estimation.md
   already asks for that) but the REPLY named only two of them ("how many eggs and how
   much yogurt"), so she corrected one thing per turn.

Plus two small ones: row 245 landed with NULL carbs/fat (silently dropped from the day's
carb/fat sums), and the "134g protein to go" line followed every single log.

## 1. Portion provenance: a guessed portion is flagged, and the reply names every guess

- **Now.** `log_meal` has no notion of how a number was arrived at. `Meal.confidence`
  exists (`high|medium|low`) and is never written. The reply names SOME guesses.
- **Becomes.** `log_meal` items gain `portion_guessed: bool` — true when the model sized
  a portion by eye (a photo count of eggs, a spoon of yogurt, spread on toast) rather
  than from a stated amount or a printed number. Code stores it as
  `Meal.confidence="low"`. The tool result appends
  `| portions guessed: 'scrambled eggs (~2)' [id 243], 'greek yogurt (~3/4 cup)' [id 244]
  — say every guessed portion in ONE line so they can fix any of them at once; a
  correction is a manage_log edit on that id` (affordance-in-tool-result, the lesson that
  actually changes behavior). TODAY'S LOGGED MEALS renders `(portion guessed)` on those
  rows so a later correction lands on the right id. `meal_estimation.md` says: log NOW
  with the guess flagged (the image is gone next turn — asking first loses the frame),
  then name every guess in one line; never ask about two items and leave the third.
- **Why.** The user corrects one line instead of three turns; code knows which rows are
  soft when a screenshot or a "nah bruh" arrives.
- **Where.** `agent_tools.py` (`LOG_MEAL_TOOL`, `handle_log_meal`), `agent_loop.py`
  (meals block), `prompts/meal_estimation.md`.

## 2. Source provenance: photo / text / app, finally kept

- **Now.** `handle_log_meal` hard-codes `source="text"`; the photo/text distinction the
  model comment promises is lost.
- **Becomes.** `run_agent_loop` records `has_image` in the per-turn state (`begin_turn`);
  `handle_log_meal` writes `source="photo"` on an image turn, `"app"` when `from_app` is
  set (§3), `"text"` otherwise.
- **Where.** `agent_loop.py`, `agent_tools.py`.

## 3. Diary screenshot: the other app's numbers replace the estimate (logger-bridge §3)

- **Now.** No image rule for a food-app diary screenshot; the model improvises. Code
  writes whatever the model decides (Sep 19: a second row).
- **Becomes.**
  - `log_meal` and `manage_log` gain `from_app` (canonical id: `myfitnesspal |
    mynetdiary | cronometer | loseit | macrofactor | other`). `log_meal` also gains an
    optional `slot` (`breakfast|lunch|dinner|snack`) for when the screenshot labels it.
  - **Slot check in code.** A `log_meal` with `from_app` whose slot already holds an
    active row today (any source) not named in `saw_similar` returns
    `error: <slot> already has [id 184] 'chicken + egg sandwich' 430cal/43g (your own
    estimate, portion guessed). A diary screenshot of the same meal IS that food even
    when the names differ (you guessed chicken, the app says turkey) — call manage_log
    edit on id 184 with the printed numbers + description, from_app set. Only if they
    ate BOTH, re-call log_meal with saw_similar=[184].` and writes nothing. Slot = the
    user's local meal window from `eaten_at` (breakfast < 11:00, lunch 11:00–16:00,
    dinner ≥ 16:00; `slot` overrides). Multi-item calls check the call's slot once.
  - A `from_app` write (either tool): `source="app"`, `log_type="app_reported"`,
    `confidence="high"`, `notes` carries `from_app=<id>`; the result appends
    `| source: their <app> screenshot`. Printed-only: blanks stay NULL (the carbs/fat
    requirement in §4 is skipped for app rows); the prompt says a guessed macro next to
    a printed one is named as a guess in the same sentence.
  - **Parity (logger-bridge §5, the suffix only).** A `manage_log edit` with `from_app`
    that overwrites a row's calories appends `| PARITY: you had this at 430 cal, their app
    says 551 (+28%) — say which way you were off in one clause; never argue with the
    app's number` (or `… (−5%) — close; mention it once if it fits`), and writes a
    `Signal(kind="parity", payload={meal_id, cued_cal, app_cal, delta_pct})`. The
    suggest-once / graduation logic stays in the bridge PR.
  - voice.md Images gains the diary-screenshot bullet (printed numbers ARE the log; one
    `log_meal` with `items`; replace-don't-add; printed-only; no comment on the app) and
    the honest answer to "can i connect my app?" (no connection; screenshot the meal or
    the day and it's taken as printed). `meal_estimation.md` gets the one-line header:
    a diary screenshot is not a plate — stop, follow the voice.md rule.
  - `capabilities.py` gains `app_screenshot` (rides on `log_meal`; `used` = any
    `source="app"` meal).
- **Why.** The Sep 19 judgment failed with the affordance in prose; the slot check moves
  the load-bearing part to code and hands the model the right tool in the error.
- **Where.** `agent_tools.py`, `prompts/voice.md`, `prompts/meal_estimation.md`,
  `capabilities.py`, `models.py` (no schema change: `Signal` and `Meal.confidence` exist).

## 4. Estimates carry all four macros

- **Now.** `log_meal` accepts calories/protein only; row 245 (90g egg whites) has NULL
  carbs/fat and today's carb/fat totals silently omit it.
- **Becomes.** A non-app item missing any of calories / protein_g / carbs_g / fat_g is
  rejected atomically: `error: '90g egg whites' needs calories, protein_g, carbs_g and
  fat_g (0 is fine) — a blank silently drops out of the day's totals`. App rows are exempt
  (printed-only). Existing tier-1 tests that log calories+protein only are updated.
- **Where.** `agent_tools.py` (`handle_log_meal`); `tests/tier1/*` fixtures.

## 5. The protein gap is said once, not after every log

- **Now.** `_day_total_suffix` hands the model `134g protein left of 173` on every write
  and the model repeats it after every item ("still 134g to go").
- **Becomes.** The suffix keeps the number (the model needs it when asked) but the
  instruction changes: `quote the total; the protein-left figure is for when they ask, at
  the evening meal, or when planning what to eat — not a line after every log`. voice.md
  nutrition adds the same rule in one sentence.
- **Where.** `agent_tools.py`, `prompts/voice.md`.

## 6. "Your app says X": reconcile item by item, never guess the gap

- **Now.** No rule. Sep 18 the coach blamed the wrong item and wrote nothing.
- **Becomes.** voice.md "Correcting a logged entry" gains: when they quote their own
  tracker's day total against yours, their number wins; don't name a culprit; list your
  logged items with numbers in one line and ask which is off (or ask for the screenshot);
  write whatever they answer with `manage_log edit`, then quote the fresh total.
- **Where.** `prompts/voice.md`. Tier-2 anchor only (model behavior).

## Out of scope (deliberately)

- `food_logger` columns, the OTHER FOOD LOGGER context block, `set_food_logger`,
  graduation, adaptive-targets explain text — logger-bridge PR, builds on this.
- The 173g protein target (1g/lb on 1400 cal): founder policy call, not a bug.
- Seeing the egg whites the model missed in the Sep 22 photo: not fixable by rule.

## Tests

Tier-1 (`tests/tier1/test_aislinn_macro_photo.py`, fake SDK, refetch between turns):
- t1 `portion_guessed` → `confidence="low"`, result names each guessed item with its id
  and the one-line instruction; the meals block shows `(portion guessed)`.
- t2 source provenance: image turn → `photo`; text turn → `text`; `from_app` → `app` +
  `app_reported` + `high` + `from_app=` note + `source:` suffix.
- t3 slot refusal, the Sep 19 replay through the loop: estimated lunch row; `log_meal`
  `from_app` same slot → error names the id, writes nothing; the follow-up `manage_log
  edit` with `from_app` lands 551/54 on THAT row, one row total, totals 551, `PARITY`
  `+28%` in the result, a `Signal(kind="parity")` row. `saw_similar` naming the row
  bypasses; a different slot writes; an explicit `slot` overrides the clock.
- t3b partial macros with `from_app` → protein NULL, no §4 error.
- t4 §4: missing fat_g → error, nothing written; batch with one missing → nothing written.
- t5 suffix wording pin; voice.md / meal_estimation.md / MANAGE_LOG schema pins.
- t6 capability coverage (existing test) passes with the new entry.

Tier-2 (`tests/tier2/test_aislinn_macro_photo_live.py`, live model, 3/3):
- a1 Sep 19 replay with a synthetic MyNetDiary lunch screenshot: one row remains,
  calories == 551 (printed, not re-estimated), `source="app"`, reply quotes 551-ish total,
  never ~980.
- a2 breakfast photo (existing `breakfast_scene.png`): every `confidence="low"` row's
  food word appears in the reply with a portion marker (the one-line correction).
- a3 "nah bruh my app says 1000 and 77g": no row edited, reply names ≥2 logged items and
  asks; then "the oats were 550 and 46g" → oats row edited to 550/46, reply quotes 1000.
- a4 protein-once: a morning log after the gap was already said today → the reply does
  not restate a protein-remaining figure.
- a5 calories-only screenshot: app rows have protein NULL; any protein number in the reply
  is marked as a guess in the same sentence.

## Rollout

No new flags: every change rides on `LOG_MEAL_TOOL_ENABLED` / `MANAGE_LOG_TOOL_ENABLED` /
`READ_IMAGE_ENABLED` (all on in prod). Deploy, then watch for `LOG_MEAL_SLOT_REFUSED`,
`PARITY`, and `LOG_MEAL_MACROS_MISSING` lines for a week; Aislinn's next screenshot is the
live check (one row, `source="app"`).
