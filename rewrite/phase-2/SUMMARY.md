# Phase 2 — SUMMARY (single agent loop, inbound only)

**Status:** tier-1 complete and green; flag **off** (legacy still serves prod).
Tier-2 voice eval + measured cost are gated on the funded API key (task #15) — the
PR ships the safe, flag-gated diff and completes those before the flag flips.

## Success criteria (roadmap Phase 2)
- ✅ All inbound (behind the flag) flows through one call — no classifier/merge.
- ✅ Symptom-1 eval passes (unified render carries cross-domain facts).
- ⏳ Symptom-4 (named split day identical across two asks) — tier-2, checkable now
  that the pointer feeds context; runs with the key.
- ⏳ Voice is one consistent persona — tier-2 voice eval, runs with the key.
- ⏳ Per-message cost measured and within budget — runs with the key.

## Result
```
tests/tier1: 58 passed, 2 skipped, 1 xfailed   (only F5a remains → Phase 3)
```
Ratchet: **F1 flipped** to a passing test. F5a is the last xfail across the whole
suite and honestly lands in Phase 3 (heal-detection trigger for remember-invalidate).

## Non-negotiable invariants held
- **Fallback (#5):** loop failure → legacy answers → `AGENT_LOOP_FALLBACK` logged →
  no gap. Tier-1 test asserts all three. Built as a real fallback, not hope.
- Safety floor still deterministic and universal in the unified render; state writes
  code-mediated; guardrails unchanged. Flag flip is a deliberate, logged event.

## ⚠️ Deploy posture
- **No prod behavior change on merge** — `SINGLE_AGENT_LOOP_ENABLED` defaults off, so
  merging is inert for real users. The loop turns on only when the flag is set,
  AFTER the tier-2 voice eval + cost measurement pass.
- No migration needed (no schema change this phase).

## Gated on the funded `ANTHROPIC_API_KEY` (task #15)
1. **Tier-2 voice eval** validates `prompts/voice.md` (one persona + warm accountability).
2. **Measured per-message cost**, recorded at BOTH intro ($2/$10) and standard ($3/$15);
   **Phase 4's heartbeat cost model consumes the standard number.**
3. **Prompt-cache verification** — `usage.cache_read_input_tokens > 0` across turns.
4. Only then: flip the flag (a logged event) and, after burn-in on the founder's
   number, begin retiring legacy paths (Phase 6).

## Parked (unchanged)
- Rotate the Railway Postgres password.
- The meal-dup source fix (per-turn extraction, no idempotency vs today's log) is a
  Phase 3 item — recorded in memory, not this phase.
