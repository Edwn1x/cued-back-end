# Heartbeat Burn-In Calibration — Investigation

Ground truth read end-to-end before any change. Two findings: a gap-guardrail
deadlock (Item 1) and a still-live legacy proactive scheduler confounding the
burn-in (Item 2). Sequenced Item 2 → Item 1 per the spec: the legacy scheduler is
a prime suspect for wedging the gap, so the confound is cleared first.

---

## Item 1 — the `unanswered_gap` deadlock

### Where it lives
`heartbeat.guardrail_reason()` (heartbeat.py:108-111) calls
`engagement_tracker.has_unanswered_outbound(user.id)` and returns
`"unanswered_gap"` when it is True — a code guardrail that suppresses the tick
*before* any model call.

### What `has_unanswered_outbound` actually does (engagement_tracker.py:21-65)
```
last_out_today = most recent OUTBOUND with created_at >= today_start (UTC midnight)
if no outbound today            -> False   (no block)
last_in_today  = most recent INBOUND  with created_at >= today_start
if no inbound today             -> True    (block)
else                            -> (last_out_today.created_at > last_in_today.created_at)
```

So it blocks whenever **the most recent outbound today is newer than the most
recent inbound today** (or there is no inbound today at all).

### The exact clear condition (investigation point 1)
Reading the code, once any outbound goes out and sits newer than the last
inbound, the guardrail keeps returning True until **one of two things happens**:

1. **The user sends an inbound** newer than that outbound (requires the user to
   speak first), or
2. **UTC midnight rolls over** — `today_start` advances, the prior outbound falls
   out of "today," and if nothing has been sent yet in the new day it returns
   False.

There is **no time-elapse clear within a day** and **no notion of a window**. A
proactive coach that has spoken today cannot speak again on its own until the
user replies or the calendar date changes. This is a structural deadlock: it
stays silent because the user hasn't replied, but it is never allowed to send the
fresh message the user would reply to.

### Legitimate vs broken (investigation point 2)
- **Legitimate goal:** *don't stack a second unanswered proactive nudge on top of
  a first* (anti-spam), with a clear condition that doesn't require the user to
  speak.
- **What's implemented:** *don't send anything while the most recent outbound of
  ANY type is unanswered* — i.e. "stay mute until spoken to." Two things are
  conflated and both are wrong for the heartbeat:
  1. **Type-blind.** It counts *reactive replies* as "unanswered outbounds." The
     coach answering the user (app.py:505, `message_type=<classified inbound
     type>`) is an outbound; the user has no reason to reply to a closing answer,
     so that reply alone latches the gap True and mutes proactive initiation for
     the rest of the day. This is the core of the deadlock — it needs no legacy
     scheduler at all.
  2. **Legacy briefings count too** (see Item 2 / point 4).

