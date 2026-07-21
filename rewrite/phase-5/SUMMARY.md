# Phase 5 — SUMMARY (nightly consolidation + episodic digest)

**Status:** built + green; flags default off. Memory now maintains itself — a nightly
batch closes stale/superseded facts, merges near-dupes, and collapses contradictions on
a copy-on-write candidate with hard invariants in code; and a quiet-conversation sweep
writes short dated life-context notes for the heartbeat to follow up on. These are the
**first writers to memory not triggered by a user turn**, so every knob is a guardrail
against silent cross-night drift.

## Success criteria (roadmap Phase 5)
- ✅ Candidate profile is **copy-on-write** — live memory never mutated mid-run.
- ✅ Closes validity windows on stale/superseded facts; merges near-dupes; collapses
   contradictions toward recency; (coaching-summary refresh is a tier-2 model step).
- ✅ Hard invariants **in code**: safety untouched; **bounded-delta abort**; full diff
   logged; previous profile retained for rollback.
- ✅ A seeded fixture full of contradictions/staleness/near-dupes comes out **clean**.
- ✅ Episodic digest on quiet — short dated non-fitness note; **does not double-cover**
   the watermark summarizer; idempotent via its own watermark.
- ⏳ Recall eval must not regress — asserted structurally in tier-1 (used facts survive);
   full recall parity is the Phase 6 gate before extraction retires.

## Result
```
tests/tier1: 103 passed, 2 skipped, 0 xfailed
```
12 new tier-1 tests + migration assertions. **Zero xfails** held.

## What shipped
| Piece | Flag / where | Notes |
|---|---|---|
| consolidation engine | `consolidation.py` | copy-on-write candidate; 3 deterministic passes reusing `memory.py` primitives |
| hard invariants | `consolidate_user` | safety-skip + assert, bounded-delta abort, diff, rollback snapshot |
| human-readable summary | `_build_summary`, `ConsolidationRun.summary` | one line/user on change; quiet nights silent |
| episodic digest | `episodic.py` | quiet-trigger sweep; Haiku pass; own table; watermark idempotency |
| context wiring | `build_loop_context` | flag-gated `## RECENT LIFE CONTEXT` — reactive loop + heartbeat |
| schema | `models.py`, `migrate.py` | `consolidation_runs`, `episodic_digests`, `users.last_episodic_message_id` |

## Design notes honored (founder directives)
- **Human-readable per-user summary on any window-close/merge** — not just a JSON diff.
  `closed: …; merged: …; superseded: …`, logged INFO + on the run row. The daily
  2-second beta audit that memory is improving rather than drifting. No-op nights quiet.
- **Bounded-delta abort** — a run removing > `CONSOLIDATION_MAX_DELTA_FRACTION` (50%) of
  valid entries discards the candidate, leaves live memory untouched, alerts. Proven in
  tier-1 (`test_bounded_delta_aborts_and_leaves_memory_untouched`).
- **Safety is sacred** — every pass skips `safety:true`; a belt-and-suspenders assert
  guards the swap. An ancient never-used safety fact survives (tier-1).
- **No double-cover** — the digest prompt is scoped OFF coaching decisions; storage is a
  separate table; a separate watermark. Tier-2 asserts the note captures life, not sets.
- **Idempotency for non-turn writers** — consolidation is idempotent (a settled profile
  stops changing); the digest watermark means the same messages never digest twice.

## Cost
- Consolidation core: **$0** (pure Python). Coaching-summary refresh (optional) + episodic
  digest run on **Haiku** (cheap): a digest pass is a short Haiku call (~120 out tokens).
- Guardrails bound blast radius, not spend; the digest sweep only calls the model when a
  conversation has actually gone quiet AND has ≥ `EPISODIC_MIN_MESSAGES` new lines.

## Handover checklist (founder actions)
1. Merge the Phase 3/4/5 PRs in order; retarget bases to `main` as each lands.
2. `python migrate.py` on prod (adds `consolidation_runs`, `episodic_digests`,
   `users.last_episodic_message_id`).
3. Flip `CONSOLIDATION_ENABLED` + `EPISODIC_ENABLED` (after the loop/heartbeat flags).
4. **Read the morning-after summaries** for the first week — that's the drift check.
   If one looks wrong, `consolidation.rollback(user_id, run_id)` restores that night.
5. Still parked: rotate the Railway Postgres password; real screenshots for read_image.

## Next (Phase 6)
Delete the legacy multi-agent pipeline + templated schedulers once the loop + tools +
heartbeat + consolidation have proven parity (incl. the recall eval); flip the final
flags; launch gate.
