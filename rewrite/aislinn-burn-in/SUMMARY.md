# Aislinn burn-in fixes — summary (2026-09-19)

Spec: `SPEC.md` in this directory. Source: user 32's live log, Sept 14 → 19.

## What changed

| # | Finding (live) | Fix | Where |
|---|---|---|---|
| 1 | Bodyweight user got the barbell full-body card; weight-0 done sets were invisible | `BODYWEIGHT_TEMPLATES` (same split keys, reps progression via `rep_step`), `templates_for(user)` by the typed `equipment` column; done-count / summary lines / SMS texts / bubble caption all reps-only; `build_state` carries `bodyweight` flags for the site card | `workouts/templates.py`, `plan.py`, `start.py`, `card.py`, `summary.py`, `card_page.py` |
| 2 | 5 days, zero training mentions from the coach | `_training_gap_signal`: code-computed days since last completed workout (or since joining) vs committed frequency (`workout_days` → int; low end of a range); names an unfinished card; renders at ≥ ceil(7/n)+1 days | `heartbeat.py` |
| 3 | Coach re-estimated the muffin via USDA, said the new number, never edited the row | USDA result names overlapping logged rows (today + yesterday) with ids and says to `manage_log edit` first; `## YESTERDAY'S LOGGED MEALS` block (ids, past-day, never summed into today); voice rule "a re-estimate IS a correction" + explain a gap in the direction it runs | `agent_tools.py`, `agent_loop.py`, `prompts/voice.md` |
| 4 | "open mcmuffin question, still her turn" for 20 ticks, reopened next morning | `_open_thread_signal`: last outbound is a question with no inbound after → OPEN THREAD; from a previous local day → EXPIRED (don't reopen, not a reason for silence); HEARTBEAT_PROMPT + voice rule | `heartbeat.py`, `prompts/voice.md` |
| 5 | "egg wHITes" → `workout_log` (at_gym, workout_confirmed), twice | Word-boundary regex for the lift keywords | `app.py` |
| 6 | Two identical admin bubbles 1 ms apart | Server: same (user, body) within 10 s → `status: duplicate`, no send; client: `_sendInFlight` guard on all three forms | `app.py`, `admin_dashboard.py` |
| 7a | "his calorie-counting app" on a female user | `neutralize_pronouns` in `sanitize_facts` (his/her/him/-self → their/them/themself) + extractor prompt rule | `memory.py`, `app.py` |
| 7b | "same time each week" with no weigh-in day | First `log_weight` sets `weigh_in_day` to that local weekday when unset | `agent_tools.py` |
| 7c | Protein followed weight, computed field didn't | `protein_target_computed` follows too (the 15% band in `set_targets` reads it) | `agent_tools.py` |

## Tests

- **Tier-1** (`tests/tier1/test_aislinn_burn_in.py`, 33 tests, red-first → green): templates/plan/
  progression/state/summary/SMS text for bodyweight; training-gap thresholds per frequency
  (parametrized), card naming, proactive-context wiring; open-thread today vs expired vs answered;
  USDA affordance (overlap, no overlap, soft-deleted); yesterday block vs today's totals; classifier
  whole words (6 cases); admin dedupe + form guards; pronoun neutralization; weigh-in anchor +
  computed protein. Full tier-1: **702 passed, 2 skipped**.
- **Tier-2** (`tests/tier2/test_aislinn_burn_in_live.py`, live model, 7 tests, first run **7/7**):
  - gap anchor ×3: spoke every time, about a bodyweight session ("5 days in and we haven't done a
    single session yet. wanna knock out a quick bodyweight one tonight, 20 min in ur room").
  - no-revive ×3: never re-asked the muffin question; the one silent tick's reason literally
    says "expired thread shouldn't be reopened".
  - write-back: the muffin row was EDITED (240 cal / 24g, audit trail), one active meal, and the
    reply challenged the 77g in the right direction ("that's not 77g tho … where'd the 77 come from").

## Judgment calls (findable, reversible)

- Bodyweight selection keys off `equipment in {bodyweight, none, no_equipment}` only; `home_gym` and
  `limited_gym` still get the barbell set (unknown kit — a follow-up, not a guess).
- Bodyweight PRs are not detected (`check_pr` needs a load); the summary shows sets, no "0 lb".
- The site card (cued-site `card.html`) still renders a weight column; the backend now sends
  `bodyweight: true` per exercise/session for it to hide. Follow-up in that repo.
- Gap threshold = ceil(7 / per_week) + 1 days (3/wk → 4 days; 4/wk → 3). A range ("3-4") uses its
  low end — the commitment they'd defend. Unknown → 3.
- `_looks_like_question` = "?" or a wh-/aux opener; subject pronouns (he/she) are not neutralized
  (facts are written subjectless; "they is" would be worse than the miss).
- Classifier state writes (at_gym / workout_confirmed) are left in place; only the match tightened.

## Not done here

- cued-site card: hide the weight column when `bodyweight` is set.
- `home_gym` / `limited_gym` template sets.
- Reactive-loop training nudge (the heartbeat owns proactive; the reply path sees RECENT WORKOUTS).
