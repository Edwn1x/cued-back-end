# Burn-in fixes — INVESTIGATION

Three correctness issues from live burn-in (Jul 21, founder's number, Phase 2–5 stack).
Investigated against the current code, not assumptions. Storage stays **naive-UTC** — the
convention every reader already assumes (Phase 1/4).

## 1. Timestamps end to end

**Write sites (all store naive UTC):**
- `Meal.eaten_at` / `logged_at`, `Workout.date`, `Event.occurred_at` / `ends_at`,
  `Message.created_at`, `EpisodicDigest.occurred_on`, `HeartbeatTick.decided_at`,
  `ConsolidationRun.ran_at` — all default to `datetime.now(timezone.utc).replace(tzinfo=None)`
  (or the aware-UTC default that round-trips to naive UTC on the prod UTC server;
  the local disposable PG stores it as naive-local — a **test-only** artifact, per Phase 4).
- Model-supplied times: `handle_log_event` **already** converts local `HH:MM` → naive UTC
  via `_parse_local_dt(tz_str, date, hhmm)`. `handle_log_meal` logs "now" (no model time).
  **So the write side is already correct.** The one new write-side conversion is item 3's
  event-edit path (start/end), which currently `setattr`s raw.

**Render sites — all bare UTC (the bug):**
- `agent_loop.build_loop_context`: `## NOW` = `%A %Y-%m-%d %H:%MZ` (**UTC**, tells the model to
  convert itself); §3 events `%H:%MZ`; §5b episodic `occurred_on %m-%d`; §7 meals `%H:%MZ`;
  §8 workouts `%m-%d`. §6 conversation window renders **no** per-message time.
- `heartbeat._proactive_context`: last-outbound age is relative ("~Xh"), fine; any absolute
  time goes through the same bare render.

**Root cause:** the model has no *local* clock to anchor relative-time reasoning against, and
every timestamp it does see is 7h ahead (PDT) — worst after 5pm local where UTC is tomorrow.
No `render_time` helper exists.

## 2. What "today" means, per reader

| Reader | Windowing today |
|---|---|
| `events.todays_events` → `_local_day_window_utc(user)` | **local day** (zoneinfo) ✓ |
| `models.recompute_daily_totals` | **local day** (inline zoneinfo) ✓ |
| `agent_loop.build_loop_context §7` (today's meals) | **local day** (inline zoneinfo) ✓ |
| workout confirmation (`confirm_workout_today` / `is_workout_confirmed_today`) | to verify; used by legacy scheduler (deleting Phase 6) |
| consolidation (`_age_days`) | relative age in days, tz-agnostic ✓ |
| episodic (`recent_episodic` cutoff) | `now - N days`, tz-agnostic ✓ |

**Finding:** local-day windowing is **already correct** where it matters (meals/events/totals),
but the logic is **duplicated inline in 3 places** — a latent drift risk the spec flags.
Consolidate onto one `local_day_bounds(user)`.

## 3. Macro totals path

- Denormalized counters `calories_today/protein_today/carbs_today/fat_today` + `totals_date`
  exist on `User`, recomputed by `recompute_daily_totals` (local day, `active()`-filtered) on
  every meal write/delete/edit — so they cannot drift *by construction*.
- **BUT neither the counters nor a row-sum reach the prompt.** `build_loop_context §7` lists
  meals per-row with macros and NO total. The model sums them itself → wrong arithmetic.
- **Hypothesis (WRONG):** "the counters are rendered and stale." Checked — they are *not*
  rendered at all. The fix is to render a code-computed total, not to fix a stale counter.
- Resolution: sum the active meals already fetched in §7 (single authoritative number, soft-delete
  filtered via `active()`), + remaining vs `calorie_target`/`protein_target`. Counters stay for
  any legacy reader but don't reach the prompt.

## 4. `manage_log` edit — build or test-and-surface?

- **`edit` is fully built** (`handle_manage_log` has list/delete/**edit**; `_ENTITY_MODEL` +
  `_EDITABLE` per entity). Events already **list and delete** (PR #12) and `todays_events`
  honors soft-delete (PR #12). So item 3 is **not** a build-from-scratch.
- **Two real gaps:**
  1. `_EDITABLE["event"] = {"event_type", "ends_at"}` — missing **description** (`raw_text`)
     and **start** (`occurred_at`); and `edit` does `setattr(row, k, v)` raw, so a model-supplied
     local `HH:MM` for start/end would be stored unconverted (7h off) — the same shape as the
     original `log_event` bug, one layer down.
  2. **No audit trail.** `edit` mutates and commits with no prior-value retention. A deleted row
     is obviously gone; an edited row silently claims to have always held its new value.
- Existing guards are sound: query is `active(session, Model, user_id=…)`, so invalid id,
  wrong-user id, and soft-deleted id already return explicit errors (never false success);
  meal edits already `recompute_daily_totals`.

## Sequencing
1 (timefmt + local render) → 2 (totals block reuses `local_day_bounds`) → 3 (event edit fields
reuse `_parse_local_dt`; audit is independent). Storage untouched; rendering + one write-path
(event-edit) + one additive schema column (`edits`).
