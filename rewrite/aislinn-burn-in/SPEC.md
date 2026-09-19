# Aislinn burn-in fixes — change spec (2026-09-19)

Source: reading user 32's live log end to end (Sept 14 → 19) after the heartbeat redeploy.
Per the playbook: what's-there-now / what-it-becomes / why / where. Tests red-first
(tier-1 in `tests/tier1/test_aislinn_burn_in.py`, tier-2 anchors in
`tests/tier2/test_aislinn_burn_in_live.py`).

## 1. Bodyweight templates + equipment-aware card

- **Now.** `workouts/templates.py` has one barbell template set; `build_session` ignores
  `user.equipment`. A `bodyweight` user (signup radio) gets squat/bench/row/OHP/RDL. The
  card state, summary and per-exercise texts all assume a weight (`s.done and s.actual_weight`
  gates done-count/volume/lines, so a weight-0 set is invisible even when done).
- **Becomes.** A `BODYWEIGHT_TEMPLATES` set (same keys) with `default_weight=0`, progression
  by reps (`rep_step`); `templates_for(user)` picks by `equipment == "bodyweight"`; every
  reader treats weight-0 done sets as done (count, lines, summary) and renders reps-only
  ("pushup · 10 × 3", "pushup — 10 · 10 · 12", "9 sets" instead of "0 lb"). `build_state`
  carries `bodyweight` flags per exercise + per session so the site card can hide the
  weight column (site change is a follow-up in cued-site; the backend contract is here).
- **Why.** Onboarding promised "bodyweight stuff in your room"; the card broke the deal.
- **Where.** `workouts/templates.py`, `workouts/plan.py`, `workouts/start.py`,
  `workouts/card.py`, `workouts/summary.py`, `card_page.py`.

## 2. Training-gap standing condition (heartbeat)

- **Now.** `_recent_win_signal` renders only when there ARE completed workouts; a user who
  never trained produces no training material at all. Zero training mentions in 5 days.
- **Becomes.** `_training_gap_signal(user, session)` — code-computed: committed frequency
  from `workout_days`/`confirmed_training_days` (count of day names, or the low end of "3-4",
  default 3), days since the last completed workout (Workout.completed or WorkoutSession
  done), or days since they joined when there is none; an untouched/abandoned card is named.
  Renders `## TRAINING GAP (standing condition)` once the gap is ≥ ceil(7/per_week)+1 days.
- **Why.** The playbook: precompute what the model will be asked for. "Days since" is date
  arithmetic the model can't do from a transcript.
- **Where.** `heartbeat.py`.

## 3. Re-estimate write-back (usda affordance + yesterday's meals + voice)

- **Now.** `usda_food_lookup` returns per-100g numbers and nothing else; the coach told her
  "~240 cal, 27g" and never called `manage_log`. Context lists only TODAY's meals with ids,
  so a correction about yesterday has no id to edit.
- **Becomes.** (a) The lookup result names any logged meal from today/yesterday whose
  description overlaps the query and says: call `manage_log edit` on that id if this changes
  its numbers. (b) `build_loop_context` adds `## YESTERDAY'S LOGGED MEALS` (ids + macros,
  labeled past-day, never added to today). (c) voice.md: a re-estimate of a logged item is a
  correction — write it before quoting it; explain a gap in the direction it actually runs.
- **Why.** Affordance-in-tool-result lesson (macro-accuracy branch); the honesty invariant.
- **Where.** `agent_tools.py`, `agent_loop.py`, `prompts/voice.md`.

## 4. Open question expires at the day boundary

- **Now.** The heartbeat carried "open mcmuffin question, still her turn" across 20 ticks
  and the next morning's reply reopened it; nothing in context says the question is stale.
- **Becomes.** `_open_thread_signal` — code-computed: last outbound is a question with no
  inbound after it → `## OPEN THREAD` with its age; if it was sent on a previous LOCAL day
  it is marked EXPIRED (don't reopen/re-ask; lead with today). HEARTBEAT_PROMPT + voice.md
  carry the rule.
- **Where.** `heartbeat.py`, `prompts/voice.md`.

## 5. Legacy classifier word boundaries

- **Now.** `classify_message` does `kw in body_lower` — "whites" contains "hit" → a meal text
  becomes `workout_log`, sets at_gym + workout_confirmed. Fired twice on user 32.
- **Becomes.** Word-boundary regex for the lift keywords. State writes unchanged.
- **Where.** `app.py`.

## 6. Admin send double-post

- **Now.** Two POSTs 1ms apart → two identical bubbles. No client or server guard.
- **Becomes.** Server: same (user, body) within 10s is a no-op (`status: duplicate`).
  Client: in-flight guard on all three send forms.
- **Where.** `app.py`, `admin_dashboard.py`.

## 7. Small

- 7a `sanitize_facts` neutralizes gendered third-person pronouns ("his app" → "their app");
  the extractor prompt says so too. Live: "his calorie-counting app" on a female user.
- 7b First `log_weight` sets `weigh_in_day` to that local weekday when unset, so "same time
  each week" has an anchor.
- 7c When protein follows weight in `log_weight`, `protein_target_computed` follows too (the
  15% band in `set_targets` reads it).
