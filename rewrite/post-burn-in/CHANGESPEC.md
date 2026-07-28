# Post-burn-in follow-ups — CHANGE SPEC

Numbered *what's-there-now / what-it-becomes / why / where*, per item.

### 1. Migrations run automatically at boot, and a genuine failure now blocks it

- **Now:** `migrate.py` runs manually after a Railway auto-deploy; `run_migrations()`
  logs `FAILED: ...` on a genuine (non-"already exists") error and continues — the
  script exits 0 either way, so a broken migration does not block boot today.
- **Becomes:** `run_migrations()` tracks failures that aren't "already exists" and
  raises `RuntimeError` after the loop if any occurred; "already exists" is still
  silently swallowed (idempotency preserved — every normal re-run must still succeed).
  `Procfile`: `web: python migrate.py && python app.py` — an uncaught raise exits
  non-zero, so `&&` actually blocks `app.py` from starting against a half-migrated
  schema. Also removed a stray module-level `logger.info("Migration complete.")` that
  fired on every `import migrate` (including in tests), independent of whether
  migrations ran — dead/misleading code directly in the function being changed.
- **Why:** a schema-dependent deploy shouldn't race a manual step, and "block boot on
  failure" wasn't actually true before this — it just happened to never fail yet.
- **Where:** `migrate.py` (`run_migrations`); `Procfile`; `README.md` Deployment
  section (dropped the manual step from the current-facing doc; historical
  `rewrite/phase-*/SUMMARY.md` deploy notes left as dated records, not touched).

### 2. `manage_log` event edit — a `date` field moves the event to a different day

- **Now:** `_EDIT_FIELDS["event"]`'s `event_time` kind ("starts_at"/"ends_at") always
  derives the target day from the row's CURRENT `occurred_at` — editing the time can
  never change the day. No field exists to move a day.
- **Becomes:** a new `"date"` edit field, kind `event_date`. Resolves `today` /
  `tomorrow` / `YYYY-MM-DD` (via a new `_resolve_local_date(tz, date_str, strict=...)`,
  factored out of `_parse_local_dt`'s existing date logic — `_parse_local_dt` itself is
  unchanged in behavior, just delegates). On edit, both `occurred_at` and `ends_at`
  (whichever are set) move to the new local day, EACH KEEPING its own existing local
  time-of-day; each moved column gets its own `edits` audit entry. Bad date input
  returns an `error:` string (strict mode) rather than a silent wrong day.
  `event_date`-kind fields are processed BEFORE `event_time`-kind fields within the
  same call (explicit sort, not dict order) so `{"date": ..., "starts_at": ...}` in one
  call combines onto the new day rather than the old one.
- **Why:** "summit got pushed to Friday" was previously inexpressible as an edit —
  forcing delete-and-relog, which destroys the audit trail the burn-in-fixes PR's
  `edits` column exists to preserve.
- **Where:** `agent_tools.py` (`_resolve_local_date`, `_EDIT_FIELDS["event"]["date"]`,
  the edit branch's field ordering + `event_date` handling, tool description hint);
  `prompts/voice.md` (day-move is an edit, not delete-and-relog).

### 3. One-time timestamp audit (data, not code)

- **Now:** no tooling exists to see which pre-fix `events`/`schedule`-memory values
  might be 7h/a-day off from what the corrected `timefmt` boundary would render.
- **Becomes:** `audit_timestamps.py` (root, read-only, no mutation) — prints every
  active `Event` row for the target user(s) with both the raw stored UTC and the
  `timefmt.render_time`/`render_date` rendering side by side, plus any dated
  `schedule`-category `user_profile_memory` facts flagged for manual review (free
  text, not machine-correctable). Filters by phone or user id; defaults to all active
  users (pre-launch, that's one profile).
- **Why:** consolidation merges/closes stale facts but never re-derives their values —
  a nightly pass will not self-heal pre-fix data. A human needs visibility to correct
  it directly.
- **Where:** `audit_timestamps.py` (new).

### 4. Heartbeat burn-in prep

- **Now:** `HEARTBEAT_WEB_SEARCH` defaults `"true"` ("ships ON during burn-in, measure
  later"). No regression test pins that the proactive path's context actually carries
  the `now` anchor / code-computed totals / today's events the burn-in-fixes PR added
  to the reactive loop.
- **Becomes:** `HEARTBEAT_WEB_SEARCH` defaults `"false"` — a deliberate burn-in
  decision (unbounded cost multiplier on an unmeasured speak rate), with the config
  comment saying so explicitly; reactive search unchanged. Two new tier-1 tests:
  the proactive tool set carries no `web_search` entry when the flag is off (the
  default), and `heartbeat._proactive_context` for a user with a meal + event logged
  contains the `now` anchor, the `TODAY'S TOTALS` block, and the event — pinning that
  the reactive fixes are actually visible to the proactive path, not just assumed.
- **Why:** resolves the spec's explicit "resolve the search-flag question" ask, and
  turns "should already work" into "provably does, and stays that way."
- **Where:** `config.py` (`HEARTBEAT_WEB_SEARCH` default + comment); `tests/tier1/test_heartbeat.py`
  (two new tests + an updated stale comment on the existing conditional search
  assertion). `HEARTBEAT_ENABLED`/`HEARTBEAT_ALLOWLIST` untouched — flipping those in
  prod is the founder's step, out of scope here.

## Tests (tier-1, red-first → green)

`test_migrations.py` (+2: swallows "already exists", raises on a genuine failure);
`test_manage_log_edit.py` (+6: correct new-day UTC instant + keeps time-of-day, audited,
buckets into the new local day via `timefmt.local_day_bounds`, combined date+time in one
call lands on the new day, bad date rejected); `test_heartbeat.py` (+2: no `web_search`
tool when the flag is off by default, proactive context carries now/totals/events).
**149 passed, 2 pre-existing skips, 0 xfails** (up from 142 on `main`).

Tier-2 (written, not executed here — no funded Anthropic key in this workspace):
`tests/tier2/test_schedule_events_and_verify.py::test_summit_pushed_to_friday_is_an_edit_not_delete_and_relog`
(new); `test_heartbeat_sees_a_dated_event` (pre-existing, re-verified against current
code by reading, not by a live run).
