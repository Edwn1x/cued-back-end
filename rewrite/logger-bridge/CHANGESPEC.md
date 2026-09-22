# Logger bridge — change spec (2026-09-21)

Goal (founder, 2026-09-21): users should switch completely from their app food logger
(MyFitnessPal, MyNetDiary, Cronometer, Lose It) to cued. Some will trust their app more at
first and keep it running while they try cued. The bridge lets them do that without cued
punishing the overlap, gets their numbers in cheaply, and makes dropping the other app the
obvious move once cued has proved parity. It is a prompt-and-state PR: no OAuth, no
aggregator, no touch on the integrations stack (#72/#76).

Why not a real sync: MyFitnessPal's partner API is closed to new applicants, MyNetDiary and
Cronometer have no user-data API, and the only web-only route (Terra) starts near $400/mo.
Apple Health needs a native app. See the 2026-09-21 assessment in the session log.

Per the playbook: what's-there-now / what-it-becomes / why / where. Tests red-first
(tier-1 in `tests/tier1/test_logger_bridge.py`, tier-2 anchors in
`tests/tier2/test_logger_bridge_live.py`, binary 3/3 per [[binary-anchors]]).

## 0. What the code already knows (and wastes)

- Onboarding asks "fitness apps or wearables they currently use" and the extractor writes
  `users.existing_tools` ("strava,apple_watch") and `users.tools_decision`. The onboarding
  prompt's vocabulary for `tools_decision` is `integrate | acknowledged | none`; the column
  comment in `models.py` says `migrate | coexist | none`. Two vocabularies, one column,
  neither honored anywhere.
- **Neither column reaches the coach.** `git grep` finds them only in `onboarding_agent.py`
  (writes), `app.py:3150` (admin display) and the migration. `build_loop_context`, the
  heartbeat, and `_known_gaps` never see them. A user who said "i use myfitnesspal" at
  signup is coached as if they'd said nothing.
- `Meal.source` is `"text" | "photo"` in the comment but `handle_log_meal` hard-codes
  `source="text"` on every tool write, so the photo/text distinction is already lost.
- voice.md's Images section routes food photos, calendar screenshots, scale screenshots,
  and Strava cards. A screenshot of another app's food diary has no rule: the model either
  re-estimates the food from a list of names (wrong, the app already has the numbers) or
  treats it as "anything else".
- `adaptive_targets` needs 10 of 14 days with 2+ logged meals. A coexisting user who logs
  in MFP starves the biweekly cycle silently, and the explain line blames "a logging gap".

## 0b. The live incident this is for (user 32, Sep 19, admin log + prod DB + deploy logs)

Aislinn (founder's sister) uses MyNetDiary. What actually happened, from the rows:

1. **Onboarding stored the wrong app.** She typed "net dairy" (MyNetDiary); the extractor
   wrote `existing_tools="myfitnesspal"`, `tools_decision="acknowledged"`. Neither reached
   the coach anyway (§0).
2. **12:08 PT** she sent a breakfast PHOTO. Coach estimated it as "chicken + egg sandwich,
   430 cal / 43g" → meal 184.
3. **13:17** "Can I connect my calorie tracking app?" → "No app connection."
4. **13:18** she sent the MyNetDiary breakfast screenshot (turkey + egg-white sandwich,
   551 cal / 53.5g). The model saw meal 184, judged "turkey ≠ chicken" and logged it as a
   DISTINCT serving (meal 185, `saw_similar=[184]`), so the day read ~980 instead of 551.
   The screenshot apparently showed calories but not the macro line, so the model **filled
   protein with its own estimate (39g) and said "logged it, 551 cal 39g protein"** as if
   both numbers came from the app.
5. **13:20** second screenshot (macro view) → `manage_log` edit 185 to 54g. The duplicate
   184 stayed.
6. **17:10** lunch screenshot → "that puts u over 1400" → "Woah how's it over 1400????" →
   "No bro I only had the 551 plus 626" → 184 deleted. Four turns of her arguing with a
   number that was wrong because of step 4.
7. **20:08** "Dinner for today" arrived with NO image at the backend (no `AGENT_LOOP_IMAGE`
   line at 03:08 UTC): a lost photo upstream, not a coach error. The coach read it as a
   request and suggested dinner; "Did u log my dinner" / "That was dinner" / "Bruh what"
   followed until she re-sent the screenshot at 21:10.
8. Both screenshot meals are stored with `source="text"` (the hard-coded value).

Two rules fall directly out of this, and they change §3 and §5 below:

- **A diary screenshot for a meal that's already estimated REPLACES the estimate.** It is
  the same food; the app's numbers win; edit the existing row, never add a second one. The
  plate estimate misnaming the protein (chicken vs turkey) is exactly why "different
  description ⇒ different meal" can't be the test. The test is the meal SLOT: same local
  day, same meal window (or the screenshot's own breakfast/lunch/dinner label), an existing
  estimated row ⇒ replace.
- **Printed beats guessed, and the two never mix in one sentence.** If the screenshot shows
  calories but not macros, log calories as printed and macros as null (or flagged
  estimates), and say so: "551 from ur app; protein i'd guess ~50, send the macro view if
  u want it exact". Never present an estimate beside a printed number as if both were read.

## 1. The `food_logger` state

- **Now.** `existing_tools` is a free-text list with wearables mixed in; `tools_decision`
  is a dead field with two vocabularies.
- **Becomes.** Two new columns on `users`, plus one timestamp:
  - `food_logger` VARCHAR(30): a canonical app id when they still log food elsewhere —
    `myfitnesspal | mynetdiary | cronometer | loseit | macrofactor | other`. NULL = they
    don't (or never said).
  - `food_logger_status` VARCHAR(12): `coexist` (still using it alongside cued) |
    `switched` (they said they dropped it, or 14 days with zero screenshot/app-sourced
    meals and 10+ cued-logged days). NULL when `food_logger` is NULL.
  - `food_logger_since` DateTime: when `coexist` was set; drives the graduation math.
  Three writers, in precedence order:
  1. Onboarding: the existing `existing_tools` extraction gains a `food_logger` key using
     the canonical ids (the extractor prompt maps "mfp", "my fitness pal", "MFP" →
     `myfitnesspal`; "net diary", "net dairy", "mynetdiary" → `mynetdiary`; the live
     miss was MyNetDiary stored as `myfitnesspal`). Setting it sets `status=coexist`
     and `since=now`. Also answer "can i connect my app?" honestly from this state: not
     a connection, but "screenshot ur day and i'll take it" (voice.md line, §3). The dead
     `tools_decision` vocabulary is unified to `integrate | acknowledged | none` (the prompt's
     version; fix the model comment) and left otherwise alone.
  2. A new tool, `set_food_logger` (§4), for mid-conversation changes: "im gonna keep using
     mfp for now" → coexist; "deleted mfp lol" / "just using u now" → switched.
  3. Code: the graduation rule above flips `coexist → switched` in the nightly consolidation
     pass and logs `FOOD_LOGGER_GRADUATED user=… app=… days=…`. One line, human-readable,
     per [[phase5-consolidation-human-readable-audit]].
  The memory extractor's stating-verb floor already accepts "uses MyFitnessPal" as a fact;
  that stays as belt-and-braces but the column is the source of truth (column-as-source-of-
  truth rule, `memory.py` header).
- **Why.** The coach can't behave differently for a coexisting user unless code tells it
  the user is one. A column beats a memory fact: it gates heartbeat signals and the adaptive
  loop in code, not in prose.
- **Where.** `models.py`, `migrate.py` (two ALTER ADD COLUMN, nullable, no default: zero
  lock risk per [[deploy-lock-pileup-incident]]), `onboarding_agent.py` (extraction schema +
  examples), `consolidation.py` (graduation), `admin_system.py` auto-surfaces the columns.

## 2. Context: the coach knows, and stops treating the overlap as a gap

- **Now.** An empty TODAY'S LOGGED MEALS block plus a 0-cal TODAY'S TOTALS reads as "they
  haven't eaten / haven't logged" on every reactive turn and every heartbeat tick. For a
  coexisting user that is false half the time.
- **Becomes.** `build_loop_context` appends, only when `food_logger_status == "coexist"`:
  ```
  ## OTHER FOOD LOGGER (code-computed)
  They still log food in MyFitnessPal alongside you (since Sep 18, 3 days). An EMPTY day
  here is NOT an unlogged day — it's probably in their app. Never ask them to re-type
  what they already logged there: ask for a screenshot of the day (one tap, you read the
  numbers off it) or an end-of-day total. Last screenshot-sourced day: Sep 20.
  Screenshot days in the last 7: 2. cued-only days in the last 7: 1.
  ```
  The block is rendered by `_food_logger_block(user, session)` from Meal rows
  (`source="app"`, §3) — no model arithmetic. The heartbeat inherits it via
  `build_loop_context`; HEARTBEAT_PROMPT gains one line: a coexisting user's empty day is
  never a standing condition; the only food nudge allowed is a once-a-day evening
  "send me ur mfp day when ur done" and only if no screenshot arrived today.
  `_known_gaps` adds "today's food is probably in their app — not a gap" so the model
  doesn't spend its one follow-up asking "what have you eaten".
- **Why.** The heartbeat and reactive loop both nag on the empty day today; that nag lands
  on exactly the cautious user we're trying to keep. Precompute what the model would
  otherwise guess (days since, counts).
- **Where.** `agent_loop.py` (`_food_logger_block`, `_known_gaps`), `heartbeat.py`
  (HEARTBEAT_PROMPT line), voice.md §"Other food logger" (short: the four rules above).

## 3. Screenshot logging: the other app's diary is ground truth, not a plate

- **Now.** voice.md Images: food photo → estimate; label → printed numbers are ground truth
  "scaled by how much they ate"; nothing about a diary screenshot. `meal_estimation.md`
  is portion-first, which is the wrong frame for a screenshot: the portion is already
  decided and the numbers are printed.
- **Becomes.** A new Images bullet in voice.md, above "Anything else":
  - **A screenshot of another food app's diary** (MyFitnessPal, MyNetDiary, Cronometer,
    Lose It — a list of foods with calories per line and a day or meal total) → the
    printed numbers ARE the log. Do not re-estimate, do not "sanity check" their app's
    calories against your own guess, do not comment on the app. Call `log_meal` once with
    `items` = every visible line (description as printed, calories/protein/carbs/fat as
    printed, blanks left null), `date` if the screenshot shows a day that isn't today,
    and `from_app` = the app id. **Replace, don't add:** if TODAY'S LOGGED MEALS already
    has an estimated meal in the same slot (a photo/text row from the same meal window,
    or the slot the screenshot itself labels — "Breakfast"), that row IS this food even
    when your description differs (you estimated "chicken", the app says "turkey": same
    sandwich). Call `manage_log edit` on that row with the printed numbers and the
    printed description instead of `log_meal`. Only lines with no estimated counterpart
    get logged new. **Printed only:** log exactly the numbers you can read; a macro the
    screenshot doesn't show stays null, and if you offer a guess for it, say it's a
    guess in the same sentence ("551 from ur app, protein i'd guess ~50"). Reply with
    the day total from the tool result and one line of coaching if there's one to give;
    never a recap of the list.
  `meal_estimation.md` gets a one-paragraph header: "If the image is a screenshot of
  another app's diary, stop here — nothing below applies; follow the voice.md rule."
  `log_meal` and `manage_log` schemas gain `from_app` (string, optional, canonical id).
  Code, on either write: the slot check is enforced in code too — a `log_meal` with
  `from_app` whose slot already holds an estimated row returns
  `error: slot already has meal <id> (your estimate) — manage_log edit it with these
  numbers instead` rather than writing (the same affordance-in-tool-result shape as the
  usda lookup). Slot = the user's local meal window from `eaten_at` (breakfast < 11:00,
  lunch 11:00–16:00, dinner ≥ 16:00, overridable by an explicit `slot` field when the
  screenshot labels it). On a screenshot write:
  `source="app"`, `log_type="app_reported"`, `confidence="high"`, `notes="from_app=<id>"`,
  and the tool result appends `| source: their <app> screenshot` so a later manage_log
  edit knows what it's editing. A screenshot-sourced write on a user with
  `food_logger IS NULL` sets `food_logger=<id>, status=coexist, since=now` (the
  screenshot is the strongest evidence they still use it). Photo-sourced meals also get
  `source="photo"` while we're in there (the comment's promise, finally kept).
