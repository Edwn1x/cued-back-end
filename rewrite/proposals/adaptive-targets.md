# Adaptive calorie targets — proposal (for the founder's read before building)

**The claim.** Every predictive equation is a one-time guess with roughly ±10% error, which is
±250 cal for a 139-lb lifter. PR #46 picks the best guess for our population (Ten Haaf at 5+
days). This proposal is the part that makes the number *right for the person*: watch what the
scale does against what they log, and move the target. It is how MacroFactor gets its accuracy,
and no static formula matches it.

**What exists today.**
- `users.calorie_target` / `protein_target`, written ONCE at onboarding completion. Every surface
  (agent loop, coach, profile page, heartbeat) reads those two columns. Nothing ever updates them.
- `meals` with per-meal calories; `calories_today` recomputed on every meal write (`recompute_daily_totals`).
- `weight_logs` (`weighed_at`, `weight_lbs`, `notes`) — the table exists, the admin page reads it,
  **nothing writes to it.** Zero rows in prod. `users.weight_lbs` is the single onboarding value.
- The heartbeat speaks on a STANDING CONDITION (a gap, a broken pattern, an open thread). A due
  weigh-in is exactly that kind of condition.
- voice.md already says a weigh-in screenshot is a durable fact → `remember`. It should be a
  `log_weight` write instead (column-as-source-of-truth rule; memory is the wrong place for a series).

## The loop, end to end

1. **Capture weight.** New tool `log_weight(weight_lbs, date?)` on the coach loop (same shape as
   `log_meal`/`log_workout`, past-day `date` allowed). Fires on "weighed in at 141 this morning",
   a scale screenshot, or a Fitbit/Strava weight card. Also updates `users.weight_lbs` (latest wins)
   so protein targets and the profile page follow. Never asked for at signup — onboarding stays
   conversational.
2. **Ask for it, lightly.** One standing condition for the heartbeat: *no weigh-in in 7+ days →
   worth one line, in the morning, once.* Copy is the friend's, not a form: "when you're up, hop on
   the scale real quick — same time, before food. i'll do the math." Skipped weigh-ins are never
   nagged twice in a week. If they say they don't own a scale, `remember` it and stop asking; the
   loop degrades to intake-only sanity checks (below).
3. **Smooth the scale.** Daily weight is noise (water, sodium, a big dinner). Use an exponentially
   weighted moving average (α = 0.1, the MacroFactor-style default) over logged weights, evaluated
   on the days a weight exists; the trend is the EWMA, never the raw number. The coach quotes the
   trend ("you're trending 138.2, down about 0.4 this week"), never a single reading.
4. **Estimate real expenditure.** Over a window, `expenditure ≈ mean_logged_intake + (Δtrend_lbs × 3500) / days`.
   Both inputs have to be trustworthy for the window to count:
   - **Intake completeness gate.** A day counts only if ≥ 2 meals were logged AND the coach
     context shows no "didn't log" admission that day. Fewer than 10 counted days in a 14-day
     window → no adjustment, and the coach says why ("i can only tune this if most days are
     logged — this week was 4 of 7").
   - **Weight gate.** ≥ 3 weigh-ins in the window, spanning ≥ 10 days.
5. **Move the target — small, bounded, explained.** Every 14 days, if both gates pass:
   - new_maintenance = 0.7 × estimated_expenditure + 0.3 × current_maintenance (damped; one bad
     fortnight can't swing it).
   - Re-apply the goal rule from `calculate_targets` (recomp −10%, cut −500, bulk +250 …) to the
     new maintenance, round to 50.
   - **Clamp** the change to ±150 cal per cycle and the target to [BMR × 1.1, BMR × 2.2]. A cut
     never goes below 1400 (the existing floor).
   - **Direction check against the goal.** Cut and the trend is flat/up → target goes down. Bulk
     and the trend is flat → up. Recomp and the trend is moving > 0.5 lb/week either way → nudge
     back toward flat. Trend already doing what the goal wants → *no change*, even if the math
     says one (the number that's working is the right number).
   - Write `users.calorie_target`, append a row to a new `target_adjustments` table (`user_id`,
     `at`, `old`, `new`, `est_expenditure`, `trend_delta_lbs`, `counted_days`, `reason`). The
     admin page shows the history; the profile page shows "target: 2450 → 2350 (Sep 28)".
6. **Tell them like a friend, once.** The adjustment is a coach-side fact injected into the next
   turn's context ("TARGET CHANGED TODAY: 2450 → 2350. Reason: trend flat 2 weeks on a cut.
   Mention it ONCE, in your words, no lecture; if they push back, hold the number and explain it's
   the scale talking, not you."). Never a templated system message. Never silently.

## What is NOT in scope
- No body-fat estimation, no Katch-McArdle. Nobody logs body fat.
- No daily "adaptive" churn. Two-week cadence, one line of copy, that's it.
- No changes to protein except following `weight_lbs` (protein is g/lb; a 5-lb trend move is 5 g).
- No adjustment for users on SMS who don't log meals. The gate handles it; the coach explains it.

## Failure modes to design against (learned the hard way elsewhere)
- **Sparse logging looks like a huge deficit.** Someone logs breakfast only for two weeks →
  "intake 800/day, weight flat → expenditure 800" → nonsense. The completeness gate is the whole
  game; without it this feature is actively harmful.
- **Water swings after a cheat day** read as +3 lb. EWMA + the 10-day span gate.
- **A user who reports weight in kg.** Tool takes `weight_lbs`; the model converts; log the raw
  text in `notes` so a 63 → 139 mistake is auditable.
- **Two calculators disagreeing** (what we just deleted). The adjustment reuses
  `calculate_targets`' goal rule; it never invents a second formula.

## Build plan (≈ 2 days)
1. `log_weight` tool + voice.md routing + `users.weight_lbs` follow — tier-1 tests. (½ day)
2. Heartbeat standing condition + one-line copy rule + "no scale" opt-out. (½ day)
3. `adaptive_targets.py`: EWMA, gates, estimator, clamp, direction check, `target_adjustments`
   table + migration (ADD COLUMN/TABLE only; pre-check + lock_timeout like the rest). Scheduler job,
   biweekly per user from their completion date. Tier-1 with synthetic 4-week series: flat-on-cut
   → down 150; dropping-fast-on-recomp → up; sparse logging → no change + reason. (1 day)
4. Coach context injection + admin/profile history. (¼ day)

## Evidence gate before calling it done
Live: the founder logs meals for 14 days and weighs in 3+ times. The first cycle's row in
`target_adjustments` is read by hand; the coach's one-line explanation is read on the phone. If
the gate blocks (most likely, given logging habits), the *explanation* is the deliverable of
cycle one, not the number.
