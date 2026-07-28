# Image-Fact Persistence — Summary

Fixes the tenders bug (an image-only fact read and spoken but never persisted) and the
"nothing came through on my end" confabulation that explained the gap. Two prompt-level
fixes plus one small code change; no new tables, tools, or flags.

## What changed

1. **`prompts/voice.md` — Images section** (fix 1): the "anything else → store nothing
   structured" bucket — the literal instruction the tenders weight fell into — is gone.
   New branches: food *not yet eaten* (package/label/groceries/meal prep) → save the
   details with `remember`, do NOT `log_meal` (totals are for eaten food), log the meal
   from the stored weight at eating time; any other image with a durable fact (sleep
   summary, weigh-in screen) → `remember` it as an ordinary fact. The load-bearing
   principle is stated once, up front: a fact only read into the reply is NOT saved —
   the tool call is the remembering.
2. **`agent_tools.py` — `REMEMBER_TOOL` description**: image-extracted facts are
   explicitly in-scope ("the image is gone next turn"); the exclusion is sharpened to
   *eaten* meals / *completed* workouts.
3. **`sms.py` / `app.py` — `[image attached]` marker** (code-owned): `log_incoming`
   gains `has_image`; the webhook passes it. The stored inbound row — the only thing
   the conversation window ever sees — now records that media arrived (marker alone for
   a captionless MMS). This is what makes "you sent a pic earlier but i didn't save the
   weight" *expressible*: before, an image with no caption logged an empty body and the
   model genuinely could not distinguish "never arrived" from "arrived, not saved".
4. **`prompts/voice.md` — new "Your own memory and gaps (honesty)" section** (fix 2):
   never invent a technical cause for own behavior ("glitchy connection", "never came
   through" banned by name); a retrieval gap is "i don't have it saved", never a
   delivery claim; the `[image attached]` trace gets the honest middle line
   ("you sent a pic earlier but i didn't save the weight").

## Judgment calls (findable + reversible)

- **`remember`, not `log_event`, for pre-consumption food.** log_event expires with its
  day; tenders photographed Monday and eaten Tuesday would vanish overnight and
  reproduce the bug. The fact's lifecycle ends when the user *eats it* (update/
  invalidate), not when the clock rolls. INVESTIGATION §2 records the dead hypothesis.
- **`User.pending_photo_meal` not reused**: legacy-pipeline-only, single-purpose,
  headed for retirement. Wiring the new path into it would couple the fix to a
  scheduled deletion.
- **Fix 2 is a NEW rule, not an extension** — spec drift, surfaced per the playbook:
  the "glitchy connection" rule the spec says was added post-incident does not exist in
  `voice.md` or anywhere in its git history (INVESTIGATION §3). The nearest rules cover
  claimed *actions* (manage_log) and coaching causes (legacy prompt), not the model
  explaining its own gaps.
- **No code guard for the confabulation**: the §V honesty guard pattern (state-change
  language ⇒ tool success) targets claimed actions; a claimed *cause* has no cheap
  detectable signature. It stays a prompt rule, gated by the tier-2 honesty case.
- **Marker at `log_incoming`, not in the render**: write-time, deterministic, one call
  site; the summarizer and episodic digest see the honest body too.

## Tests

- **Tier-1** (`tests/tier1/test_image_fact_persistence.py`, 9 cases): red-first for the
  behavior changes (marker unit + webhook-level, prompt-shape pins — 4 red pre-fix,
  green post); regression guards for what already worked (remember/log_meal/log_event
  from an image turn, window-loss survival, pre-consumption resolution, sleep-as-facts
  with an assert that no sleep table exists). Full tier-1: **164 passed, 2 pre-existing
  skips, no new xfails.**
- **Tier-2** (`tests/tier2/test_image_fact_persistence.py`, 2 cases): the tenders
  replay against a real label fixture (`tests/fixtures/tenders_label.png`, "NET WT
  1.5 LB (680 g)") asserting the weight reaches durable state in turn 1 and is used —
  not re-asked — after the window is wiped; and the retrieval-honesty case with
  claim-level assertions that no delivery-failure phrasing appears.

  **RUN 2026-07-28 against the funded key: 2/2 passed.** Recorded transcripts:
  - Tenders turn 1 (package photo): the model called `remember` unprompted; durable
    state after the turn: `"has a 1.5 lb (680g) package of raw chicken tenders on
    hand, uncooked — not eaten yet"`. Reply: "solid haul — that's like 4-5 servings
    of protein sitting there. when you cook em up just tell me how much you eat and
    i'll log it."
  - Tenders turn 2 (window wiped, "cooking the whole package now, eating all of
    it"): "logged the whole tray — 750 cal, 156g protein, basically zero carbs/fat.
    …" — meal logged from the stored weight, no re-ask.
  - Honesty case (marker present, nothing saved): "nah, i don't have a weight saved
    from that pic — what'd the package say?" — gap admitted, zero delivery claims.

  First run surfaced two bugs in the TESTS, not the product (fixed): the turn-2 user
  object was stale (test session held the pre-write profile; prod refetches per
  webhook — `db.expire_all()` added), and the honesty positive-phrase list missed the
  legitimate "not seeing a weight saved" phrasing. The honesty run-1 reply was already
  correct behavior.

## Scope held

No sleep subsystem (a sleep screenshot is just another loose-fact image; tier-1 asserts
no sleep table exists). No accuracy/confidence work. Everything rides existing flags
(`READ_IMAGE_ENABLED`, `REMEMBER_TOOL_ENABLED`, …); rollback is a revert of one commit.
