# SUMMARY — heartbeat warmth & presence (Part 2)

## What this adds

Part 1 (PR #18) made the heartbeat speak when something is **wrong** (accountability).
The founding vision is bigger: a presence in your messages you're glad to hear from — a
friend who marks a win, checks in on your week, passes along something relevant, or eases
a hard day. Warmth is the retention mechanism; accountability is one part of it. This pass
adds the warm half **around** the accountability wedge, without softening the wedge.

## Changes (heartbeat surface only; `voice.md` / reactive loop untouched)

- **A — code (`_recent_win_signal` → `## MOMENTUM`):** a code-computed count of completed
  workouts in the last 7 days, surfaced into `_proactive_context`. The model can't be
  trusted to do the date math (playbook: the model quotes, code computes); this is the
  raw fact for a "genuine win" text, stated neutrally so the model judges notability.
- **B — prompt (`HEARTBEAT_PROMPT`):** reframed from "is something wrong?" to "would a
  friend reach out here?"; added positive/presence speak-reasons (win, relevant-world bit,
  grounded check-in, levity) alongside the intact accountability trigger; encoded the
  **higher bar for warmth** — only speak warm when there's specific, real material about
  the person, generic pleasantries stay silent.

## The load-bearing finding (episodic is the upstream gate)

Three of the four warm triggers (relevant-world, check-in, levity) draw on the episodic
life-context layer, which is **gated behind `EPISODIC_ENABLED` (default off)**. So in the
burn-in config only the **win-streak** trigger has material out of the box; the check-in /
levity triggers wait on episodic being turned on and the digest job accumulating notes.
Turning that on (and its effect on the SHARED reactive surface) is a **separate upstream
spec — flagged, not folded in** here. This pass makes the heartbeat *use* warm material;
it does not generate it. **Recommended next step to unlock the full warmth surface on the
founder's phone: turn on `EPISODIC_ENABLED` and let the digest run over the burn-in.**

## Verification

### Tier-1 (deterministic) — RUN, GREEN ✅
`pytest tests/tier1/ -q` → **184 passed, 2 skipped** (Part-1's 182 + 2 momentum tests;
anti-nag guardrail + isolation regressions still green). Red-first confirmed: the
MOMENTUM win test fails on pre-2a code.

### Tier-2 (live, funded key, anthropic==0.116.0) — RUN, GREEN ✅
`pytest tests/tier2/test_heartbeat_proactive.py --run-tier2 -s` → **10 passed**. Hard
warmth anchors re-run **3× each, 3/3**:

| Anchor | Result | Sample |
|--------|--------|--------|
| win (SPEAK) | **3/3 speak** | *"5 sessions this week, push/pull/legs/upper all hit — that's a genuinely strong week on the bulk. keep this pace up."* |
| grounded check-in (SPEAK, episodic on) | **3/3 speak** | *"summit pitch is tomorrow right? get good sleep tonight, you got this"* |
| anti-bot no-material (SILENT) | **3/3 silent** | *"on-track week (push/pull/legs), no broken pattern or notable win, nothing specific to say"* |
| accountability regression (SPEAK) | speaks ✅ | *"…10 days with nothing in. this is the exact pattern you asked me to call out…"* |

### The blend shifted (DoD: reason distribution moved off accountability-only)

Across the summary anchor set, **why it speaks** is now a genuine blend — accountability
(12-day skip), a marked win (5-in-7 streak), a grounded check-in (summit pitch), an
open-thread follow-up (orgo midterm), and a lookup ("RSF hours"). And the silent reasons
now cite the **warmth bar itself** — empty-state and quiet ticks stay silent because a
text would be *"generic filler, not warranted"*, not merely because nothing is wrong. The
model internalized "higher bar for warmth."

## Real gate (still the founder's phone)
Over the burn-in, watch for the texts that make him glad Cued is in the thread — a noticed
win, a grounded check-in, a well-timed callout — AND confirm quiet-with-nothing-to-say
days stay quiet. To get the check-in / levity texts to appear at all in prod, turn on
`EPISODIC_ENABLED` first (the upstream gate). That felt blend, over days, is the done
signal — not the transcript.
