# Daily rhythm — change spec (2026-09-22)

Source: user 32 (16, the founder's sister), verified in prod: "reminders and check-ups are
way too far apart, users will just end up forgetting; maybe water reminders". The heartbeat
evaluates her ~30x/day and speaks 0–1x. Silence reasons: (1) the global 21:00–08:00 quiet
window mutes a 6:50 waker's whole morning; (2) the model says "nothing new to add" because
no code-computed standing condition exists for food — an unlogged lunch at 2pm is never a
reason to speak; (3) the legacy fixed-slot briefings are flag-disabled and nothing replaced
their rhythm. Per the playbook: now / becomes / why / where. Tests red-first (tier-1 in
`tests/tier1/test_daily_rhythm.py`, tier-2 anchors in `tests/tier2/test_daily_rhythm_live.py`).
Every piece ships behind a flag that defaults OFF in `config.py`; flip in prod after deploy.

## 1. Meal-gap standing condition (`HEARTBEAT_MEAL_GAP_ENABLED`)

- **Now.** Standing conditions exist for TRAINING GAP, OPEN THREAD, MOMENTUM, weigh-in due.
  Food only reaches the heartbeat as TODAY'S TOTALS; "no meal logged for 7 hours" is date
  arithmetic the model never does, so it never speaks about it.
- **Becomes.** `_meal_gap_signal(user, session, now=)` — code-computed in the user's local
  time from `wake_time` (alt honoured by weekday; default 08:00), `sleep_time` (default
  23:00) and `meals_per_day` (default 3). Renders `## MEAL GAP (standing condition —
  code-computed)` when: nothing logged today by wake+5h ("no breakfast or lunch on record");
  or ≥6h since the last logged meal during waking hours; or within 2h before sleep with
  fewer meals than expected and ≥3h since the last ("evening close — anything not logged?").
  The block carries hours since the last meal, today's count vs expected, and whether a
  proactive text already went out during this gap (once-per-gap anti-repeat, code-dated;
  TICK HISTORY / RECENT PROACTIVE MESSAGES carry the wording). Softer branch when
  `food_logger_status == "coexist"` (sibling PR adds the column; read via getattr): it's
  probably in their app — ask for a screenshot of the day, never a re-type. HEARTBEAT_PROMPT
  names MEAL GAP as a valid reason to speak on its own, once per gap.
- **Why.** Precompute what the model would otherwise guess; the gap is the whole reason.
- **Where.** `heartbeat.py`.

## 2. Water reminders on the reminders engine (`WATER_REMINDERS_ENABLED`)

- **Now.** A `Reminder` is a local time + optional weekdays. "Every 2 hours" has no shape.
- **Becomes.** Nullable columns `every_hours` (Integer), `window_start`, `window_end`
  ('HH:MM' local; null = the user's wake/sleep, resolved at fire time so a changed wake time
  follows). One row = "every N hours between wake and sleep, every day". Slots anchor at the
  window start; `next_interval_fire_at` re-arms to the next slot inside the window and skips
  past the window end to the next day's start (windows may cross midnight). `fire_due` treats
  an interval row like a recurring one (re-arm, stay active; stale rows re-arm). `_compose`
  keeps a standing ping to a few words. `set_reminder` gains `every_hours` only when the flag
  is on (`set_reminder_tool()`), with hydration named as the canonical use; `time` becomes
  optional for interval rows. A short "drank"/"done"/👍 reply to a water ping rides the
  existing closing-ack branch in `app.py` (no model call, 👍 tapback on iMessage) via
  `reminders.is_water_ack` — never double-handled. Capability entry `water_reminders`.
- **Why.** She asked for water reminders; the engine already fires on schedule, independent of
  the heartbeat and exempt from quiet hours because the user named it. Inert without rows.
- **Where.** `reminders.py`, `models.py`, `migrate.py`, `agent_tools.py`, `agent_loop.py`,
  `app.py`, `capabilities.py`, `prompts/voice.md`.

## 3. Quiet hours from the user's own wake/sleep (`QUIET_HOURS_FROM_PROFILE_ENABLED`)

- **Now.** `_in_standing_quiet_hours` uses the global 21:00–08:00 window; a parseable
  profile time can only extend it. User 32 (up 6:50, bed 00:00) is muted 6:50–8:00 and
  texted-eligible 21:00–23:30 while she's still up.
- **Becomes.** When BOTH `wake_time` (alt honoured for the day's weekday) and `sleep_time`
  parse as 'HH:MM', quiet = sleep−30min until wake+15min; otherwise the global window as
  before. Free-phrase profiles change nothing.
- **Why.** The window should be theirs, not a fleet constant.
- **Where.** `heartbeat.py`.

## 4. Morning open + evening close (`HEARTBEAT_RHYTHM_ENABLED`)

- **Now.** Nothing replaced the legacy briefings' rhythm.
- **Becomes.** `_morning_open_signal`: within 90 min after wake and no non-reaction message
  either way since wake → `## MORNING OPEN` with ONE line of material (weekday, workout day
  per their split or rest day, today's logged events). `_evening_close_signal`: within 2h
  before sleep and no non-reaction outbound since 5pm local → `## EVENING CLOSE` with one line
  (today's totals vs target, workouts done today). Both say: one short line, a friend's
  morning text / evening check, not a briefing. Same once-per-day anti-repeat via history.
- **Where.** `heartbeat.py`.

## 5. Per-user check-in level (`SET_CHECKIN_LEVEL_TOOL_ENABLED`)

- **Now.** "text me more" / "chill with the texts" gets a spoken "ok" and nothing changes.
- **Becomes.** `users.checkin_level` VARCHAR(10) ('more' | 'normal' | 'less', null = normal).
  Coach tool `set_checkin_level` (flag-gated, capability entry, voice.md routing: never just
  say ok). Effect in code regardless of the flag (the column is the contract): 'more' → cap
  8/day; 'less' → cap 2/day and the meal-gap / morning / evening conditions don't render;
  'normal' → as built. `## CHECK-IN LEVEL` in the heartbeat context names the level and cap.
- **Where.** `models.py`, `migrate.py`, `agent_tools.py`, `agent_loop.py`, `heartbeat.py`,
  `capabilities.py`, `prompts/voice.md`.

## Flags to flip in prod (all default false)

`HEARTBEAT_MEAL_GAP_ENABLED`, `WATER_REMINDERS_ENABLED`, `QUIET_HOURS_FROM_PROFILE_ENABLED`,
`HEARTBEAT_RHYTHM_ENABLED`, `SET_CHECKIN_LEVEL_TOOL_ENABLED`.
