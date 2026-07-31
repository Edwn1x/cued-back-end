# INVESTIGATION — heartbeat speak-calibration (PR2)

## Why this PR exists

PR1 (#17) fixed the `unanswered_gap` deadlock: the code gate now opens and
`decide()` reaches the model. That exposed the real failure one layer deeper —
**given the chance to speak, the live model almost always chooses silence**
(0/4 across every scripted opening in the tier-2 run, including a ~10-day
training fall-off for a user who explicitly asked to be called out). A
proactive-accountability product whose heartbeat *can* initiate but *won't* is
the same broken outcome as one that *couldn't*.

The model's stated reasons are three **structural** biases, not random hesitance:

1. **Empty history → more cautious.** "No tick history to confirm this isn't a
   duplicate, so I'll stay cautious." Self-perpetuating: it won't speak because
   it hasn't spoken. Root cause is in code — `_proactive_context`
   (heartbeat.py:159/167) only appends the `RECENT PROACTIVE MESSAGES` /
   `TICK HISTORY` blocks when non-empty, so a quiet day shows the model *nothing*
   and it fills the void with fear of repeating. **Locus: code (`_proactive_context`).**
2. **"No new info since last tick."** A heartbeat tick has no new user input *by
   definition* — that's what makes it proactive. If the model treats new input as
   the trigger, it can never fire on a **standing condition** (a 10-day skip),
   which is exactly what proactive accountability exists to catch.
   **Locus: prompt (`HEARTBEAT_PROMPT`).**
3. **Threshold set too high.** "Not yet a pattern" / "reply in-thread, not
   proactively." The bar is where a cautious assistant sets it, not where a coach
   the user asked to be held accountable would. **Locus: prompt (`HEARTBEAT_PROMPT`).**

## Separability trace (the seam that has bitten before — MUST confirm)

`decide()` (heartbeat.py:190-193) composes the decision-call system prompt as:

```python
system = [
    {"type": "text", "text": _voice_prompt(), "cache_control": {"type": "ephemeral"}},  # BLOCK 1
    {"type": "text", "text": HEARTBEAT_PROMPT + "\n\n" + context},                       # BLOCK 2
]
```

- **Block 1 = `_voice_prompt()`** — reads `prompts/voice.md` (cached, agent_loop.py:37-42).
  This is the SAME first system block the reactive loop sends. It IS present on every
  tick. The isolation guarantee is therefore NOT "voice.md isn't involved."
- **Block 2 = `HEARTBEAT_PROMPT` + `context`**, where `context = _proactive_context(...)`
  (heartbeat.py:185). This block is heartbeat-only.

The isolation guarantee is precise: **this PR edits only Block 2's two inputs
(`HEARTBEAT_PROMPT` text and the empty-history branch of `_proactive_context`), so
it cannot leak into reactive behavior.** Confirmed by grep:

- `HEARTBEAT_PROMPT` — referenced ONLY at heartbeat.py:40 (def) and heartbeat.py:192
  (the decide() composition). **No reactive path reads it.**
- `_proactive_context` — referenced ONLY in heartbeat.py:144/185 and tests. **No
  reactive path calls it.**
- `_proactive_context` DOES call `build_loop_context(user, session)` (heartbeat.py:145),
  which IS shared with the reactive loop (agent_loop.py:55). **This PR does NOT touch
  `build_loop_context`** — it only adds a heartbeat-only `PROACTIVE STATUS` block in
  `_proactive_context` around it. voice.md is untouched.

**Verdict: surfaces are cleanly separable. Two files change: `heartbeat.py`
(prompt + context) plus tests/docs. `prompts/voice.md` and `build_loop_context`
stay untouched.** Proceeding with the two-file scope.

## Guardrails unchanged (the anti-nag floor from PR1)

Calibration loosens *when* the coach speaks. The floor that stops a loosened prompt
from becoming a nag is code, from PR1: the anti-stack window
(`HEARTBEAT_STACK_WINDOW_MINUTES`, one unanswered proactive nudge at a time) and the
hard daily cap (`HEARTBEAT_MAX_PER_DAY`), both in `guardrail_reason`. Judgment in the
prompt, limits in code. Existing tier-1 guardrail tests must stay green after the
prompt loosens.
