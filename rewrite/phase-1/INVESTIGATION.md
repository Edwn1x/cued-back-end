# Phase 1 — INVESTIGATION

**Goal:** give the model true facts to reason over — Event table + synchronous
status detection, split pointer, validity windows + history + substring-match —
plus the verified webhook `MessageSid` idempotency fix surfaced in Phase 0. No
agent changes; legacy agents may consume new state read-only where trivial.

**Which Phase-0 xfails flip here** (each XPASSes → strict-fails the build → its
marker is removed in the commit that fixes it):
- `test_already_at_gym_suppresses_pre_workout_nudge` (failure 3) — Events feed the gate
- `test_healed_injury_leaves_active_context` (failure 5a) — validity invalidation
- `test_changed_numeric_fact_yields_one_current_value` (failure 5b) — substring-match + validity
- `test_duplicate_message_sid_produces_one_inbound` (idempotency) — MessageSid dedup

---

## 1. Migration mechanism (how schema changes ship here)

- **Fresh DBs:** `models.py` is source of truth; `init_db()` → `Base.metadata.create_all`.
- **Prod (Railway):** hand-rolled `migrate.py` — an idempotent `MIGRATIONS` list of
  `ALTER TABLE … ADD COLUMN` / `CREATE TABLE IF NOT EXISTS` run once (`python migrate.py`),
  each wrapped in try/except that swallows "already exists". No Alembic.
- **Phase 1 pattern per schema change:** (a) add column/table to `models.py`; (b) append the
  idempotent SQL to `migrate.py`; (c) a migration **test** that seeds a fixture DB with
  realistic current-shape rows (incl. a populated legacy `user_profile_memory` JSON and the
  scrubbed founder-row export) and asserts nothing is lost/corrupted. Tests run on real PG18,
  so `CREATE TABLE`/`ALTER` behave as prod.

## 2. Memory entry shape → validity windows

Current entry (`memory._new_entry`): `{id: 12-hex, text, ts: ISO-utc, uses: int, [safety: true]}`.

**Add (optional, back-compat):** `valid_from` (default = existing `ts` when absent),
`invalidated_at` (ISO | absent = currently valid), `invalidated_by` (str reason: "user_healed"
| "superseded" | "admin" | …). An entry with no `invalidated_at` is valid — so **old rows
need no backfill** (absence = valid). History store: invalidated entries move to a per-profile
`_history` bucket (or per-category `history` list) that the **injectable-size budget ignores**
(`_profile_total_chars`/`_enforce_caps` count only live entries). Decision to finalize in the
change spec: single top-level `__history__` list vs per-category — leaning single top-level list
so category caps and dedup only ever see live entries.

**Every reader/writer of entries (validity-filtering surface — all must skip invalidated):**
- `memory.render_categories` / `_compose_block_from_profile` — injection read → filter to valid.
- `memory._find_duplicate` (dedup) — **must consider only valid entries** (re-injured shoulder is
  a new fact, not a dup of the healed one) — spec-mandated.
- `memory._evict_one` / `_enforce_caps` — operate on live entries only; history is exempt.
- `memory.update_memory_uses_task` (memory.py:997) — bump `uses` only on live entries.
- `app.py:184` debug/render of the profile — filter too.
- Writers: `apply_facts` (add/update/skip + dedup), `apply_safety_signals_task`,
  `extract_and_store_memory` (app.py:318-325). Row-locked + `flag_modified` discipline preserved.

**Substring-match replaces (5b):** today `apply_facts` update path is **byte-exact**
(`e["text"] == replaces_text`, memory.py:505). Becomes: distinctive-substring match that
requires a **unique** hit within the category; ambiguous (0 or >1) → the existing
mismatch-logged-then-add fallback (never a guess). This is what lets "trains 5 days" supersede
"trains 3 days" instead of coexisting.

## 3. Events vs session_state

- `session_state` (JSON on User): `{status: at_gym|workout_logging|…, started_at}`, auto-clears on
  calendar-day change and at 2h for `at_gym` (`models.get_session_state`). It's transient and
  single-slot — set synchronously in the webhook (Phase 0 §2 step 13) but **no scheduler gate
  consults it for "already went"** (only the pre_workout probe reads it).
- **Events (new):** append-only table (went_to_gym, in_class[end], skipped, ate, traveling, life)
  written **synchronously on the inbound path**, deterministically for the two nudge-critical
  types (gym-went, in-class), following the **safety pre-pass floor pattern** (regex, no LLM,
  runs before the buffer at the webhook top — same seam as `apply_safety_signals_task`, Phase 0
  §2 step 5). Time expressions ("in class till 2") resolve against `user.user_timezone`; no stated
  end → sensible default duration (investigate what each gate needs — `in_class` needs an end so
  the nudge can resume after). A reader exposes **today's events** for injection + gate logic.
- **Relationship:** Events are the durable record the scheduler gates read; `session_state` stays
  for live in-session flow (logging mode). Don't double-write the same truth — Events own
  "happened today," session_state owns "currently mid-activity".

**Scheduler gates to wire (from Phase 0 §3, `scheduler.send_scheduled_message`):**
`_is_training_day`, the `pre_workout`/`post_workout` branches, and the pre_workout session probe.
Gate change: a `went_to_gym` event today suppresses pre_workout; an `in_class till HH` event
suppresses nudges until end time. This flips failure-3.

