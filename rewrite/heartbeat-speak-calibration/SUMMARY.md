# SUMMARY — heartbeat speak-calibration (PR2)

## What this fixes

PR1 (#17) opened the code gate; this PR fixes what the open gate exposed: **given the
chance to speak, the pre-calibration model chose silence on the clearest possible
accountability moment** (0/4 in the tier-2 burn-in, including a ~10-day fall-off for a
user who asked to be called out). Three prompt/context biases, PLUS a decision-mechanics
bug the live run surfaced:

| Bias | Locus | Fix |
|------|-------|-----|
| #1 empty history → caution | code (`_proactive_context`) | 2a: render empty history as explicit PERMISSION, not a void |
| #2 "no new info since last tick" | prompt (`HEARTBEAT_PROMPT`) | 2b: standing conditions are valid triggers absent new input |
| #3 threshold too high | prompt (`HEARTBEAT_PROMPT`) | 2b: concrete SPEAK/SILENT anchors; anti-nag is code, not self-suppression |
| #4 speak decision routed through the silence tool | code+prompt (`decide` tools) | 2e: make BOTH outcomes explicit tools (send_text + stay_silent) |

Judgment in the prompt, limits in code. The anti-stack window + daily cap (PR1) are the
floor that keeps a loosened prompt from becoming a nag.

## The mechanics finding (2e — surfaced by the live run, the real unlock)

2a/2b were necessary but NOT sufficient. The first live run still failed the yes-anchor —
but not because the model chose silence. The raw response showed the model had DECIDED
to speak and routed that decision through the wrong channel: it called `stay_silent` with
reasons like *"actually should speak — but tool forces silence"* and *"placeholder -
actually need to speak, not stay silent"*. An effort sweep (low/medium/high, empty
thinking blocks at every level) ruled out under-thinking.

**Root cause (response-shape seam):** speaking was defined as the *absence* of a tool
call (just emit text), while only a `stay_silent` tool was offered. That fights the
model's strong prior to use an available tool — so a model that wanted to speak still
called the one tool it had, protesting in the reason field.

**Fix (2e):** make BOTH outcomes explicit tools. Added `SEND_TEXT_TOOL`; `decide()` now
offers `[send_text, stay_silent]` and terminates on either (`send_text` → speak,
`stay_silent` → silent). The bare-text path is kept as a robustness fallback. Prompt
tool-contract updated: "call EXACTLY ONE tool; if you conclude a text is warranted, call
send_text — never call stay_silent and protest." Still isolated to `heartbeat.py`;
`prompts/voice.md` and `build_loop_context` untouched.

## Separability

Confirmed the heartbeat decision surface is cleanly separable from the reactive loop:
all edits touch only the SECOND system block (`HEARTBEAT_PROMPT` + `_proactive_context`)
and `decide()`'s own tool set — nothing reactive reads any of them. Full trace + grep
evidence in INVESTIGATION.md.

## Verification status

### Tier-1 (deterministic) — RUN, GREEN ✅
- `pytest tests/tier1/ -q` → **182 passed, 2 skipped** (anti-nag guardrail tests still
  green; added send_text primary-path + bare-text fallback tests + the two context tests).
- Red-first confirmed: `test_proactive_context_empty_history_renders_permission` FAILS
  on the pre-2a code (verified by stashing `heartbeat.py` and re-running).

### Tier-2 (live anchors) — RUN, GREEN ✅ (funded key, anthropic==0.116.0)
- `pytest tests/tier2/test_heartbeat_proactive.py --run-tier2 -s` → **7 passed**.
- **Yes-anchor re-run 3× (binary): 3/3 speak** — plus 3/3 in the debug two-tool sweep =
  **6/6** on the clearest accountability moment. Sample nudge: *"it's been 12 days since
  your last logged workout (pull, the 18th). you asked me to call this out instead of
  letting it slide — so: what's going on, and what's it gonna take to get back in there
  this week?"*
- All NO-anchors stayed silent with honest reasoning (empty-state, on-track-quiet,
  mid-conversation, repeat-guard).

### Founder-phone (the real gate) — pending
With `LEGACY_SCHEDULER_ENABLED=false` and the allowlist on the founder's number, watch
for the first proactive message that reads like a coach who noticed a 10-day skip — not
a bot on a timer — while quiet days stay quiet. Only after that lands do
allowlist-widening / Phase-6 deletions become eligible.

## Before/after speak rate

| Anchor | Before (pre-calibration) | After (this PR) |
|--------|--------------------------|-----------------|
| accountability-gap (YES) | 0/4 silent | **3/3 speak** (6/6 incl. debug sweep) ✅ |
| empty-state (NO) | silent ✓ | silent ✅ |
| on-track-quiet (NO) | n/a (new) | silent ✅ |
| mid-conversation (NO) | n/a (new) | silent ✅ |
| blended spread (quiet / open-thread / already-spoke / lookup) | — | 2/4 speak (open-thread + lookup spoke; quiet + already-spoke silent) |

Per-track cost from the summary run: searched ticks ~$0.031/tick (1/1 spoke), unsearched
~$0.009/tick (1/3 spoke); ~1/4 ticks invoked search. Extrapolate to N users from the
SHARE + per-track costs, never the blended average.
