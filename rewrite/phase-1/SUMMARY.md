# Phase 1 — SUMMARY (state-layer primitives)

**Status:** complete. Every failure Phase 1 owns is retired; the legacy pipeline
still runs. Suite green.

## Success criteria (roadmap Phase 1) — met
- ✅ "I'm in class" / "already went" writes an Event **synchronously** on the inbound floor.
- ✅ A logged / confirmed workout advances the split pointer (code-mediated).
- ✅ An invalidated fact leaves the render and enters history.
- ✅ eval tier-1 green.

## Result
```
tests/tier1: 55 passed, 2 skipped, 2 xfailed   (exit 0)
```
**Ratchet:** flipped **idempotency, 5b, 3** to passing tests (strict markers removed
in their fixing diffs). Remaining xfail: **F1** (Phase 2, unified render) and **F5a**
(mechanism landed here; heal *trigger* is Phase 3's remember-invalidate). `F4`/others
remain skip-until.

## Commits
`e1a6629` MessageSid idempotency · `58fef35` Twilio-semantics correction ·
`4ed891c` validity windows + substring-match · `459a0d4` Event table + sync
detection · `1192f57` split pointer.

## Non-negotiable invariants held
- Safety floor still deterministic (regex, no LLM); safety entries never evicted/
  deduped, and now close **only** via explicit invalidation **with a recorded
  trigger** (rejected + logged otherwise). State writes remain code-mediated and
  row-locked. Guardrails unchanged.

## ⚠️ DEPLOY NOTES (this PR changes prod behavior on merge)
Two changes are **not** feature-flagged, both by design:

1. **`MessageSid` dedup** (webhook). Dedup-off is meaningless, so no flag. Ships
   live to every inbound. Fail-open (a miss never drops a message). **Run
   `migrate.py` with the deploy** so `processed_messages` exists before the code
   references it.
2. **Scheduler event-gate** (nudge suppression). Suppression-only — it can only
   *withhold* a nudge, never send a wrong one — so no flag; safe direction. **Run
   `migrate.py`** so `events` + the split-pointer columns exist.

**Migration order:** `python migrate.py` **before/with** the code deploy. All
statements are idempotent.

**Post-deploy observability (Railway log grep, first few days):**
- `WEBHOOK_DUPLICATE` — genuine duplicate deliveries caught (quantifies the real rate).
- `WEBHOOK_DROPPED` — inbounds lost to a crash; should be ~0, each worth investigating.
- `NUDGE_SUPPRESSED` — event-driven suppressions (confirms the gate fires correctly).
- `EVENT_DETECTED` / `EVENT_NEAR_MISS` — floor hits + the recall corpus for Phase 2.
- `SPLIT_POINTER*` — pointer advances (confirmed vs inferred).
- `SAFETY_INVALIDATION` / `SAFETY_INVALIDATION_REJECTED` — safety-entry closures.

## Notes / parked
- No backfill needed: validity lives in the existing JSON (absence == valid);
  split-pointer/event columns default NULL.
- Migration test uses a synthetic legacy-shape fixture; the **scrubbed founder-row
  export** slots in as an extra fixture when provided (parked input).
- Still parked: **rotate the Railway Postgres password**; `ANTHROPIC_API_KEY` for
  the first tier-2 run.