## 4. Split pointer

**Reality today:** a logged workout is written `workout_type="logged"` (app.py:761, 1087) — the
specific split **day (push/pull/legs) is not recorded**. `user.current_split` /
`confirmed_training_split` hold the split **system** (ppl/upper_lower/…). `coach.py`
(`get_training_day_status`, ~40-65) re-derives "today = legs" from `current_split` + time every
turn — the drift source (failure 4).

**Pointer (two facts only):** `last_completed_split_day` (e.g. "push") + `last_completed_at`.
- **Advanced only by code**, at the workout-log / confirmation seams: `_create_in_progress_workout`
  + `_handle_logging_mode_message` (logging mode, app.py:753/856), the one-shot `workout_log`
  branch (app.py:1082-1096), and freeform training-day confirmation (app.py:1106-1115) — all of
  which currently call `confirm_workout_today` + `set_session_state("at_gym")`.
- **Policy (founder-set):** unnamed "already went" → advance pointer to the **rule-expected next
  day** given `current_split`; a **named** day ("did legs") overrides; a **missed** day **shifts**
  the cycle (does not skip the slot). No cycle rule engine — the model derives "today is X unless
  something's up" from the pointer at reasoning time (Phase 2). Phase 1 only stores + advances it.
- Deriving "rule-expected next day" needs the split-system → day-sequence mapping; investigate
  whether `current_split` values map deterministically (ppl → push→pull→legs→…) at build time.

## 5. MessageSid idempotency (NOT flag-gated — ships live on merge)

**Verified root cause (Phase 0):** webhook reads `From/Body/NumMedia/MediaUrl0`, **never
`MessageSid`**; no dedup; `Message` has no provider-sid column/constraint. A slow synchronous
`classify_message` before Twilio's HTTP response exceeds Twilio's ~15s webhook timeout → Twilio
re-delivers → whole synchronous pass + a fresh buffer cycle run twice → double write.

**Most conservative design (this is the one item that live-patches the founder's own rail):**
- **Storage:** a dedicated `processed_messages` table with a **UNIQUE** column on the Twilio
  `MessageSid` (+ `user_id`, `received_at`). Preferred over a nullable unique col on `Message`
  because it's append-only, cheap, and independently revertable, and doesn't touch the hot
  `messages` table's shape.
- **Claim lifecycle (the subtle part — when the sid is claimed decides the failure mode):**
  - **Claim at the top** of the webhook (after user lookup, before `log_incoming`/safety/classify)
    so *concurrent* retries — the observed bug: Twilio re-delivers while the slow first pass is
    still running — hit the unique violation and early-return. On unique-violation: early-return
    empty TwiML + loud `WEBHOOK_DUPLICATE sid=… user=…`.
  - **Release the claim on unhandled exception** in the synchronous pass (delete the row / mark
    failed) so a pass that *crashes after claiming* doesn't leave a poisoned claim that would
    silently drop Twilio's retry — that would be fail-closed sneaking into a fail-open design.
    Known mode (slow LLM → late 200 → retry after success) **keeps** the claim; unknown mode
    (crash) **releases** it so the retry reprocesses cleanly. Requires the crash to yield a
    retry-eligible response — see change-spec decision on the existing friendly-200 handler.
- **Fail-open (critical):** a dedup *miss* must never drop a real message. Missing sid (shouldn't
  happen with Twilio) or any **non-uniqueness** insert error → **fall through and process** (a rare
  duplicate beats a dropped message). The uniqueness guarantee is the DB constraint, not app logic.
- **Two tests:** duplicate-sid-during-processing → exactly one set of writes; crash-after-claim →
  a resend with the same sid reprocesses (claim was released).
- **Isolated commit**, its own migration + migration test, and a **DEPLOY NOTE** in the phase
  summary (changes prod webhook behavior for every inbound the moment it merges; run `migrate.py`
  before/with deploy so the table exists). **Post-deploy observability:** grep Railway logs for
  `WEBHOOK_DUPLICATE` over a few days — hits correlating with slow requests both confirm the fix
  and *quantify* how often the duplicate bug was firing, closing the duplicate-meal loop with data
  instead of inference.

## 6. External facts to verify (spec Rule 1)

| Fact | Gates | Plan |
|---|---|---|
| Postgres UNIQUE-violation → SQLAlchemy `IntegrityError` semantics for the dedup early-return | MessageSid fix | verify against SQLAlchemy 2.0 docs; test on real PG18 (savepoint/rollback so the insert failure doesn't poison the request's session) |
| APScheduler behavior unchanged by adding event-gate reads | Events → scheduler | read-only gate additions; no scheduler API change — low risk, note in summary |
| Timezone math for "in class till 2" (pytz/zoneinfo already used) | Events | reuse `zoneinfo` as `get_session_state`/`ensure_todays_totals` do |

## 7. Open decisions (recorded; resolved in the change spec)

1. History store shape: single top-level `__history__` vs per-category (leaning top-level).
2. `valid_from` explicit vs derived-from-`ts` (leaning: optional, absence → `ts`).
3. Event storage: dedicated `events` table (append-only) — columns for type, start/end, source,
   raw text, created_at, user_id.
4. Split-system → day-sequence map location (config vs code) and whether unknown `current_split`
   values fall back to "don't advance / model-derives".