- **Why.** Zero integration work, works for every app, and it's what a skeptical user does
  anyway. The vision path reads printed numbers far better than it estimates plates.
  Tagging the source is what makes §5 (parity) and §1 (graduation) computable.
- **Where.** `prompts/voice.md`, `prompts/meal_estimation.md`, `agent_tools.py`
  (`LOG_MEAL_TOOL`, `handle_log_meal`), `capabilities.py` (one new entry: "send me a
  screenshot of your mfp day and i'll log the whole thing", rides on `log_meal` +
  `READ_IMAGE_ENABLED`, relevance 9 when `food_logger` is set, `used` = any
  `source="app"` meal). The coverage test enforces the entry.

## 4. `set_food_logger` tool

- **Now.** No way for the coach to record "im keeping mfp for now" or "deleted it".
- **Becomes.** `SET_FOOD_LOGGER_TOOL` `{app: id, status: coexist|switched}`, flag
  `SET_FOOD_LOGGER_TOOL_ENABLED`, capability entry, voice.md routing line under
  "Remembering vs scheduling": an app they log FOOD in is state, not memory — set it with
  the tool, never remember. `switched` clears the §2 block on the next turn and writes one
  memory fact "switched to cued from <app> on <date>" (dated, the one thing worth
  remembering about it). The handler is 20 lines; result string names what changed.
