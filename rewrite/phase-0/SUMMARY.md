# Phase 0 — SUMMARY (eval harness)

**Status:** complete. Suite green, ratchet armed, CI wired, no production behavior
changed. Written for asynchronous review; companion docs: `INVESTIGATION.md`
(what the code actually does + the decisions), `CHANGESPEC.md` (what this phase
adds).

## Success criteria (from roadmap Phase 0) — met

- ✅ Two-tier harness built: tier-1 deterministic/mocked/free; tier-2 live/metered.
- ✅ The six observed failures encoded as explicit tests, each failing against the
  current system **for the documented reason** (or green where current behavior is
  already correct — see taxonomy below).
- ✅ Runner works against a test DB with mocked LLM in tier-1.
- ✅ CI runs tier-1 on push/PR, no secrets.

## Result

```
tests/tier1:  10 passed, 5 xfailed, 2 skipped     (exit 0)
tests/tier2:  5 skipped (excluded from CI; run with --run-tier2)
full run incl. cluster bring-up: ~1s
```

## Test taxonomy — three deliberate species

**Green today — regression net** (`tests/tier1/test_invariants.py`): must stay green
through every phase; a later phase that breaks one goes red.
- safety floor: allergy → `User.restrictions`; injury → `safety:true` constraint (regex, no LLM)
- safety-universal render: a `safety:true` constraint injects for every agent (incl. nutrition/coach whose slice maps exclude `constraints`)
- goodnight early-return: suppresses further outbound + sets `quiet_until`; still captures a safety fact (the documented pre-pass-at-top fix)

**xfail(strict) today — the ratchet** (`tests/tier1/test_acceptance.py`): broken now;
when a phase fixes one it XPASSes and **breaks the build**, forcing the marker's
removal into that phase's diff. All assert on deterministic state/render/gate, never
the mocked model's words.
- failure 1 — per-agent slice map drops cross-domain facts → Phase 2
- failure 3 — scheduler gates ignore "already went"/"in class" → Phase 1
- failure 5a — no heal-invalidation path → Phase 1/3
- failure 5b — numeric-divergent facts coexist → Phase 1
- webhook idempotency — no `MessageSid` dedup (likely duplicate-meal root cause) → Phase 1

**skip-until** — no system to test against yet, so xfail would be dishonest:
- tier-1: failure 4 (needs Phase 1 split pointer), correction-honesty (needs Phase 2 loop + Phase 3 `manage_log`)
- tier-2 (judged): failure 2 screenshot (Phase 3), failure 1/4 judged + failure 6 voice (Phase 2), full correction round-trip (Phase 3)

## Notable decisions (findable & reversible — full rationale in INVESTIGATION.md)

1. **Tier-1 on real Postgres 18.4** (matches prod exactly), not SQLite — else `FOR
   UPDATE` / `flag_modified` invariants are silently no-op'd. Disposable native
   cluster locally (`pg_ctl -l` to avoid the daemon-inherits-pipe hang); PG18
   service container in CI via `CUED_TEST_DATABASE_URL`.
2. **Two-layer replay driver**: real Flask `/webhook` test client (true ordering +
   four early returns + synchronous stubbed `classify_message`) → deterministic
   buffer flush → `process_buffered_message`. Founder's catch — entering at
   `process_buffered_message` would have bypassed the safety floor and every intercept.
3. **Central Anthropic stub** at `Messages.create` (client is an import-time
   module-global in ~10 modules, no factory), autouse, disabled under `@tier2`.
4. **Determinism shims**: post-reply daemon threads run synchronously; buffer
   `Timer` fires on command — kills the memory-write race.
5. **Assertions are deterministic** (state/render/gate) because tier-1 mocks the
   LLM; model-judgment versions live in tier-2. This is what makes an XPASS mean
   the behavior genuinely changed.

## Surfaced for later phases

- **Duplicate-meal root cause (verified):** no webhook `MessageSid` dedup + a
  synchronous LLM `classify_message` before Twilio's HTTP response → slow call
  exceeds the ~15s webhook timeout → Twilio re-delivers → double write. Fix =
  deterministic idempotency guard in **Phase 1** (test already red).
- SDK bump 0.42→current is the next isolated commit, now that the harness is a net.

## Parked on founder (non-blocking)

- **Rotate the Railway Postgres password** shared in chat.
- Provide `ANTHROPIC_API_KEY` (`.env`/Conductor env) before the first tier-2 run.
