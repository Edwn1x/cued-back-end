# Phase 6 — CHANGE SPEC (deletion + launch)

Numbered *what's-there-now / what-it-becomes / why / where*. This PR is the
**evidence-independent** half of Phase 6 — nothing legacy is deleted here; the
deletions are prepared commits that land when their live gates go green
(see INVESTIGATION). One small refactor is included because it makes the deletion
provably safe.

### 1. Deletion inventory (INVESTIGATION.md)
- **Now:** the legacy pipeline's boundaries were documented per-phase but never
  inventoried as a deletion set.
- **Becomes:** a verified inventory — doomed set (`orchestrator`, `coach`, `agents/`,
  `skill_loader`, `tone_analyzer`), retained set (`macro_calculator` [onboarding],
  `scheduler` module [helpers survive], all new-path modules), the two evidence gates,
  and the order of the prepared deletion commits.
- **Where:** `rewrite/phase-6/INVESTIGATION.md`.

### 2. New-path isolation test (the safety proof)
- **Now:** "the single agent doesn't need the legacy pipeline" was true but unproven,
  and in fact NOT clean — `heartbeat → scheduler → coach` via a top-level import.
- **Becomes:** `tests/tier1/test_new_path_isolation.py` — an AST transitive
  import-closure check proving the brain (`agent_loop` + `agent_tools` + `heartbeat` +
  `consolidation` + `episodic`) never reaches the doomed set (catches lazy in-function
  imports too). Passes today; stays true after deletion; the pipeline commit extends it
  to assert the modules are gone.
- **Why:** deletion is safe exactly while this is green — it's removing dead weight the
  brain already doesn't touch, not surgery.
- **Where:** `tests/tier1/test_new_path_isolation.py`.

### 3. Relocate `has_unanswered_outbound` (the refactor the isolation test forced)
- **Now:** `scheduler.has_unanswered_outbound` — but `scheduler` top-level-imports
  `coach.generate_scheduled_message`, so the heartbeat's use of it dragged `coach`
  (and `skill_loader`, `tone_analyzer`) into the brain's closure.
- **Becomes:** moved to `engagement_tracker.py` (a coach-free outbound-gating home);
  `scheduler` re-exports it for its own callers; the heartbeat imports it from there.
- **Why:** makes the isolation genuinely clean NOW, so the deletion story isn't blocked
  on the Gate A commit. Behavior identical; the heartbeat's `unanswered_gap` guardrail
  test now patches the real home.
- **Where:** `engagement_tracker.py`, `scheduler.py`, `heartbeat.py`, `tests/tier1/test_heartbeat.py`.

### 4. Extraction→remember parity eval (Gate B's automated verdict)
- **Now:** `remember` runs in parallel with legacy extraction, but nothing measures when
  extraction can safely retire.
- **Becomes:** `tests/tier2/test_extraction_parity.py` — replays a conversation dataset
  two ways (extraction-only vs remember-only) into fresh users and scores model-judged
  recall of stated facts. Passes when remember recall ≥ extraction recall − ε. **Skips
  with a clear reason until `CUED_PARITY_DATASET` exists**, so it fires automatically the
  moment live parallel writes are exported. Calls extraction directly → must run before
  the Gate B deletion commit (exactly when its verdict is needed).
- **Where:** `tests/tier2/test_extraction_parity.py`.

### 5. README rewrite
- **Now:** describes the old classify→generate + templated-scheduler architecture and a
  `coach.py` monolith.
- **Becomes:** the single-agent system — one agent / voice / memory, tools, heartbeat,
  deterministic state — plus the flag rollout order, the two test tiers (disposable PG18),
  and the legacy-retirement note.
- **Where:** `README.md`.

## Prepared deletion commits (staged separately; land when a gate goes green)
Not in this PR. Order + contents in INVESTIGATION §"Prepared deletion commits":
Gate A (templated scheduler touchpoints + `coach.generate_scheduled_message`),
Gate B (per-turn LLM extraction; retain the deterministic safety floor), pipeline
removal (`orchestrator`/`agents/`/`coach`/`skill_loader`/`tone_analyzer` + app fallback).

## Tests
`tier1: 106 passed, 2 skipped, 0 xfails` (+3 isolation tests, heartbeat guardrail patch
updated). Parity eval skips cleanly until its dataset exists.