- **Why.** [[response-shape-seam]] #18 lesson: offer a tool for BOTH branches, or the model
  narrates the state change and nothing persists.
- **Where.** `agent_tools.py`, `agent_loop.py` (flag-gated tool add), `config.py`,
  `capabilities.py`, `prompts/voice.md`.

## 5. Parity: the graduation mechanism

- **Now.** Nothing compares cued's estimate to their app's number, so the user never gets
  the evidence that would let them drop the app.
- **Becomes.** Code, not prose. The §3 replace is the parity event: when a `manage_log`
  edit with `from_app` overwrites an estimated row, code computes the delta between the
  old (cued) and new (app) calories before writing and appends to the tool result:
  ```
  | PARITY: you had this at 430 cal, their app says 551 (+28%). Say which way you were
  off, plainly, in one clause — never argue with the app's number.
  ```
  or, when close: `| PARITY: you had 640, app says 610 (−5%) — close. Mention it once
  if it fits, don't make a thing of it.` No model arithmetic. A `Signal(kind="parity")`
  row records `{meal_id, cued_cal, app_cal, delta_pct}`. (Sep 19 would have produced
  a +28% on breakfast — a true miss, said once, instead of a double-count she had to
  catch herself.) When the user has
  3+ parity signals within ±15% and no >20% miss in the last 7 days, the §2 block gains
  one line: "Parity: 3 of 3 close this week. Suggest ONCE, casually, that they can drop
  the app — then never again unless they ask." A `parity_suggested_at` stamp on the
  user (JSON in `session_state`, not a column) enforces "once".
