# Image-Fact Persistence — Change Spec

Narrow bug fix (two fixes), per the spec. No new tables, no new tools, no new flags.
Everything rides the existing gates: `READ_IMAGE_ENABLED`, `REMEMBER_TOOL_ENABLED`, and
friends; the legacy fallback path is untouched. Live during burn-in — smallest possible
surface.

---

## Change 1 — `prompts/voice.md` `## Images (MMS)`: loose facts and pre-consumption get a write path

- **What's there now:** four routing bullets (food → log_meal, calendar → log_event,
  whiteboard → log_workout, anything else → react conversationally, "Store nothing
  structured").
- **What it becomes:** the food bullet is split into eaten-now (log_meal, unchanged) vs
  **not-yet-eaten** (package / groceries / meal prep / nutrition label): do NOT log_meal
  (totals are for eaten food) — save the concrete details with `remember` so a later
  "ate the whole thing" resolves against the stored weight; at eating time log_meal from
  the stored numbers and update/invalidate the fact. The "anything else" bullet loses
  "store nothing structured" and gains: if the image showed a durable fact (sleep
  summary, weigh-in screen, a number you'd need later), save it with `remember` like any
  text-stated fact; only pure reaction content stores nothing. Plus the load-bearing
  principle, stated once: **a fact you only read into your reply is NOT saved — if
  you'll need it later, a tool call this turn is what saves it, and only a returned
  `ok` makes "got it saved" true.**
- **Why:** INVESTIGATION §1 — the drop is prompt-mandated; the write tools all exist.
- **Where:** `prompts/voice.md` (Images section only; the PROVISIONAL calendar note
  stays).

## Change 2 — `agent_tools.py` `REMEMBER_TOOL` description: image-borne facts are in-scope

- **What's there now:** "not for transient chatter … Do NOT log meals or workouts here"
  — steers the model away from remembering a package weight (reads as meal-adjacent
  chatter).
- **What it becomes:** adds one sentence: a durable detail read off an image (package
  weight, label macros, food on hand not yet eaten) counts and should be saved the same
  turn — the image is gone next turn. The meals/workouts exclusion is sharpened to
  "eaten meals / completed workouts (separate tools)".
- **Why:** INVESTIGATION §1/§2 — `remember` is the sanctioned home for the
  pre-consumption trace and other loose image facts.
- **Where:** `agent_tools.py` REMEMBER_TOOL description string only; schema unchanged.

## Change 3 — inbound Message rows record that an image was attached

- **What's there now:** `log_incoming(user.id, body)` stores caption text only; a
  captionless image logs an empty body. After the turn, nothing in the window shows an
  image ever arrived (INVESTIGATION §4).
- **What it becomes:** `sms.log_incoming` gains `has_image: bool = False`; when true the
  stored body is suffixed with the deterministic marker `[image attached]` (marker alone
  when the caption is empty). The webhook passes `has_image=image_url is not None` —
  same presence signal `classify_message` already uses. Code-owned, applied at write
  time; render is untouched.
- **Why:** makes "you sent a pic earlier but I didn't save the detail" *expressible*
  (fix 2's honest fallback needs a trace to point at), and makes the window honest for
  the summarizer/episodic digest too.
- **Where:** `sms.py` (`log_incoming`), `app.py` (the single `log_incoming` call site in
  the webhook).

## Change 4 — `prompts/voice.md`: retrieval-gap honesty rule (fix 2)

- **What's there now:** no rule against inventing technical causes (INVESTIGATION §3 —
  the "glitchy connection" rule the bug-fix spec references was never landed; spec drift,
  surfaced). The manage_log honesty block covers claimed actions only.
- **What it becomes:** a new short section, "Your own memory and gaps (honesty)":
  - Never invent a technical cause for your own behavior or gaps — no "glitchy
    connection", no "the image never came through", no "nothing came through on my
    end". You cannot see delivery; don't make claims about it.
  - Can't find something they say they told/showed you? Say you don't have it saved and
    ask for the detail — "I didn't save that" is honest; "it never arrived" is not.
  - If the recent conversation shows `[image attached]` but you don't have the detail:
    say exactly that ("you sent a pic earlier but i didn't save the weight — what did
    it say?").
  - Verify-before-conceding still applies: if they insist they sent it, check what you
    can actually see (window, logs in context) before agreeing something was lost.
- **Why:** the confabulation is the trust-destroying half of the incident; the rule must
  name the class, not just the instance.
- **Where:** `prompts/voice.md`, placed with the discipline/honesty material (after the
  manage_log correction section, before web search).

## Change 5 — tests

- **Tier-1** `tests/tier1/test_image_fact_persistence.py` (red-first where behavior
  changes; green regression guards where it must not):
  1. RED → `log_incoming(..., has_image=True)` stores the marker; captionless image
     stores marker-only body; `has_image=False` byte-identical to today.
  2. RED → webhook-level: an MMS post logs an inbound Message carrying the marker.
  3. Guard: image turn + scripted `remember` ToolUse → fact lands in
     `user_profile_memory` and renders in a **fresh** context with the Message window
     empty (survives window loss).
  4. Guard: pre-consumption — stored tenders fact is present in a later turn's context
     so the quantity is resolvable without re-asking.
  5. Guard: image → scripted log_meal ToolUse still creates the Meal (no regression).
  6. Guard: image → scripted log_event ToolUse still creates the Event (no regression).
  7. Guard: sleep-screenshot facts persist via remember as ordinary facts; assert no
     sleep table/subsystem exists to be invoked.
  8. Prompt-shape pins (voice.md/tool description contain the new load-bearing lines;
     "store nothing structured" gone) — cheap tripwires against a future prompt edit
     silently reverting the fix.
- **Tier-2** `tests/tier2/test_image_fact_persistence.py` (funded key, `--run-tier2`):
  1. Tenders replay: label PNG (fixture `tests/fixtures/tenders_label.png`, "NET WT
     1.5 LB (680 g)") → turn 1 must produce a durable write mentioning the weight; wipe
     the Message window (simulate roll-past); turn 2 "cooking the whole package now,
     eating all of it" → reply/log uses ~1.5 lb / 680 g and does NOT re-ask the weight.
  2. Retrieval honesty: user references a detail that was never saved → reply admits
     the gap and contains no delivery-failure claim ("came through", "never got",
     "didn't receive", "glitch", …) — claim-level assertion on the phrasing.

## Rollback

Every change is prompt text, a description string, or the additive `has_image` kwarg
(default `False` = today's behavior). Revert of the single commit restores the prior
state; no data migration, no flag flips.
