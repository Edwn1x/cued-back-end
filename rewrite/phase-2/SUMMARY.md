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

## Tier-2 results (live, `tests/tier2/test_voice_and_cost.py --run-tier2`)
Ran the real loop (Sonnet 5 + `voice.md` + unified context) on 3 scenarios:
1. **Voice validated** — one consistent lowercase-peer voice, precise numbers, the
   shoulder injury respected from unified context ("skip overhead press… landmine
   press instead"), and warm accountability on a skipped-twice message. No emoji /
   no hype (asserted).
2. **Measured per-message cost: `$0.0072` standard / `$0.0048` intro.** **Phase 4's
   heartbeat cost model consumes the standard number ($0.0072/decision).** Worst-case
   sketch (pre-gate): 20 ticks/user/day × $0.0072 × 50 users ≈ **$7.2/day (~$216/mo)**
   before the Phase-4 rules/Haiku pre-gate cuts obvious-silence ticks.
3. **Prompt caching confirmed** — `cache_read_input_tokens=6866` across the run (stable
   voice prefix cached and re-read).
4. Live-response parsing on SDK 0.116 works (also closes the SDK-bump residual).

**Remaining before the flag flips:** founder review of `voice.md`, then flip
`SINGLE_AGENT_LOOP_ENABLED` (a logged event) and burn-in on the founder's number
before beta; legacy retires in Phase 6.

## Parked (unchanged)
- Rotate the Railway Postgres password.
- The meal-dup source fix (per-turn extraction, no idempotency vs today's log) is a
  Phase 3 item — recorded in memory, not this phase.