- **Why.** The conversion moment is the user seeing cued land within a few percent of
  their app a handful of times. Make it an observed fact the coach can say, not a claim.
  The >20% branch is the honesty invariant: a real miss gets written back, never
  papered over ([[coach-corrects-number-but-never-writes-it]]).
- **Where.** `agent_tools.py` (`_parity_suffix`), `models.Signal` (existing table, new
  kind), `agent_loop.py` (§2 block line), `prompts/voice.md` (the once rule).

## 6. Adaptive targets: don't blame a coexisting user for a "logging gap"

- **Now.** `adaptive_targets.run_cycle` counts a day only with 2+ meals; a coexisting
  user's app days count as zero, the cycle returns "only 3 of 14 days…", and the explain
  line says "if it's a logging gap, say so once".
- **Becomes.** App-sourced meals count like any other (they're meals; §3 tags them as
  such, so no change to the counting query beyond not filtering by source). The explain
  text for a coexisting user names the real fix: "send a screenshot of each mfp day and
  the cycle can run". No change to the 10-of-14 gate itself.
- **Why.** The gate is "the whole game" per the module header; the fix is feeding it, not
  loosening it.
- **Where.** `adaptive_targets.py` (explain branch only).

## 7. Onboarding: say the bridge exists, once, in the friend's voice

- **Now.** When the user names MFP at signup the coach acknowledges and moves on.
- **Becomes.** When `food_logger` is set during onboarding, the kickoff's capability
  rundown (`_send_capability_rundown`, already model-written from `rundown_context`)
  ranks the §3 entry first for this user: "keep mfp if u want — screenshot ur day and i'll
  take it from there". No new bubble, no new prompt: it's a relevance score.
- **Where.** `capabilities.py` (relevance lambda).

## 8. Out of scope (deliberately)

- Export-file import at onboarding (history → baseline targets). Real, separate PR: needs
  a file upload surface cued doesn't have on iMessage/SMS.
