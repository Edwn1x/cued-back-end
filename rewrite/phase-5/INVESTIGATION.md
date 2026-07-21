# Phase 5 — INVESTIGATION (nightly consolidation + episodic digest)

**Goal:** the memory layer maintains itself. A nightly batch closes stale/superseded
facts, merges near-duplicates, collapses contradictions toward recency — on a
copy-on-write candidate, with hard invariants in code. Separately, when a conversation
goes quiet, a cheap pass writes a short dated episodic entry (non-fitness life context)
— the heartbeat's raw material for personal follow-ups.

These are the **first writers to memory not triggered by a user turn** — so their diffs
are the first place a bad write can compound silently across nights. That shapes every
guardrail below, and one artifact requirement (the human-readable summary).

## What exists today (the ground we build on / must not double-cover)

- **`user_profile_memory`** (JSON on `User`) — `{category: [{id, text, ts, uses, safety?}]}`
  across 6 `CATEGORIES` + `HISTORY_KEY`. `memory.py` already has the exact primitives
  consolidation needs, as **pure functions**:
  - `_find_duplicate` (token-Jaccard ≥ 0.75, numeric-divergence guard) — near-dupe merge.
  - `_find_supersession_target` (same non-numeric core, divergent numbers, unique) —
    contradiction-by-recency.
  - `invalidate_entry(profile, id, by, trigger)` — closes a window (moves to HISTORY_KEY);
    **safety guard**: a `safety:true` entry only closes WITH a trigger, else rejected+logged.
  - `_new_entry`, `render_categories`, cap enforcement.
  Consolidation is mostly *scheduling* these primitives over a candidate profile — not
  new memory logic. Reuse, don't reinvent.
- **`invalidate_entry` already logs safety closures at WARNING** with the comment
  "so nightly consolidation can surface it" — Phase 1 anticipated this job.
- **Watermark summarizer (Phase B)** — `coaching_summary` + `last_compressed_message_id`
  compress old messages into a rolling summary of **coaching decisions / fitness progress**.
  `build_loop_context` renders it (§5) and windows raw history AFTER the watermark (§6).
  **The episodic digest must NOT re-cover this ground:** coaching_summary owns fitness
  coaching; the digest owns **non-fitness life context + non-obvious session substance**
  ("orgo midterm tomorrow", "moving apartments"). Different content, separate watermark.
- **`Event`** — structured, nudge-critical episodic detections (went_to_gym / in_class /
  life) written by a regex floor, read today-only via `todays_events`. The digest is
  *prose*, multi-day, model-written — a different thing; it does not replace Event.
- **Soft-delete chokepoint** `models.active` + `deleted_at` (Phase 3) — the pattern the
  new episodic table follows.

## Decisions

### Consolidation (`consolidation.py`, flag `CONSOLIDATION_ENABLED`)
`consolidate_user(user_id)`:
1. **Copy-on-write.** `candidate = deepcopy(user_profile_memory)`; live memory is never
   mutated mid-computation. Count `valid_before` (non-history entries).
2. **Structural passes on the candidate (pure Python, deterministic — no model):**
   - `_close_stale` — non-safety entry, age > `CONSOLIDATION_STALE_DAYS`, `uses == 0`
     (never referenced) → `invalidate_entry(by="consolidation:stale")`. Records
     `closed: <short text>, stale <N>d`.
   - `_merge_near_dupes` — within category, `_find_duplicate` ladder collapses to the
     entry with more `uses` (tie → newer `ts`); loser invalidated. Records
     `merged: N <category> near-dupes`.
   - `_collapse_contradictions` — `_find_supersession_target`: keep newest, invalidate
     the stale numeric value. Records `superseded: <text>`.
3. **Hard invariants, in code (not prompt):**
   - **Active safety entries are untouched** — every pass skips `safety:true`; asserted.
   - **Bounded delta** — if `valid_before > 0` and `removed / valid_before >
     CONSOLIDATION_MAX_DELTA_FRACTION` → **ABORT**: candidate discarded, live memory
     unchanged, WARNING alert logged, run recorded `aborted=True`. A runaway pass can
     never land.
   - **Full diff logged** — JSON before/after per touched entry, on the run row.
   - **Rollback retained** — the pre-run profile is snapshotted on the run row.
4. **Human-readable summary (founder directive).** One line per user, emitted only when
   something changed: `closed: shoulder constraint, stale 24d; merged: 2 protein-target
   near-dupes`. Logged at INFO + stored on the run row — the founder's daily 2-second
   beta sanity check that memory is improving, not drifting. No-op nights stay quiet.
5. **Coaching-summary refresh** is a *separate, optional* model step AFTER the structural
   pass (kept out of the hard-invariant core so the core stays deterministic + tier-1
   testable). Tier-2 grades its quality; recall eval must not regress.

Storage: **`ConsolidationRun`** (user_id, ran_at, valid_before, removed_count, aborted,
summary, diff JSON, prev_profile JSON) — diff + human summary + rollback in one place.
`rollback(user_id, run_id)` restores `prev_profile`.

### Episodic digest (`episodic.py`, flag `EPISODIC_ENABLED`)
- **Trigger — "conversation goes quiet."** A sweep (interval job): a user with messages
  `id > last_episodic_message_id` **and** last message older than
  `EPISODIC_QUIET_MINUTES` → run the cheap pass; else skip. Deterministic + testable,
  and the watermark makes it **idempotent** (never re-digests the same messages) — the
  non-turn-writer safety property.
- **Cheap model pass** (`EPISODIC_MODEL`, Haiku-class) over the un-digested window →
  a short dated prose entry of the **non-obvious, especially non-fitness** substance.
  Prompt explicitly scoped OFF coaching decisions (that's the watermark summarizer's job).
- **Storage — `EpisodicDigest`** (user_id, occurred_on, text, created_at, deleted_at;
  soft-deletable via `models.active`). Kept **out of `user_profile_memory`** so
  consolidation never dedupes/closes session digests. New watermark column
  `User.last_episodic_message_id` (mirrors `last_compressed_message_id`, independent).
- **Reader** `recent_episodic(user_id, days=EPISODIC_RECENT_DAYS)` surfaced in
  `build_loop_context` so BOTH the reactive loop and the heartbeat see it — the
  follow-up material ("how'd the midterm go").

### Scheduler
Nightly `CronTrigger` (off-peak, single-tz base) for `consolidate_all`; interval sweep
for `digest_all`. Both flag-gated, default off.

## Config (config.py, default off/safe)
`CONSOLIDATION_ENABLED`, `CONSOLIDATION_STALE_DAYS` (30),
`CONSOLIDATION_MAX_DELTA_FRACTION` (0.5), `CONSOLIDATION_HOUR` (4, Pacific),
`CONSOLIDATION_MODEL` (Haiku-class), `EPISODIC_ENABLED`, `EPISODIC_QUIET_MINUTES` (90),
`EPISODIC_SWEEP_MINUTES` (30), `EPISODIC_MODEL` (Haiku-class), `EPISODIC_RECENT_DAYS` (5).

## Tests
- **Tier-1 (deterministic core):** a seeded fixture profile full of contradictions,
  staleness, and near-duplicates comes out **clean**; safety entries survive every pass;
  bounded-delta **aborts** a runaway run (live memory unchanged); the human-readable
  summary names the closes/merges; re-running is idempotent; a no-op night writes no
  summary; rollback restores. Episodic: the sweep respects the watermark (idempotent),
  skips a still-active conversation, and `recent_episodic` surfaces into context.
- **Tier-2 (judged):** coaching-summary refresh + episodic prose quality; recall eval
  must not regress.
