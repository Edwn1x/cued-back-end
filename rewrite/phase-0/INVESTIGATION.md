# Phase 0 — INVESTIGATION

**Goal of Phase 0:** build the two-tier eval harness *before* any production change, and
encode the six roadmap failures (+ correction round-trip + honesty) as tests that fail
against the current system for the expected reasons.

This document is the ground-truth record of what the code actually does today, what
surprised me, where the spec and the code disagree, and the harness-design decisions those
findings force. Written for asynchronous review — every judgment call below is meant to be
findable and reversible.

Method: read the code end-to-end along three real execution paths (inbound SMS → reply;
scheduled message → send; fact → stored memory → injected prompt), probe the environment
directly rather than assume, and verify external facts against live docs where they gate a
Phase-0 decision (deferring API-surface verification to the phase that consumes it).

---

## 1. Environment (probed, not assumed)

| Thing | Finding | Consequence |
|---|---|---|
| Python | 3.13.2; project `.venv` created, pinned deps installed | harness runs here |
| `anthropic` SDK | pinned **0.42.0**; latest on PyPI **0.116.0** | SDK bump is its own early commit (task #5), *after* the harness exists as a net |
| Web egress | works (docs.anthropic.com, pypi reachable) | I verify external facts against live docs per spec |
| Docker | **absent** | can't use a container for the test DB |
| Postgres | 14 was installed; **prod is 18.4** (queried live via `SELECT version()`); installed `postgresql@18` locally → **local 18.4 == prod 18.4** | tier-1 runs on a disposable native PG18 cluster; no silent JSON/lock divergence |
| `ANTHROPIC_API_KEY` | **not in env yet** | tier-1 is fully mocked and does not need it; tier-2 blocks on it. Founder to provide via `.env`/Conductor env |
| `twilio==9.4.0` | **yanked** on PyPI (installs with warning) | noted; not touched this phase |

**Prod DB caveat:** the connection string was shared in chat for the one-time version query.
Read-only use only; **it must be rotated** and future prod creds should arrive via env, not chat.
No writes/migrations ever run against prod — migration tests use a scrubbed *export* of the
founder's row (per spec), not a live connection.

---

## 2. Trace A — inbound SMS → reply (`app.py` `/webhook`, lines ~963–1201)

The webhook does a **rich synchronous pass with four early-return branches that never reach
the buffer**, then hands off to an async buffer. This is the single most important harness
finding: entering the pipeline at `process_buffered_message` (the post-buffer callback) would
**bypass the entire synchronous pass** — including the safety floor and every intercept — and
green-light safety-floor and nudge tests while exercising a different path than production.

Exact production order:

| # | Step | Location | Notes |
|---|------|----------|-------|
| 1 | Parse form; if `NumMedia>0`, **download MMS media inline** (Twilio basic-auth) → base64 `image_data` | 969–994 | the real Twilio media path (Phase 3 `read_image`) |
| 2 | User lookup by phone; unknown → **early return** | 1000–1007 | |
| 3 | Clear `quiet_until` if passed or user is texting | 1011–1015 | |
| 4 | `log_incoming(user.id, body)` | 1018 | inbound row written |
| 5 | **`apply_safety_signals_task(user.id, body)`** — regex safety pre-pass, **no LLM** | 1029 (`memory.py:762`) | ← safety-floor entry; Phase 1 sync event detection hooks adjacent |
| 6–8 | `reset_unanswered`, `maybe_update_style`, `resolve_pending_clarification` | 1032–1038 | |
| 9 | **Ack-suppression** (onboarded + pure ack + no open `?`) → cancel buffer, **early return** | 1047–1051 | |
| 10 | **`classify_message(body, has_image=...)`** — **LLM call, inside the sync path** | 1054 | tier-1 must stub even "layer A" |
| 11 | **workout_log_start** intercept → create in-progress `Workout`, session_state, send, **early return** | 1058–1071 | |
| 12 | `_pb_in_mode` = already in logging mode? | 1078–1079 | |
| 13 | workout_log / workout_request / freeform training-day confirm → write `Workout`, `confirm_workout_today`, `set_session_state("at_gym")`, spawn daemon `maybe_infer_training_days` | 1082–1115 | **synchronous state writes**; Phase 1 split-pointer hooks here |
| 14 | **goodnight** intercept → set `quiet_until`, clear state, send, cancel buffer, **early return** | 1119–1150 | |
| 15 | **workout-logging-mode** intercept → `_handle_logging_mode_message`, **early return** | 1157–1159 | |
| 16 | Adaptive buffer delay (25–35s onboarding / 20–30s active / 90–150s cold) | 1162–1183 | |
| 17 | `buffer_message(..., process_callback=process_buffered_message)` | 1186–1194 | the **only** path that reaches the post-buffer stage |

### Post-buffer stage — `process_buffered_message` (`app.py:474–558`)

1. Onboarding routing if `onboarding_step < 3` → `handle_onboarding_reply`, return.
2. **`route_message(user, body, type, image_data)`** (orchestrator) → classifier → one of four
   specialists → personality merge → `response_text`. *This is the multi-agent core Phase 2 replaces.*
3. `send_sms(...)` the reply.
4. End-of-workout signal detection → `clear_session_state`.
5. **Five fire-and-forget daemon threads, spawned after the reply, never joined:**
   `extract_and_store_decisions`, `extract_and_store_memory` (the per-turn Haiku memory write),
   `extract_and_store_coaching_points_task`, `update_memory_uses_task`, `maybe_update_coaching_summary`.

**Harness consequence (critical):** memory is written on background threads with no join, so
"fact stated → recalled N turns later" (failure 1) is **inherently racy**. Tier-1 must make
these threads synchronous/deterministic (monkeypatch `threading.Thread` in test mode to run
inline, or a test hook that joins spawned workers) or the recall test flaps.

### Buffer flush seam (`message_buffer.py`)

In-memory `threading.Timer(delay, _flush_buffer, [phone, callback])`; `_flush_buffer` pops the
buffer, joins message bodies, calls `process_callback(user_id, combined_body, type, image_url)`.
The callback is bound into the Timer args, **not** stored in the buffer dict.

**Harness decision — two-layer replay driver:**
- **Layer A:** drive the real Flask `/webhook` route via Flask's **test client** with Twilio-shaped
  form fields (`From`, `Body`, `NumMedia`, `MediaUrl0`) → exercises the true ordering + all four
  early returns + the synchronous `classify_message` LLM call (stubbed).
- **Buffer:** monkeypatch `threading.Timer` (as imported in `message_buffer`) with a controllable
  fake so the test fires the flush deterministically — the *same* `_flush_buffer` → `process_buffered_message`
  code runs, with zero real delay and no real threads.
- **Threads:** make `threading.Thread` synchronous/capturable in tier-1 so the post-reply memory
  writes complete before the next turn's assertions.

---

## 3. Trace B — scheduled message → send (`scheduler.py`)

- `BackgroundScheduler` (APScheduler); per-user cron jobs registered by `schedule_user` (271),
  global jobs by `start_scheduler` (465).
- The send path is **`send_scheduled_message(user_id, message_type)`** (141), gated by:
  - `_is_training_day(user)` (100)
  - `has_unanswered_outbound(user_id)` (56) — engagement/decay gate
  - `get_session_state(user_id)` (read at 176) — e.g. `at_gym`, `workout_logging`
- **Failure-3 (nudge blindness) today:** "I already went" / "I'm in class" writes only
  `session_state` (set synchronously in webhook step 13) — there is **no Event table**, and the
  scheduler gates don't consult a conversational "already went today" signal beyond session_state,
  which is transient and cleared on workflow transitions. The failure-3 test asserts the *desired*
  suppression (Phase 1 Event-driven) and is expected to **fail red** now.

**Harness consequence:** failure-3 tests drive `send_scheduled_message` + gate functions directly
with a fixture whose inbound history contains the "already went"/"in class" message; assert the
gate suppresses (currently: does not → red).

---

## 4. Trace C — fact → stored memory → injected prompt (`memory.py`)

- **Store shape:** categorized JSON on `User.user_profile_memory`, gated by
  `USER_PROFILE_MEMORY_ENABLED` (else legacy blob via `_legacy_blob_or_empty`, 198).
- **Write path:** `apply_facts(profile, facts, user_id=)` (444) — dedup via `_find_duplicate`
  (jaccard + numeric tokens, 326), caps/eviction via `_enforce_caps`/`_evict_one` (383/413).
  `_evict_one` **never evicts `safety:true`** (invariant already partially honored in code).
  Update-matching is byte-exact-ish today — **this is failure 5b's root** (contradictions coexist).
- **Safety floor render:** `render_categories(..., include_safety_universal=True)` (549) appends
  **all** `safety:true` entries for **every** agent regardless of the per-agent slice map. This is
  the "nutrition-scoped allergy" safety behavior the spec warns must survive the Phase-2 unified
  render — confirmed to live here, not in the slice map.
- **Per-agent injection map:** `build_memory_block_with_ids(user, agent_type)` (224) slices
  categories per agent (`nutrition`/`training`/`readiness`/`coach`). This is the "per-agent memory
  injection map" the ledger deletes in Phase 6.
- **Write discipline:** `with_for_update()` + `flag_modified(user, "user_profile_memory")` used
  throughout (`memory.py:786/832/993`, `app.py:314/325`). **These are exactly the semantics SQLite
  silently no-ops** — the reason tier-1 must run on Postgres.

**Harness consequence:** failure-1 (cross-turn recall) and failure-5b (numeric contradiction)
tests assert on `user_profile_memory` state after a scripted sequence, with the async extraction
threads synchronized (§2). Failure-5a (injury heals) has no invalidation path today → red until Phase 1.

---

## 5. Anthropic client instantiation & stub strategy

- The client is a **module-global built at import time** — `client = anthropic.Anthropic(api_key=...)`
  — in `coach.py`, `orchestrator.py`, `memory.py`, `onboarding_agent.py`, `agents/*` (readiness,
  nutrition, training, personality, meal_extractor, weight_extractor), plus **function-local**
  instantiations in `app.py` (63/214/415). ~20 `messages.create` call sites total. No shared factory.
- Two implications:
  1. Importing these modules constructs a client at import time, so tests need a **dummy
     `ANTHROPIC_API_KEY`** present (conftest sets it) or import fails.
  2. Chasing N module globals for mocking is brittle. **Decision: patch the SDK method centrally**
     — `anthropic.resources.messages.Messages.create` (verify exact path in installed 0.42) — so a
     single patch covers every client instance regardless of how many modules hold one.
- **Tier-2 unpatch:** the central stub is installed by an **autouse fixture disabled under
  `@pytest.mark.tier2`**, so tier-2 runs the same harness end-to-end with the real SDK + real key,
  routing every metered call through `cost_tracking`.

---

## 6. Existing test / fixture / DB infrastructure

- **None.** Zero test files (`grep` for `def test_`/`pytest`/`unittest` → nothing). `models.py`
  builds a module-global `engine = create_engine(config.DATABASE_URL)`, `Session = sessionmaker(...)`,
  `Base = declarative_base()`; `init_db()` calls `Base.metadata.create_all(engine)`; `get_session()`
  returns `Session()`. Engine binds to `DATABASE_URL` **at import**, so the test DB URL must be set
  in the environment **before** `models` is imported (conftest responsibility).
- `migrate.py` is a hand-rolled migration script (no Alembic).

---

## 7. External-fact verification status

| Fact | Needed by | Status |
|---|---|---|
| SQLAlchemy JSON needs `flag_modified` for nested mutation | invariant #2 test | **confirmed in-code** (documented at `memory.py:981`, `app.py:321`); matches current SQLAlchemy 2.0 behavior |
| `SELECT FOR UPDATE` is a no-op on SQLite | tier-1-on-Postgres decision | confirmed by code comments + drives PG18 decision |
| anthropic 0.42 mock surface (`Messages.create` path) | stub strategy | to confirm by inspecting the installed package (more reliable than docs) before scaffolding |
| Anthropic web-search tool / vision / prompt caching / current model strings + pricing | Phase 2/3, SDK bump | **deferred** to the phase that consumes it; resolving doc: https://docs.anthropic.com/en/api/ (verify live, not from memory) |
| Twilio MMS media params (`NumMedia`, `MediaUrlN`, content-type) | Phase 3 `read_image` | webhook already uses them; full verification deferred to Phase 3; resolving doc: https://www.twilio.com/docs/messaging |
| APScheduler cron behavior on Railway | Phase 4 heartbeat | deferred to Phase 4 |

---

## 8. Surprises / spec-vs-code notes

1. **`classify_message` is an LLM call in the "synchronous" webhook path** — the sync pass is not
   LLM-free. Only the *safety* pre-pass is regex-only. Tier-1 stubs the classifier too.
2. **Memory writes are post-reply background threads with no join** — biggest testability hazard;
   forces thread-synchronization in tier-1.
3. **Safety universality lives in `render_categories`, not the slice map** — good news for Phase 2:
   the unified render preserves the safety floor by keeping `include_safety_universal=True`.
4. **`_evict_one` already refuses to evict `safety:true`** — invariant #4 is partially satisfied
   in code today; the missing half is *invalidation/consolidation* (Phase 1/5).
5. **Four webhook early-returns** (ack, workout_log_start, goodnight, logging-mode) each bypass the
   buffer entirely — any behavior test for these must drive Layer A, not the callback.
6. **No webhook idempotency — likely root cause of the duplicate-meal bug (verified).** The webhook
   reads `From`/`Body`/`NumMedia`/`MediaUrl0` but **never `MessageSid`**; there is no dedup anywhere
   and no provider-sid column / unique constraint on `Message`. Because step 10 (`classify_message`)
   is a **live Anthropic call made synchronously before Twilio gets its HTTP response**, a slow model
   call can exceed Twilio's ~15s webhook timeout → Twilio **re-delivers the same message** → the entire
   synchronous pass **and** a fresh buffer cycle run twice → the meal (and any step-13 workout row) is
   written twice. This compounds with the separately-documented double-write (one photo triggering both
   the photo handler and a text-extraction path). The synchronous-LLM-in-webhook problem itself
   **dissolves in Phase 2** when the classifier dies, so no restructuring now — the fix is just
   `MessageSid` dedup. **Fix lands in Phase 1** (small deterministic idempotency guard); Phase 0 only
   **encodes the failing test**.

---

## 9. Harness design decisions (carried into scaffolding)

1. **pytest**, disposable **native PG18** cluster (`initdb` into a temp dir, `pg_ctl start`, create
   test DB, `create_all`, teardown), `DATABASE_URL` set before `models` import.
2. **Two-layer replay driver:** Flask test-client `/webhook` (Layer A) + deterministic buffer flush
   + synchronous threads (post-buffer stage).
3. **Central Anthropic stub** at `Messages.create`, autouse, disabled under `@pytest.mark.tier2`.
4. **Fixture-user loader** + scripted-sequence runner + state/output assertion helpers.
5. **CI:** GitHub Action running tier-1 on push with a Postgres 18 service container.
6. Tier-1 fully mocked/free; tier-2 live, metered through `cost_tracking`, gated on the API key.

### Eval-plan additions (beyond the 8 spec cases)

- **9. Webhook idempotency.** Replay the **same `MessageSid` twice** through Layer A → assert
  **exactly one** set of state writes (one inbound `Message`, one `Meal`/`Workout`, one safety
  extraction effect). Red now (no dedup); the deterministic guard lands in Phase 1. The driver
  therefore must send a `MessageSid` form field so this is expressible.
- **10. Four early-return branch fixtures.** Each early-return path (ack-suppress, workout_log_start,
  goodnight, logging-mode) is a route where a message **produces state changes but never reaches the
  agent** — historically where facts got lost (the goodnight-bypass dropping injury reports is
  documented in-code at `app.py:1020–1029`). One fixture per branch asserting the *intended*
  synchronous state writes still occur (esp. **safety extraction runs on a goodnight message that
  also reports an injury**) and nothing silently drops.

---

## 10. Open items / founder inputs (non-blocking)

- `ANTHROPIC_API_KEY` via `.env`/Conductor env — needed only at first tier-2 run.
- **Rotate the Railway Postgres password** shared in chat.
- Real non-food screenshots (Phase 3) and the founder's phone number (Phase 4) arrive when they
  arrive; provisional paths ship meanwhile.
