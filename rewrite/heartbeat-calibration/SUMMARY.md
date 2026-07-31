# Heartbeat Burn-In Calibration — Summary

Two fixes so the burn-in can finally measure the heartbeat: a deadlock that kept it
from ever speaking, and a legacy proactive scheduler that was confounding the data.
Both reversible, flag-gated; founder stays the only allowlisted user.

## What shipped

**Item 2 — legacy proactive scheduler disabled** (`LEGACY_SCHEDULER_ENABLED`,
default off). Defense-in-depth gates on `schedule_user`, `send_scheduled_message`,
`check_meal_adherence`, and the global-adherence job registration in
`start_scheduler`. The heartbeat is now the only proactive system. Dining scrape,
heartbeat, consolidation, and episodic keep running. Not deleted — reversible.

**Item 1 — `unanswered_gap` deadlock fixed.** Root cause: `has_unanswered_outbound`
is type-blind and clears only on a user inbound, so the coach's own reactive reply
(and every morning's legacy briefing) muted all proactive initiation until the user
spoke — the heartbeat could never send the message the user would reply to. Replaced
the heartbeat's use of it with `has_unanswered_proactive(user_id, window)`, an
anti-**stack** gate that blocks only a within-window unanswered *proactive* nudge and
**clears on time-elapse without user input**. Reactive replies and legacy briefings
no longer wedge it. New knob `HEARTBEAT_STACK_WINDOW_MINUTES` (default 180).
Guardrail reason renamed `unanswered_gap` → `proactive_stack`.
`has_unanswered_outbound` kept unchanged for the (gated-off) legacy scheduler.

Files: `config.py`, `engagement_tracker.py`, `heartbeat.py`, `scheduler.py`,
`tests/tier1/test_heartbeat.py`, `tests/tier1/test_legacy_scheduler.py`,
`tests/tier2/test_heartbeat_proactive.py`. Docs under
`rewrite/heartbeat-calibration/` (INVESTIGATION, CHANGESPEC, this summary).

## Definition of done — status

- [x] INVESTIGATION.md, CHANGESPEC.md (with the legacy-vs-heartbeat decision
      surfaced), SUMMARY.md under `rewrite/heartbeat-calibration/`.
- [x] Tier-1 green, no new xfails: **179 passed, 2 pre-existing skips**. Deadlock
      regression and anti-stack cases both present and passing (red-first verified —
      the new suite failed on the old code before the fix).
- [x] Legacy proactive scheduler disabled behind a reversible flag (not deleted).
- [x] Scope held: no deletion, no allowlist widening.
- [x] **Tier-2 RAN against the funded key — 5 passed** (`--run-tier2 -s`,
      `anthropic==0.116.0`; the worktree had a stale 0.42.0 that predates the
      `thinking=` param — installed the pinned version to run). The deadlock fix is
      proven live: on a quiet tick with a real standing signal the code gate does NOT
      suppress and the decision call **reaches the model** (metered) — structurally
      impossible under the old deadlock, where `decide()` was never called once an
      outbound sat unanswered.

## FINDING (headline — hand to founder): the coach is still default-silent

The gate is fixed, but the live model **spoke 0/4 times** across every scripted
opening (empty state, ~10-day training fall-off for a user who asked to be called
out, a stale exam follow-up, an open thread, a lookup-worthy evening plan). It
consistently reached the decision and then chose silence, citing:
- **empty anti-repetition context** — "no tick history/messages provided to confirm
  not a duplicate, so I'll stay cautious" (a *fresh* user makes it MORE hesitant);
- **"no new info since last tick"** — a structural objection: a heartbeat tick has no
  new user input by definition, so a *standing* condition (a multi-day skip) never
  reads as a new trigger;
- **"not yet a pattern" / "reply in-thread, not a proactive text"**.

So the burn-in's original worry ("never demonstrated the core behavior") is only
half-resolved: the heartbeat *can* now initiate (the gate opens), but the speak
**threshold is miscalibrated toward silence**. This is a prompt-calibration issue,
**out of scope for this deadlock/cleanup PR** (surfaced as a candidate, not built):
consider telling the model explicitly that an empty tick history means it has *not*
yet nudged, and that a standing accountability gap is itself a valid trigger absent
new input — then re-run this harness and read the speak rate.

## Deploy note (for the founder)

Ship with `LEGACY_SCHEDULER_ENABLED=false`. Then watch the founder's number for
(a) whether a real quiet-tick nudge ever fires, and (b) zero legacy briefings.
Speak-rate/cost numbers only count from here — prior data was confounded by the
legacy scheduler. Given the 0/4 live speak rate above, expect **near-silence** until
the speak threshold is recalibrated; that calibration (and only then allowlist
widening / deletion) is the next step, not part of this PR.
