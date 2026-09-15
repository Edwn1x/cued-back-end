# Workout logging fidelity — burn-in finding (2026-09-15, founder's live legs session #7)

The founder ran a real legs session over text mid-workout. Five distinct failures, one
root cause: **the terse set-text parser is too eager and the coach doesn't log a working
set it clearly knows.** State that must be exactly right (what was lifted) was left to the
model's phrasing and to a greedy regex.

## What happened, in order
1. `Doing laying leg press` / `1 plate each side` / `3 plates each side for my first working
   set` — **plate-talk never parses.** The weight (315) existed only in the model's reply,
   never in code. Coach asked for reps; nothing logged. (Correct not to guess — but see #2.)
2. `I got 5`, `Got 7 for my second set`, `Got 7 3rd set` — **rep-only reports fell through**
   ("I "/"for my second set" defeat the rep-only regex) → the model coached (`315x5, clean`)
   but **did not call log_workout**, so three working sets went unrecorded. Honesty invariant
   held (it never claimed to log), but the data was lost.
3. `bank that number, next legs we start at 325` — a **forward promise the system couldn't
   keep**: nothing was logged, so the next plan would baseline off template defaults, not 325.
4. `Did squats with 135 for 3 sets 7 reps each` → **mis-parsed**: `W_X_R` grabbed the first
   `135 for 3` as weight×reps and logged one squat set at **135×3** (wrong reps, missing two
   sets). `swapped it in.` gave no sign of the misread.
5. On `The numbers aren't correct?` the coach **re-logged from conversational context** (good
   recovery — `log_workout` routed into the open session), but `_log_into_open_session`
   **appended instead of reconciling**, leaving the bad `135×3` next to the three real `135×7`
   and a stray card tap. The closed summary + legacy mirror kept the wrong `135×3` row.

## The fixes (this PR)
- **Parser (`workouts/parse.py`)**: structured multi-set forms parse correctly
  (`135 for 3 sets 7 reps`, `3 sets of 7 at 135`, `135x3x8`) → `(weight, sets, reps)`. Any
  sentence with a bare `set(s)`/`rep(s)` word that DOESN'T match a structured form returns
  `None` (the model logs it with full context) — never the greedy `135 for 3` misread.
  Rep-only auto-log at the planned weight is **narrowed to miss-framing** (`only`/`just`/
  `barely got 3`); plain `got 5` → `None` (the model knows the real weight; code doesn't).
- **`apply_text_update`**: applies a multi-set update (`135 for 3 sets 7 reps` → three sets),
  replies `logged 3 sets.`
- **Reconcile (`_log_into_open_session`)**: when the coach re-logs an exercise, this session's
  prior **text-sourced** sets for it are superseded (soft-deleted) before the new ones land —
  card/tapback/coach sets (real user actions) are preserved. No more duplicate/over-count.
- **voice.md**: with a session open and a working set's weight + reps known (even across
  messages), **log_workout it** — don't just praise it. Never promise "next time we start at X"
  off a set you didn't log.
- **Prod repair**: session #7 squat `135×3` → `135×7`; session volume + legacy mirror recomputed.
