# Phase 1 — CHANGE SPEC (state-layer primitives)

Numbered *what's-there-now / what-it-becomes / why / where*. Implemented across
five commits (`e1a6629`, `58fef35`, `4ed891c`, `459a0d4`, `1192f57`); this is the
consolidated record.

### 1. Webhook idempotency
- **Now:** webhook reads `From/Body/NumMedia/MediaUrl0`, never `MessageSid`; no dedup.
- **Becomes:** `processed_messages` table (`UNIQUE(message_sid)`); claim at webhook
  top before any state write, release on unhandled exception; fail-open on missing
  sid / unexpected error. `WEBHOOK_DUPLICATE` / `WEBHOOK_DROPPED` logs.
- **Why:** an idempotency primitive; catches genuine duplicate deliveries. (Twilio
  does *not* auto-retry inbound webhooks by default — see the Twilio-correction
  commit; the duplicate-*meal* source fix is Phase 3's photo+text double-write.)
- **Where:** `models.py`, `migrate.py`, `app.py` webhook.

### 2. Validity windows + history + substring-match
- **Now:** entries `{id,text,ts,uses,[safety]}`; updates byte-exact; contradictions
  coexist; injuries immortal.
- **Becomes:** `invalidate_entry` moves an entry to a `__history__` bucket (exempt
  from the size budget) with `invalidated_at/by/trigger`; absence == valid (no
  backfill). Update matching -> distinctive-substring requiring a unique non-safety
  match; add path supersedes numeric-divergent near-matches (5b). **Safety guard:**
  a `safety:true` close requires a recorded `trigger` or is rejected + logged.
- **Why:** facts change state instead of contradicting; safety closes are auditable.
- **Where:** `memory.py`.

### 3. Event table + synchronous detection
- **Now:** "already went"/"in class" write only transient `session_state`, which no
  scheduler gate reads for suppression; nudges fire anyway (failure 3).
- **Becomes:** append-only `events` table; a precision-biased regex floor detects
  `went_to_gym`/`in_class` synchronously on the inbound path (near-misses logged for
  the Phase 2 corpus); `todays_events` windows by the user's LOCAL day; scheduler
  gates suppress workout nudges on those events (`NUDGE_SUPPRESSED`).
- **Why:** the scheduler stops sending wrong nudges — the churn mechanism.
- **Where:** `events.py`, `models.py`, `migrate.py`, `app.py`, `scheduler.py`.

### 4. Split pointer
- **Now:** logged workouts store `workout_type="logged"` (no day); the model
  re-infers the split day each turn and drifts (failure 4).
- **Becomes:** `split_pointer_{day,at,source}` columns; `advance_split_pointer` is
  the only writer (named -> confirmed; unnamed -> inferred next day, once/local-day;
  never guesses). Cycle map is write-time only — no read-time day computation.
- **Why:** one durable day the Phase 2 model reads instead of re-deriving.
- **Where:** `split_pointer.py`, `models.py`, `migrate.py`, `events.py`.

### 5. Testable migrations
- **Now:** `migrate.py` executes at import (not importable/testable).
- **Becomes:** `run_migrations()` guarded under `__main__`; a migration test asserts
  idempotency + new tables + legacy-profile-renders-as-valid.
- **Where:** `migrate.py`, `tests/tier1/test_migrations.py`.
