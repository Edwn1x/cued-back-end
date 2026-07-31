# CHANGESPEC — heartbeat warmth & presence (Part 2)

Scope: `heartbeat.py` (one code helper + one prompt block) + tests + docs. `voice.md`,
`build_loop_context`, and the reactive loop UNTOUCHED. No new flags. Reversible via
`git revert`. Stacked on Part 1 (PR #18).

## What's there now

Part 1 tuned the heartbeat to speak on **accountability** (a broken pattern) and stay
silent on quiet/on-track days. Its speak-reasons ask "is something wrong?" — so a good,
interesting, or friendly-check-in moment scores as silence. `_proactive_context` carries
today's events, recent workouts, meals/totals, coaching summary, and (flag-gated)
episodic life-context, but **no code-computed win/streak signal**, and the prompt has no
positive triggers. See INVESTIGATION.md.

## What it becomes

### A. `_recent_win_signal` + `## MOMENTUM` block (code — heartbeat-only)

New helper `_recent_win_signal(user, session)` counts **completed workouts in the last 7
days** (active/soft-delete-filtered, rolling window) and renders a neutral `## MOMENTUM`
block with the count + types. Surfaced into `_proactive_context` (NOT `build_loop_context`
— stays off the shared reactive surface). Rationale (playbook: *the model quotes, code
computes*): the model can't be trusted to do the date arithmetic, and `RECENT WORKOUTS`
lists rows without a windowed total. The block states facts only — no "great job", or
code would be doing the model's judging — and the header primes the bar ("a genuinely
strong run may be worth marking, a thin or falling-off one is NOT a win"). Returns None
(clean-absent, no placeholder) when there's no recent completed training.

Nutrition/PR streaks are deliberately NOT computed — `DailyLog` is not a reliable
daily-totals history and exercise PRs live in messy JSON; a fabricated nutrition streak
would violate the honesty invariant. Training consistency is the honest, available win.

### B. `HEARTBEAT_PROMPT` — positive/presence speak-reasons (prompt — heartbeat-only)

- Reframed the opening question from "is something wrong?" to "would a real friend reach
  out here?" — a presence you're glad to hear from, not a tracker that only pings on
  failures.
- Kept the **accountability** trigger as a first-class, non-negotiable wedge (unchanged).
- Added **warmth & presence** triggers: a genuine win worth marking (references MOMENTUM),
  a relevant bit of their world (grounded in memory), a timely grounded check-in on
  something THEY mentioned, levity/ease on a hard day.
- Added the **higher-bar-for-warmth** rule explicitly: reach for a warm text ONLY when
  there's specific, real material about this person; a generic pleasantry with nothing
  behind it stays silent ("how'd the summit go" speaks; "hope your day's going well" does
  not). Anti-nag stays code-enforced; warmth additionally must clear the "genuinely about
  them" test, not just the "under my cap" test.
- STAY-SILENT list updated: a modest, unremarkable few workouts is explicitly NOT a win
  to text about (guards the participation-trophy failure).

## Upstream dependency (flagged, out of scope)

Three of the four warm triggers (relevant-world bit, check-in, levity) draw on the
**episodic** life-context layer, which is gated behind `EPISODIC_ENABLED` (default off) —
so in the burn-in config only the win-streak trigger has material until episodic is
turned on and the digest job accumulates notes. Per §scope-guardrails that is a separate
upstream spec (it also changes the SHARED reactive surface); flagged, not folded in. The
tier-2 check-in anchor flips the flag on within the test to prove the heartbeat USES the
material correctly when present.

## Tests

**Tier-1** (deterministic): `test_proactive_context_surfaces_momentum_win` (5-in-7 →
MOMENTUM block, red-first verified), `test_proactive_context_no_momentum_when_no_recent_training`
(clean-absent; old + non-completed workouts don't count). Isolation + Part-1 context
regressions covered by existing tests.

**Tier-2** (live, hard where definitional): `test_heartbeat_speaks_on_genuine_win` (SPEAK),
`test_heartbeat_checks_in_on_mentioned_event` (SPEAK, episodic flag on),
`test_heartbeat_stays_silent_no_warm_material` (SILENT, the anti-bot guard). Part-1
accountability + no-anchors retained as regressions. All hard anchors re-run 3×.
