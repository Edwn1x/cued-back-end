# CHANGESPEC — heartbeat speak-calibration (PR2)

Scope: two changes in `heartbeat.py` (one code, one prompt) + tests + docs.
`prompts/voice.md` and `build_loop_context` UNTOUCHED (see INVESTIGATION.md for the
separability proof). No new flags — edits are in place, reversible via `git revert`.

## 2a — code: invert the empty-history signal (`_proactive_context`)

**Before:** when a user had no outbound today AND no tick history, both the
`RECENT PROACTIVE MESSAGES` and `TICK HISTORY` blocks were simply omitted — the model
saw *nothing* and filled the void with "no proof this isn't a duplicate → stay
cautious" (bias #1: it wouldn't speak because it hadn't spoken).

**After:** the empty case renders an explicit `## PROACTIVE STATUS` block stating the
absence of history is PERMISSION to speak (there is nothing it could be repeating),
never a reason for caution. The non-empty branch is unchanged: the existing
anti-repetition blocks still render, and the permission line does NOT appear (a user
already nudged today is not a clean slate). Both branches are now mutually exclusive
and each is pinned by a tier-1 test.

## 2b — prompt: `HEARTBEAT_PROMPT` rewrite (kills biases #2 and #3)

Kept: the default-silent spine, the stay_silent tool contract, "One text. No preamble.",
"never open like your last few texts", "accountability is the job; fun is the delivery."

Added:
- **Standing conditions are valid triggers absent new input (bias #2).** Explicit:
  a heartbeat has no new message by design — that's what makes it proactive. A standing
  condition (days-long gap, a pattern the user asked to be held to, an open thread) is a
  valid reason on its own; the longer a warranted nudge goes unsent, the MORE worth
  sending. "Nothing new since the last tick" is called out as true on every tick and
  therefore NOT a silence reason.
- **Recalibrated threshold with concrete anchors (bias #3).** Explicit SPEAK list
  (multi-day skip / broken pattern for someone who asked to be called out — the "obvious
  yes"; timely follow-up on a real open thread; check-in tied to today's events) and
  STAY-SILENT list (quiet on-track day; mid-conversation = reactive territory; already
  sent this thought). Silence stays the default on quiet/on-track days.
- **Anti-nag via code, not self-suppression.** Tells the model the over-texting limits
  are enforced in CODE (one unanswered proactive nudge at a time via the anti-stack
  window; a hard daily cap) so it should NOT ration itself out of fear of nagging — its
  only job is the single judgment call "is THIS worth a text right now."

## 2e — code+prompt: both outcomes are explicit tools (added after the live run)

The live run proved 2a/2b necessary but not sufficient — the model decided to speak yet
called `stay_silent` anyway (reason: *"actually should speak — but tool forces silence"*),
because "speak = emit text, no tool" fought its tool-use prior when only `stay_silent`
was offered. Fix:

- **New `SEND_TEXT_TOOL`** (`{message}`) — speaking is now an explicit tool call.
- **`decide()`** offers `[SEND_TEXT_TOOL, STAY_SILENT_TOOL]` and terminates on either:
  `send_text` → `(True, message, search)`, `stay_silent` → `(False, reason, search)`.
  The bare-text `_join_text` path is retained as a robustness fallback (model ends with
  text and no tool → still speaks). web_search continuation logic unchanged; both
  decision tools are excluded from the tool-result feedback.
- **Prompt tool-contract** (`HEARTBEAT_PROMPT` tail): "call EXACTLY ONE tool — send_text
  to speak, stay_silent to stay quiet; if any part of your reasoning concludes a text is
  warranted, call send_text — never call stay_silent and protest in the reason."

Verified live: yes-anchor 6/6 `send_text`, quiet states still `stay_silent`. Isolated to
`heartbeat.py` (voice.md / build_loop_context untouched).

## 2c — tests

**Tier-1** (`tests/tier1/test_heartbeat.py`, deterministic, model stubbed):
- `test_proactive_context_empty_history_renders_permission` — fresh user gets the
  `PROACTIVE STATUS` / `PERMISSION` block, not a void. **Red before 2a (verified).**
- `test_proactive_context_with_history_suppresses_permission_block` — with a prior
  proactive message the anti-repetition block appears and the permission line does not.
  Pins the other branch.

**Tier-2** (`tests/tier2/test_heartbeat_proactive.py`, live, hard anchors):
- `test_heartbeat_speaks_on_accountability_gap` — THE yes-anchor, hard `spoke is True`.
  Fixture encodes BOTH halves (multi-day fall-off in the log AND explicit "call me out"
  request in the summary) and asserts both are present before the decision. Proven RED
  pre-calibration (0/4).
- `test_heartbeat_stays_silent_on_on_track_quiet_day` — NO-anchor, hard `spoke is False`
  (loosened threshold must not become a nag).
- `test_heartbeat_stays_silent_mid_conversation` — NO-anchor, hard `spoke is False` (a
  3-min-old inbound is reactive territory; the prompt itself must hold the line).
- Existing `test_heartbeat_default_silent_on_empty_state` — NO-anchor, retained.
- The ambiguous middle (open-thread, lookup-worthy, search-by-need) stays printed
  observation; the two-track speak-rate/cost summary is unchanged.

## Anti-nag floor (unchanged, from PR1)

`HEARTBEAT_STACK_WINDOW_MINUTES` (anti-stack: one unanswered proactive nudge at a time)
and `HEARTBEAT_MAX_PER_DAY` (hard daily cap), both in `guardrail_reason`. Existing
tier-1 guardrail tests stay green (verified: full tier-1 181 passed / 2 skipped).
