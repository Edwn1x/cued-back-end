# Phase 0 — CHANGE SPEC (eval harness)

Format per working agreement: **what's-there-now / what-it-becomes / why / where**. Phase 0 adds
test/eval infrastructure and encodes the failing acceptance cases. **No production behavior changes**
land in Phase 0 — the one production-shaped fix this investigation surfaced (webhook `MessageSid`
idempotency) is only *encoded as a red test* here and *implemented in Phase 1*.

---

### 1. Test runner & layout
- **Now:** no tests, no pytest, no CI.
- **Becomes:** `pytest` with `tests/` split into `tests/tier1/` (deterministic, LLM mocked, run
  always) and `tests/tier2/` (live model, `@pytest.mark.tier2`, run before merges). `pytest.ini`
  registers the `tier2` marker and defaults to deselecting it unless `--tier2` is passed.
- **Why:** the roadmap gates every later phase on this harness; tiers separate free/fast from
  metered/slow.
- **Where:** new `tests/`, `pytest.ini`, `requirements-dev.txt` (pytest, pytest-timeout).

### 2. Disposable Postgres test DB
- **Now:** `models.py` binds `engine = create_engine(config.DATABASE_URL)` **at import**; default URL
  is SQLite; `FOR UPDATE`/`flag_modified` are no-ops on SQLite.
- **Becomes:** a session-scoped fixture spins up a **native PG18 cluster** (`initdb` → temp dir,
  `pg_ctl start` on an ephemeral port, `CREATE DATABASE`), sets `DATABASE_URL` **before** `models`
  is imported, runs `Base.metadata.create_all`, and tears the cluster down at session end. A
  function-scoped fixture truncates/rolls back between tests.
- **Why:** local PG18.4 == prod PG18.4, so the exact locking/JSON semantics the invariants protect
  are actually exercised.
- **Where:** `tests/conftest.py`, helper `tests/_pgcluster.py`.

### 3. Central Anthropic stub
- **Now:** `client = anthropic.Anthropic(...)` is a module-global built at import in ~10 modules
  (`coach`, `orchestrator`, `memory`, `onboarding_agent`, `agents/*`) + function-local in `app.py`.
  No factory.
- **Becomes:** an **autouse** fixture sets a dummy `ANTHROPIC_API_KEY` (so imports don't fail) and
  patches the SDK method (`anthropic.resources.messages.Messages.create`) centrally with a
  programmable fake (queue of scripted responses / default canned reply). Disabled under
  `@pytest.mark.tier2`, where the real SDK runs and every call is metered via `cost_tracking`.
- **Why:** one patch point covers every client instance; tier-2 reuses the identical harness live.
- **Where:** `tests/conftest.py`, `tests/_fake_anthropic.py`.

### 4. Deterministic threads & buffer timer
- **Now:** post-reply memory writes are fire-and-forget daemon `threading.Thread`s (no join);
  the buffer uses a real `threading.Timer`.
- **Becomes:** in test mode, `threading.Thread` (in `app`) and `threading.Timer` (in
  `message_buffer`) are monkeypatched to **synchronous/controllable** fakes: threads run inline;
  the timer stores `(fn, args)` and a driver helper `flush_buffer(phone)` fires it.
- **Why:** failure-1 recall and every state assertion are otherwise racy against background writes.
- **Where:** `tests/conftest.py`, `tests/driver.py`.

### 5. Two-layer replay driver
- **Now:** none.
- **Becomes:** `tests/driver.py` exposing `send(user, body, *, media=None, message_sid=...)` which
  POSTs Twilio-shaped form data (`From`, `Body`, `NumMedia`, `MediaUrl0`, **`MessageSid`**) to the
  real Flask `/webhook` test client (Layer A: full ordering + early returns + stubbed classifier),
  then synchronously flushes the buffer into `process_buffered_message` (Layer B). Returns the
  outbound reply(ies) and exposes helpers to read resulting state.
- **Why:** exercises the real production path end-to-end without real delays/threads; `MessageSid`
  makes the idempotency case expressible.
- **Where:** `tests/driver.py`.

### 6. Fixture-user loader
- **Now:** none; users are created ad hoc via onboarding.
- **Becomes:** `make_user(**overrides)` factory producing a post-onboarding `User`
  (`onboarding_step>=3`, timezone, wake_time, typed columns, optional seeded `user_profile_memory`),
  plus a loader for the scrubbed founder-row export used by migration tests (added when the export
  lands).
- **Why:** every acceptance case needs a deterministic starting user.
- **Where:** `tests/factories.py`.

### 7. The 10 acceptance/invariant cases (encoded red)
- **Now:** none.
- **Becomes:** tier-1 tests for the rule-based failures (1 recall, 3 nudge-suppression, 4 split-day,
  5a injury-heals, 5b numeric-contradiction, correction-round-trip honesty half, **9 idempotency**,
  **10 four early-return fixtures**) + tier-1 invariant tests (safety floor, code-mediated writes,
  guardrails-outside-model, safety-entry immortality, no-regression fallback, no-confabulated-completion).
  Tier-2 stubs for the judged/vision cases (2 screenshot, 6 voice, 7 full correction round-trip)
  that land with Phase 3. Each must fail against the current system **for the documented reason**.
- **Why:** the phase's success criterion.
- **Where:** `tests/tier1/`, `tests/tier2/`.

### 8. CI
- **Now:** none.
- **Becomes:** a GitHub Action running tier-1 on push/PR with a **Postgres 18 service container**.
- **Why:** an unrun suite decays before Phase 3.
- **Where:** `.github/workflows/tier1.yml`.

---

**Out of Phase 0 (recorded, not built here):** the `MessageSid` idempotency *fix* (Phase 1), any
agent/loop changes (Phase 2+), the SDK bump (its own early commit, after this harness exists).