- Apple Health Shortcut bridge. Founder-phone experiment first.
- Terra / any OAuth nutrition sync. Revisit at a user count that justifies the floor cost.
- The integrations stack (#72, #76) is untouched.

## Tests

Tier-1 (`tests/tier1/test_logger_bridge.py`, fake SDK, real loop per
[[harness-fidelity-two-failure-modes]] — refetch between turns):
- t1 screenshot turn: `ToolUse("log_meal", {items: [...3 lines], from_app: "myfitnesspal"})`
  → 3 Meal rows `source="app"`, `log_type="app_reported"`, totals = printed sum, and the
  user flips to `food_logger=myfitnesspal / coexist` from NULL.
- t2 replace-not-add (the Sep 19 case): an estimated breakfast row exists (430/43g,
  "chicken + egg sandwich"); the model calls `log_meal` with `from_app` for the same slot →
  code returns the slot error naming the row and writes nothing; the model's follow-up
  `manage_log edit` with `from_app` lands 551/54g on THAT row, `source="app"`, one row
  total, totals = 551, and the tool result carries `PARITY … +28%`.
- t2b partial macros: `log_meal` with `from_app`, calories only → protein/carbs/fat NULL,
  never a model guess written as a number; the TODAY'S TOTALS block renders protein as
  "unknown for 1 meal", not 0.
- t3 `_food_logger_block` renders only for `coexist`; day counts are right across a local
  midnight; NULL/`switched` → absent.
- t4 heartbeat: a coexisting user with an empty day and no screenshot today gets the block,
  and the fixture decide-turn is allowed exactly one evening ask (fixture: 21:00 local, no
  screenshot → send_text permitted; 09:00 local → the block says don't).
- t5 `set_food_logger` both branches persist; `switched` writes the dated memory fact and
  the block disappears on the next `build_loop_context`.
- t6 parity suffix: cued 640 vs app 610 → "−5%" and "close"; 640 vs 480 → ">20%" branch
  text; Signal row written; 3 close + 0 misses → the once-line appears, and
  `parity_suggested_at` suppresses it on the next render.
- t7 graduation: 14 days, 0 app-sourced meals, 10+ cued days → `switched`, one
  human-readable log line; 13 days → unchanged.
- t8 adaptive: a window of app-sourced days counts; the explain line names the screenshot
  fix for a coexisting user.
- t9 capability coverage: `set_food_logger` and the screenshot entry exist (the existing
  test fails the build otherwise).
- Migration test: both ALTERs validate against Postgres, 0 lock waits (existing harness).

Tier-2 anchors (`tests/tier2/test_logger_bridge_live.py`, live model, binary 3/3):
- a1 a real MFP diary screenshot (fixture image, 4 lines + total) → one `log_meal` with
  `items` matching the printed lines within 0 cal (no re-estimation), `from_app` set, reply
  ≤ 2 lines, no recap of the list, no comment on the app.
- a2 the Sep 19 replay: a photo-estimated "chicken + egg sandwich 430" already logged,
  then a MyNetDiary breakfast screenshot showing "turkey + egg white … 551" → the reply
  quotes 551 as the day total (never ~980), one row remains, and the reply names the miss
  in ≤ 1 clause without arguing with the app.
- a2b calories-only screenshot → the reply never states a protein number as fact; if it
  offers one it's marked as a guess in the same sentence.
- a3 coexisting user, empty day, 09:00 tick → `stay_silent` with a reason that names the
  other app; 21:00 tick, no screenshot → one ask, ≤ 1 sentence, contains "screenshot" or
  the app name.
- a4 "ok im deleting mfp, just using u" → `set_food_logger(status=switched)` fires, reply
  doesn't gloat or list features.
- a5 parity: a turn whose tool result carries the −5% line → the reply mentions it in ≤ 1
  sentence with the direction right; a turn with the >20% line → the reply asks which is
  right and (next turn, on "the app's right") calls `manage_log edit` on the cued meal.

## Rollout

All behind flags, default off: `FOOD_LOGGER_BRIDGE_ENABLED` (block + voice rules + parity),
`SET_FOOD_LOGGER_TOOL_ENABLED`. The migration ships with the code. Flip for the founder
(user 31) first; the founder logs a real MFP day by screenshot; verify: 1 log_meal call,
`source="app"` rows, the block present on the next heartbeat tick, no morning nag.
Then all users. Watch `FOOD_LOGGER_GRADUATED` and `PARITY` lines for a week.
