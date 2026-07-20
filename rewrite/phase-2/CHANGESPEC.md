# Phase 2 — CHANGE SPEC (single agent loop, inbound only)

Numbered *what's-there-now / what-it-becomes / why / where*. All new behavior is
behind `SINGLE_AGENT_LOOP_ENABLED` (default off); the legacy pipeline is untouched
and still serves prod.

### 1. Inbound routing
- **Now:** `process_buffered_message` → `orchestrator.route_message` → classifier
  (Haiku) → one of four specialists → personality merge / `coach.get_coach_response`.
- **Becomes:** if the flag is on, one `agent_loop.run_agent_loop` call; on ANY
  exception, fall back to `route_message` and log `AGENT_LOOP_FALLBACK` at ERROR.
- **Why:** one voice, full context; fewer agents, more context (fixes 1, 4, 6). The
  fallback keeps invariant #5 (no user-visible regression) for the first flag in
  front of every inbound.
- **Where:** `app.py` (`process_buffered_message`), `config.py`.

### 2. Unified context
- **Now:** each agent gets a per-agent memory slice (`build_memory_block(user, agent)`);
  cross-domain facts drop (failure 1).
- **Becomes:** `agent_loop.build_loop_context` renders ALL categories
  (`render_categories(CATEGORIES, include_safety_universal=True)`) + typed profile +
  today's events + split pointer WITH provenance + coaching summary + delivered points
  + the watermark conversation window + a known-gaps line (≤1 follow-up permitted).
- **Why:** the single loop sees every domain's facts; safety stays universal.
- **Where:** `agent_loop.py`.

### 3. Voice
- **Now:** two competing prompts — `system_prompt.txt` (JARVIS) and the personality
  skill (peer) — producing a voice that fractures by routing path (failure 6).
- **Becomes:** one merged `prompts/voice.md` (lowercase-peer default + shared
  discipline + precise-numbers register + age-tier/mirroring), used as the loop's
  system prompt. Founder-reviewable; validated by the tier-2 voice eval.
- **Where:** `prompts/voice.md`, `agent_loop.py`.

### 4. Model + cost
- **Now:** model strings scattered/hardcoded (`claude-sonnet-4-6`).
- **Becomes:** `config.AGENT_LOOP_MODEL = "claude-sonnet-5"` (separate from legacy
  `COACH_MODEL`); loop passes no sampling params, adaptive thinking + `effort: low`
  held constant, `cache_control` on the stable voice prefix, every call through
  `cost_tracking`.
- **Where:** `config.py`, `agent_loop.py`.

### 5. Tests
- Flag-fallback (loop raises → legacy answers, logged, no gap) + flag-on happy path;
  failure-1 flipped to assert the unified loop context carries a cross-domain fact.
- **Where:** `tests/tier1/test_agent_loop_fallback.py`, `tests/tier1/test_acceptance.py`.

**Pending the funded key (not in this diff):** tier-2 voice eval, measured
per-message cost (both rates; Phase 4 consumes standard), cache-hit verification,
and the flag flip.
