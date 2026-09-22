# Aislinn macro accuracy — photo path fixes: summary (2026-09-22)

Spec: `CHANGESPEC.md` in this directory. Branch `aislinn-macro-photo-fixes` off `origin/main`
(8076127). Built in an isolated worktree (`.context/wt-aislinn`) because another session was
editing the same workspace checkout concurrently (`teen-eer-protein-basis-reported-maintenance`,
uncommitted edits to agent_tools.py / voice.md / macro_calculator.py).

## What changed (by file)

- `agent_tools.py`
  - `LOG_MEAL_TOOL`: `portion_guessed`, `from_app`, `slot`; description rewritten (all four
    macros; flag guesses; diary screenshots are printed-only and replace, never add).
  - `handle_log_meal`: §4 all-four-macros rejection (atomic, app rows exempt); §3 slot
    refusal for `from_app` writes (`LOG_MEAL_SLOT_REFUSED`), tagging `source="app"` /
    `app_reported` / `confidence="high"` / `from_app=` note; §2 `source` = photo/text/app
    from turn state; §1 `confidence="low"` + the "portions guessed … ONE line" affordance.
  - `MANAGE_LOG_TOOL` + `handle_manage_log`: `from_app` on edit flips provenance to app
    (audited) and appends the `PARITY` line + writes `Signal(kind="parity")`.
  - `_day_total_suffix`: keeps `Ng protein left of T`, adds "not a line after every log".
  - Helpers: `_canon_app`, `_meal_slot`, `_with_from_app_note`.
- `agent_loop.py`: `has_image` in the turn state; `(portion guessed)` / `(from their app)`
  markers on TODAY'S LOGGED MEALS rows.
- `prompts/voice.md`: diary-screenshot Images bullet (+ honest "connect my app" answer);
  protein-gap-once rule in Nutrition; "their tracker's total disagrees" rule replaces the
  old "name the item you likely under-counted" sentence (that sentence is what produced the
  wrong-culprit guess on Sep 18).
- `prompts/meal_estimation.md`: diary-screenshot hand-off header; flag-the-guess + name
  every guess in one line (log now, the image is gone next turn).
- `capabilities.py`: `app_screenshot` entry (rides on log_meal + vision; `used` = any
  `source="app"` meal).
- Fixtures: `tests/fixtures/diary_screenshot.png` (MyNetDiary lunch, 5 lines, 551 cal /
  54 P / 44 C / 17 F) and `diary_calories_only.png`; generator updated.
- Existing tier-1 fixtures that logged calories+protein only now pass all four macros.

No schema change, no new flag: rides on `LOG_MEAL_TOOL_ENABLED` / `MANAGE_LOG_TOOL_ENABLED` /
`READ_IMAGE_ENABLED` (all on in prod).

## Evidence

- Tier-1: `tests/tier1/test_aislinn_macro_photo.py` 15/15 (red first: 12 failed for the
  expected reasons, incl. the loop replay reproducing the Sep 19 981-cal double count);
  full tier-1 suite 821 passed, 2 skipped.
- Tier-2 (`tests/tier2/test_aislinn_macro_photo_live.py`, live model, 3/3 each):
  - a1 Sep 19 replay: 3/3 — one row, edited to 551/54, `source="app"`, reply names the miss
    ("i had it low at 430").
  - a2 breakfast photo: 3/3 — guessed portion flagged and named ("sized it as about a cup —
    off?").
  - a3 "my app says 1000 and 77g": 3/3 — lists all four items with numbers, asks which is
    off or for the screenshot, writes nothing; on "the oats were 550/46" edits the oats row
    and quotes 1000.
  - a4 mid-morning log: 3/3 — "logged, 208 cal 27g protein", no gap restated.
  - a5 calories-only screenshot: 3/3 — five app rows, protein NULL, reply says protein
    wasn't in the shot.
  - First run of a3/a4 failed on keyword checks only (no "?" in "which one's off, or just
    screenshot the app"; regex hit "29g total"); assertions were loosened to the semantic
    property per the plain-voice lesson, behavior was already right.

## Judgment calls (reversible)

- Slot refusal applies to ANY row in the slot (including an earlier app row), bypassed
  only by `saw_similar` naming it. `snack` never collides.
- Parity delta uses calories only; ±15% is the "close" line. Signal rows are written now
  so the bridge PR's graduation logic has data from day one.
- All-four-macros is a hard reject (nothing written) rather than a nudge: a NULL is a
  silent error in the totals, and the model re-calls in one round trip.
- The 173g protein target itself is untouched (founder policy call; another session is
  building the protein-basis change).

## Follow-ons

- Logger bridge PR (`rewrite/logger-bridge/CHANGESPEC.md` §1/§2/§4/§5-graduation/§6/§7):
  columns, the OTHER FOOD LOGGER block, `set_food_logger`, parity-suggest-once.
- Watch after deploy: `LOG_MEAL_SLOT_REFUSED`, `PARITY`, `LOG_MEAL_MACROS_MISSING`;
  Aislinn's next screenshot should produce one `source="app"` row.