### Log correlation (investigation point 3)
Consistent with the spec's July-29 excerpt: nearly every tick `spoke=False
reason=guardrail:unanswered_gap`, with one `active_conversation` mid-exchange
(correct). The founder *did* send inbounds during the window (food photo, "you
math is wrong"). Each inbound would momentarily clear the gap (inbound newer than
last outbound) — but the very next outbound (the coach's reactive reply to that
inbound, or the next morning's legacy briefing) re-latches it. So the gap
oscillates closed→open→closed and the heartbeat never finds an open tick on a
genuinely quiet stretch. Net: zero proactive messages, exactly as observed. The
code path fully explains the log without needing any other cause.

### Interaction with the legacy scheduler (investigation point 4 — confirmed)
`has_unanswered_outbound` is **type-blind**, so a legacy `morning_briefing`
(sent 06:00-ish local via `send_scheduled_message`) is a fresh outbound the
founder rarely "answers." From that send until the next inbound, the gap is True
and the heartbeat is muted. Because the briefing fires **every morning**, it
re-latches the deadlock at the start of each new UTC day — closing even the
midnight-rollover escape hatch. The legacy scheduler is therefore both an
independent confound (Item 2) *and* an aggravating cause of the Item 1 deadlock.

### The fix (design)
Replace the heartbeat's use of `has_unanswered_outbound` with a new, purpose-built
anti-**stack** gate, `has_unanswered_proactive(user_id, window_minutes)`:

- Look at the **single most recent outbound** (any day; the window bounds
  staleness, not a UTC-midnight cutoff).
- Block **only if** that outbound is (a) a **proactive/heartbeat** message
  (`message_type == "heartbeat"`), (b) **unanswered** (no inbound after it), and
  (c) **within `window_minutes`** of now.
- Otherwise allow the tick to reach the decision call.

This gives:
- **A time-elapse clear condition** (window expiry) that needs no user input —
  the deadlock is broken.
- **Reactive replies never block** proactive initiation — they aren't
  `heartbeat`-typed (freshness of a live exchange is already handled by the
  separate `active_conversation` guardrail on last-inbound age).
- **Legacy briefings never block** the heartbeat (point 4 neutralized) — Item 2
  disables them anyway, but the gate is correct even if legacy is re-enabled.
- **Anti-stack preserved:** a second proactive nudge within the window, on top of
  an unanswered first, is still suppressed.

It stays a **code-enforced guardrail** (never a prompt rule). `guardrail_reason`
returns `"proactive_stack"` (renamed from `unanswered_gap` to name what it now
enforces). `has_unanswered_outbound` is **kept unchanged** for the legacy
scheduler's own use (reversibility).

New knob: `HEARTBEAT_STACK_WINDOW_MINUTES` (default 180 = 3h). Within 3h of an
unanswered proactive nudge, don't stack a second; after that a fresh opening is
the product working, not a violation.

---

## Item 2 — the legacy proactive scheduler is still live

### Enumeration of legacy proactive jobs (scheduler.py)
Registered in `start_scheduler()` → `schedule_all_users()` → `schedule_user()`:

| Job id pattern | message_type(s) | Trigger |
|---|---|---|
| `user_{id}_morning_briefing` (+`_alt`) | `morning_briefing` | wake_time |
| `user_{id}_pre_workout` | `pre_workout` | 15m before workout |
| `user_{id}_post_workout` | `post_workout` | 75m after workout |
| `user_{id}_evening_wrap` | `evening_wrap` | 90m before sleep |
| `user_{id}_weigh_in` | `weigh_in` | weigh_in_day |
| `global_adherence_check` | `adherence_gentle`/`_firm` | daily 20:00 UTC |

All per-user jobs run through **`send_scheduled_message`** →
`coach.generate_scheduled_message` (legacy `claude-sonnet-4-6`). The global
adherence job runs **`check_meal_adherence`** → `send_scheduled_message`.

`schedule_user` has **three callers**: `schedule_all_users` (boot),
`app.py:1391` (profile update), `onboarding_agent.py:874` (onboarding complete).
Gating `schedule_user` itself covers all three.

### Not proactive-to-user — keep running
- `daily_dining_scrape` (`scrape_all_halls`) — scrapes dining halls into context;
  no outbound to the user. **Keep.**
- `global_heartbeat`, `nightly_consolidation`, `episodic_digest_sweep` — the new
  systems. **Keep**, each on its own flag already.

### The fix (design) — disable behind a reversible flag, default off
New flag `LEGACY_SCHEDULER_ENABLED` (default `false`). Defense in depth so no
legacy proactive outbound can fire during burn-in, and re-enabling is a single
flag flip:
1. `schedule_user` — early-return when off (registers no per-user jobs; covers all
   three callers).
2. `send_scheduled_message` — early-return when off (any legacy send path is a
   no-op even if a stray job exists).
3. `check_meal_adherence` — early-return when off.
4. `start_scheduler` — register `global_adherence_check` only when on.

Verified clean: the scheduler still starts, `daily_dining_scrape` and
`global_heartbeat` still register, and no code references a now-unregistered job
(the jobs are only ever added by the gated paths above).

**Founder decision surfaced (not executed silently):** default recommendation is
to disable legacy and let the heartbeat own all proactive contact — that is the
architecture the burn-in exists to validate. The alternative (consciously keep
the morning briefing, scope the heartbeat around it) is left to the founder;
proceeding on disable unless redirected. See CHANGESPEC.md §Decision.

### Scope held
No deletion (that is Phase 6's playbook, gated on a clean burn-in — disabling is
reversible, deletion is not). No allowlist widening until the heartbeat has
demonstrably initiated on its own.

---

## Sequencing rationale
Item 2 lands first: removing the legacy briefings eliminates the daily
re-latching of the gap, so Item 1 is diagnosed and fixed with the confound gone.
Item 1's gate is then correct independent of whether legacy is ever re-enabled.
