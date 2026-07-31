# INVESTIGATION — heartbeat warmth & presence (Part 2)

Part 1 (PR #18) fixed the *structural* silence (deadlock, `stay_silent`-only tool
asymmetry, empty-history inversion) and tuned **accountability** triggers. This pass
adds the other half of the founding vision: a presence you're glad to hear from — a
friend who marks a win, passes along something relevant, checks in on your week, or
lightens a hard day. Warmth is the retention mechanism; accountability is one part of it.

## 1. What warm material actually reaches the proactive decision today? (load-bearing)

`decide()` → `_proactive_context(user, session)` → `build_loop_context(user, session)`
(agent_loop.py:55, SHARED with the reactive loop) + heartbeat-only additions
(TIME SINCE LAST MESSAGE, RECENT PROACTIVE MESSAGES / TICK HISTORY / PROACTIVE STATUS).

`build_loop_context` already assembles a lot of the raw material a warm text needs:

| Block | Warm use | Reaches decision? |
|-------|----------|-------------------|
| `## TODAY'S EVENTS` (regex + model-logged, with descriptions + time spans) | grounded check-in ("good luck at the summit") | **Yes** (not flag-gated) |
| `## RECENT WORKOUTS` (last 5, active) | raw training signal | **Yes**, but NOT summarized into a win/streak |
| `## TODAY'S LOGGED MEALS` + `## TODAY'S TOTALS` (code-computed) | nutrition signal | Yes (today only) |
| `## COACHING SUMMARY` | stated goals/cadence | Yes |
| `## RECENT CONVERSATION` | open threads the user mentioned | Yes (windowed) |
| `## RECENT LIFE CONTEXT` (episodic digests: "orgo midterm tomorrow", "founders summit Friday", "rough week") | check-in / levity / relevant-world — the friend material | **Flag-gated: only if `EPISODIC_ENABLED`** |

### The two real gaps

1. **Episodic life-context is gated OFF.** `build_loop_context` renders
   `## RECENT LIFE CONTEXT` only `if config.EPISODIC_ENABLED` (agent_loop.py:134);
   `EPISODIC_ENABLED` defaults **false** (config.py:147), and the digest sweep
   (`episodic.digest_all`) is the same-flag no-op (episodic.py:121). So in the burn-in
   config the model has NO episodic material. This is load-bearing: **without episodic
   on, 3 of the 4 warm triggers (relevant-world bit, check-in, levity-on-a-hard-day)
   have essentially no material to fire on.** Per §scope-guardrails, episodic
   *generation* (turning the flag on + running the digest job + the resulting change to
   the SHARED reactive surface) is a separate upstream spec — **flagged here, not folded
   in.** This pass makes the heartbeat *use* warm material; it does not generate it.

2. **No "notable win / streak" signal is computed anywhere.** grep for
   `streak|PR|milestone|consecutive` finds nothing in product code. The model sees the
   last 5 raw workouts but not a code-computed "N completed sessions in the last 7 days"
   — which, per the playbook's *precompute what the model is asked for / the model
   quotes, code computes*, is arithmetic that must not be the model's job. This is the
   one warm trigger whose material is missing but cheaply, honestly computable from the
   Workout table — and it works immediately, independent of the episodic flag.

Nutrition/PR streaks are NOT cheaply available: `DailyLog` (models.py:240) is a legacy
structure written in one place and not a reliable daily-totals history; exercise-weight
PRs live in messy `Workout.exercises` JSON. So the honest win signal this pass computes
is **training consistency** (completed workouts in the last 7 local days), not a
fabricated nutrition streak.

## 2. What does the episodic layer contain right now?

For the founder's real profile it is **empty in the burn-in config** — `EPISODIC_ENABLED`
is off, so the digest job has never run and `EpisodicDigest` has no rows. The layer is
built and correct (idempotent watermark, non-fitness-scoped digest prompt), it's simply
not turned on. Flagged as the upstream dependency the warm check-in / levity triggers
wait on. The win-streak trigger does not depend on it.

## 3. Isolation from `voice.md` still holds

Confirmed (same boundary as Part 1). This pass edits only: `HEARTBEAT_PROMPT` and the
new win-signal block inside `_proactive_context` — both heartbeat-only. `build_loop_context`
and `prompts/voice.md` (block 1, shared with the reactive loop) are UNTOUCHED. grep
confirms `HEARTBEAT_PROMPT` / `_proactive_context` are read by nothing reactive. The win
signal is computed in `_proactive_context`, NOT in `build_loop_context`, so it cannot
leak into the reactive loop.

## 4. Anchors that exist today (Part 1) — which are accountability-only

Tier-2 (`tests/tier2/test_heartbeat_proactive.py`):
- YES: `speaks_on_accountability_gap` (12-day skip + call-me-out) — **accountability**.
- NO: empty-state, on-track-quiet, mid-conversation, repeat-guard.
- Observed: open-thread, lookup-worthy, search-by-need.

All SPEAK anchors are accountability. **The new positive anchors (win, grounded check-in)
are ADDITIONS; the 12-day accountability anchor stays intact and must still speak** — the
wedge is preserved, warmth is added around it.

## Plan (in-scope, heartbeat surface only)

- **A (code, `_proactive_context`):** a `## MOMENTUM` block — code-computed completed
  workouts in the last 7 local days (count + types). Honest facts only, no editorializing
  ("great job!"); the model judges whether it's a genuinely notable win worth marking vs
  participation-trophy. Renders cleanly absent when there's nothing (no crash/placeholder).
- **B (prompt, `HEARTBEAT_PROMPT`):** add positive/presence speak-reasons (a genuine win,
  a relevant bit of their world, a timely grounded check-in, levity on a hard day) as
  first-class alongside the Part-1 accountability triggers, WITH the higher warmth bar:
  reach for a warm text only when there's specific, real material about the user;
  generic engagement bait stays silent. Accountability triggers unchanged.
- **Upstream flag (out of scope, recorded):** `EPISODIC_ENABLED` must be turned on (and
  the digest job left to accumulate life-context) before the check-in / levity triggers
  have material in prod. Separate spec.
