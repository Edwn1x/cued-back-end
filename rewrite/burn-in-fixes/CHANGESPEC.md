# Burn-in fixes — CHANGE SPEC

Numbered *what's-there-now / what-it-becomes / why / where*. Storage stays naive-UTC;
these are a rendering boundary + one write-path + one additive column. Context reshape is
behind `CONTEXT_LOCAL_TIME_ENABLED` (default on).

### 1. `timefmt.py` — the naive-UTC → local rendering boundary
- **Now:** tz/local-day logic is duplicated inline in 3 readers; no time renderer.
- **Becomes:** `timefmt.py` with `resolve_tz`, `to_local`, `local_day_bounds`,
  `render_time` (`9:51 PM PDT (2h ago)`), `render_date`, `now_anchor`. `local_day_bounds`
  is the one window `todays_events` / `recompute_daily_totals` / `build_loop_context §7`
  share. IANA name only (crosses Nov DST); logs when the default tz is used.
- **Why:** one documented place for the storage convention instead of twenty assumptions.
- **Where:** `timefmt.py` (new); `events._local_day_window_utc` and `models.recompute_daily_totals`
  delegate; `agent_loop.build_loop_context §7` uses it.

### 2. Local time everywhere in context + a local "now" anchor
- **Now:** `## NOW` is UTC and tells the model to convert itself; meals/events/workouts
  render `%H:%MZ` / `%m-%d` (bare UTC, 7h ahead in PDT).
- **Becomes:** `## NOW` = `now_anchor(user)` (`Right now: Tuesday, July 21, 2026, 2:11 PM
  PDT (America/Los_Angeles)`); events/meals/workouts/episodic all render through
  `render_time`/`render_date`. Heartbeat inherits it via `build_loop_context`.
- **Why:** the model needs a local clock to resolve "tomorrow/later" and must never see a
  bare UTC stamp.
- **Where:** `agent_loop.build_loop_context` (all render sites); flag `CONTEXT_LOCAL_TIME_ENABLED`.

### 3. Code-computed macro totals block
- **Now:** meals are listed per-row with macros; **no total**. The model sums them → wrong.
- **Becomes:** a `## TODAY'S TOTALS` block after the meal list: code sum over the
  already-fetched active meals (soft-delete filtered, local-day windowed) + code-computed
  remaining vs `calorie_target`/`protein_target`. The row-sum is the single number that
  reaches the prompt; `calories_today` counters stay for legacy use, unrendered.
- **Why:** LLM arithmetic drifts; approximately-right totals are worse than reliably wrong.
- **Where:** `agent_loop.build_loop_context §7`; `voice.md` quote-don't-add rule.

### 4. `manage_log` — editable events + audited edits
- **Now:** `edit` exists but `_EDITABLE["event"]` is only `{event_type, ends_at}` and does a
  raw `setattr` (a local `HH:MM` start would store unconverted, 7h off); no edit is audited.
- **Becomes:** a per-entity **field-map** (`_EDIT_FIELDS`) with coercions — events gain
  `description→raw_text`, `starts_at→occurred_at`, `ends_at→ends_at` (local `HH:MM` → naive
  UTC on the row's **current** local day via `_parse_local_dt`, so editing the time keeps
  the day); ints coerced; partial edits touch only supplied fields. Every change appends
  `{at, field, old, new}` to a new `edits` JSON column and `flag_modified`s it. Meals still
  `recompute_daily_totals` (recompute-never-adjust). Existing `active()` query already
  rejects invalid / wrong-user / soft-deleted ids.
- **Why:** the live "can't get the event's id → mentally drop it" gap; and an edited row
  silently rewrites history without an audit trail.
- **Where:** `agent_tools` (`_EDIT_FIELDS`, edit branch, tool `fields` hint); `models.py` +
  `migrate.py` (`edits JSONB` on meals/workouts/events); `voice.md` edit-vs-delete rule.

## Tests (tier-1 red-first → green)
`test_timefmt.py` (7: local render, anchor, DST PDT/PST, non-default tz, default-tz log,
log_event round-trip, no-bare-UTC context guard); `test_totals_block.py` (4: soft-delete
excluded, remaining exact, no-target omits line, 11pm-local windowing); `test_manage_log_edit.py`
(5: meal round-trip+recompute+audit, event start edit reflected in context + delete,
edit-keeps-day, partial update, errors never false-success incl. cross-user/deleted/bad-field);
+ migration `edits`-column assertion. **140 passed, 0 xfails.**
Tier-2 `test_burn_in_fixes.py` (3, funded key): names the local day; quotes totals digit-for-digit;
corrects via edit not delete-and-relog.
