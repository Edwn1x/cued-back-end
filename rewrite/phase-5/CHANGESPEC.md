# Phase 5 — CHANGE SPEC (nightly consolidation + episodic digest)

Numbered *what's-there-now / what-it-becomes / why / where*. Two new flag-gated
modules (default off); nothing legacy removed. These are the first writers to memory
NOT triggered by a user turn, so the design is guardrails against silent drift.

### 1. Nightly consolidation engine

- **Now:** `user_profile_memory` is only ever written on a user turn (extraction /
  `remember`). Stale facts, near-dupes, and stale-numeric contradictions accumulate;
  the dedup/supersession primitives in `memory.py` run only at write time, per fact.
- **Becomes:** `consolidation.py`. `consolidate_user(id)` builds a **copy-on-write**
  candidate (deepcopy — live memory never mutated mid-run), runs three deterministic
  passes reusing the Phase-1 primitives — `_close_stale` (non-safety, `uses==0`, older
  than `CONSOLIDATION_STALE_DAYS`), `_merge_near_dupes` (`_find_duplicate` → keep
  higher-uses/newer), `_collapse_contradictions` (`_find_supersession_target` → keep
  newest) — then writes the candidate atomically **only** if it passes every guardrail.
  `consolidate_all()` sweeps active users nightly.
- **Why:** memory that maintains itself, without a turn to trigger it — the roadmap's
  self-consolidating profile.
- **Where:** `consolidation.py` (new), reusing `memory.py`.

### 2. Hard invariants (in code, not prompt)

- **Now:** the only protection on memory writes is the per-fact dedup ladder + the
  safety-invalidation trigger guard.
- **Becomes:** four guarantees enforced in `consolidate_user`:
  **(a)** active `safety:true` entries are skipped by every pass (+ a belt-and-suspenders
  assert that no safety id left the live set); **(b)** **bounded delta** — a run removing
  more than `CONSOLIDATION_MAX_DELTA_FRACTION` of valid entries **ABORTS** (candidate
  discarded, live memory untouched, WARNING alert); **(c)** full JSON **diff** persisted;
  **(d)** the pre-run profile snapshot persisted for **rollback**.
- **Why:** a non-turn writer is exactly where a bad write compounds silently across
  nights — a runaway pass must be structurally unable to land.
- **Where:** `consolidation.py`, `models.ConsolidationRun` (diff + rollback home).

### 3. Human-readable per-user summary (founder directive)

- **Now:** n/a.
- **Becomes:** every run that changes something emits ONE human-readable line —
  `closed: shoulder constraint, stale 24d; merged: 2 protein-target near-dupes` —
  logged at INFO and stored on `ConsolidationRun.summary`. No-op nights stay silent.
- **Why:** the founder's daily two-second beta sanity check that memory is improving,
  not drifting — the difference between auditable-in-principle and actually-audited.
- **Where:** `consolidation._build_summary`, `ConsolidationRun.summary`.

### 4. Episodic digest

- **Now:** the watermark summarizer (`coaching_summary` / `last_compressed_message_id`)
  captures **coaching decisions / fitness progress**. Nothing captures the non-fitness
  life context a good coach follows up on ("how'd the midterm go").
- **Becomes:** `episodic.py`. A sweep digests a user whose conversation has gone quiet
  (`EPISODIC_QUIET_MINUTES`) and has enough un-digested messages — a cheap Haiku pass
  writes a short dated prose note of the **non-obvious, especially non-fitness**
  substance. Scoped OFF coaching ground in the prompt (no double-cover). Stored in
  `EpisodicDigest` (its own table, soft-deletable) — **never** in `user_profile_memory`,
  so consolidation can't dedupe session digests. A per-user watermark
  (`User.last_episodic_message_id`, independent of the summary watermark) makes it
  **idempotent** — the same messages are never digested twice.
- **Why:** the heartbeat's raw material for personal follow-ups; the roadmap's episodic
  digest, kept distinct from the summarizer.
- **Where:** `episodic.py` (new), `models.EpisodicDigest`, `User.last_episodic_message_id`.

### 5. Context wiring + scheduler

- **Now:** `build_loop_context` renders memory/events/summary; `start_scheduler` runs
  cron touchpoints + heartbeat.
- **Becomes:** `build_loop_context` gains a flag-gated `## RECENT LIFE CONTEXT` block
  from `recent_episodic(days)` — read by BOTH the reactive loop and the heartbeat.
  `start_scheduler` adds a nightly `CronTrigger` (`CONSOLIDATION_HOUR`, Pacific) for
  `consolidate_all` and an `IntervalTrigger` (`EPISODIC_SWEEP_MINUTES`) for `digest_all`.
- **Where:** `agent_loop.build_loop_context`, `scheduler.start_scheduler`.

### 6. Config + schema

`config.py`: `CONSOLIDATION_ENABLED` (off), `CONSOLIDATION_STALE_DAYS` (30),
`CONSOLIDATION_MAX_DELTA_FRACTION` (0.5), `CONSOLIDATION_HOUR` (4),
`CONSOLIDATION_MODEL` (Haiku), `EPISODIC_ENABLED` (off), `EPISODIC_QUIET_MINUTES` (90),
`EPISODIC_SWEEP_MINUTES` (30), `EPISODIC_MODEL` (Haiku), `EPISODIC_RECENT_DAYS` (5),
`EPISODIC_MIN_MESSAGES` (4). `models.py` + `migrate.py`: `consolidation_runs`,
`episodic_digests` (+ indexes), `users.last_episodic_message_id` (idempotent DDL).

## Tests (tier-1 red-first → green)

`tests/tier1/test_consolidation.py` — messy profile → clean; summary names the changes;
safety never closed; bounded-delta aborts + leaves memory untouched; quiet night writes
no run; idempotent; rollback restores. `tests/tier1/test_episodic.py` — quiet convo
digested + watermark advances (idempotent); active convo not digested (no model call,
no watermark move); NONE advances watermark without writing; too-few skips;
`recent_episodic` surfaces into context. `test_migrations.py` — the two tables + column.
`tests/tier2/test_consolidation_digest_quality.py` — judged: digest captures life not
coaching. Result: **103 passed, 2 skipped, 0 xfails.**
