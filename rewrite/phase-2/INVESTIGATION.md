# Phase 2 — INVESTIGATION (single agent loop, inbound only)

**Goal:** one Sonnet call per inbound (behind a flag) with unified context + one
voice, replacing classifier→specialists→merge. Flips F1. Heartbeat stays OFF;
legacy cron still sends proactive messages.

## 1. Control flow today (what we replace)

`process_buffered_message` (app.py:474) → `orchestrator.route_message` (orchestrator.py:93):
- Fast-paths: receipt photo, `is_daily_log_query` (returns **directly**, no voice layer).
- Dining-mention override → force nutrition.
- Else `classify_message` (Haiku, orchestrator.py:23) → primary agent + confidence.
- Nutrition / training / readiness pipelines → specialist `handle()` produces a **structured**
  result → `agents.personality.write_response` (personality.py:36) renders it in the peer voice.
- Personality / fallback → `coach.get_coach_response` (coach.py:378) — the JARVIS monolith.
- Weight extraction runs on every path; **`extract_and_log_meal` is spawned per nutrition turn**
  (orchestrator.py:172) — see §3.

**Existing fallback pattern (the template for task #12):** every specialist pipeline is
`try: … except: logger.error("… falling back to legacy"); <fall through to get_coach_response>`.
Phase 2's flag wraps the new loop the same way: try the loop, on any exception fall back to
`route_message` (legacy) and log loudly. Legacy stays until Phase 6.

## 2. Voice — two competing sources + transcript evidence (failure 6)

- **`prompts/system_prompt.txt`** (used by `coach.get_coach_response`): **JARVIS** — "calm, precise,
  quietly confident, dry", **capitalized** greetings ("Gm"), title-case, "I don't sleep".
- **`skills/personality/SKILL.md`** (used by `personality.write_response`): **Berkeley peer** —
  **lowercase default**, "bro/ngl/lowkey", peer-not-authority, "friend who got into fitness".
- They **agree** on discipline: no hype, observe-don't-celebrate, drop-wit-on-injury, no
  process-narration, no name-first, opinionated, never re-ask. They **conflict** on persona + casing.

**Transcript evidence (founder's own outbound; verify-against-behavior).** The deployed coach
**fractures by routing path**, mid-conversation: nutrition/daily-log turns skew *capitalized/precise*
("You're at 1300/2450 cal and 122g protein"); freeform turns skew *lowercase-peer* ("go for it -
you've got 1905 cal left today"). That inconsistency IS the "four voices on one number" feel, and it
maps 1:1 to the code: `write_response` (peer) vs `get_coach_response` (JARVIS) vs
`handle_daily_log_query` (direct, formal). The single Phase-2 call is what enforces one voice.

**Merged voice-spec direction (own file, tier-2-validated, founder-reviewable):**
- **Default lowercase-peer** (roadmap target; the transcript's best-reading lines are the peer ones).
- Keep the **shared discipline** both prompts already encode.
- **Lowercase register, precise numbers** — port the JARVIS path's *data discipline* ("1905 left",
  never "~1900ish"); macros are the one place precision *is* the warmth.
- Keep **age-tier tone + style-mirroring** — the transcript is n=1 (the ~20yo founder); beta users
  vary, and the spec already says mirror. Lowercase-peer is the *default*, not a straitjacket.

## 3. Meal-dup root cause (verified — reshapes Phase 3, not Phase 2)

`extract_and_log_meal` (spawned per nutrition turn, orchestrator.py:172) has **no idempotency against
today's log**. Verified in prod (user 25): one meal across ~5 turns (text → text refinement → photo)
produced 3 `meals` rows (61/62/63, distinct sources), because each turn's extractor inserted blind.
NOT Twilio retry, NOT single-request double-write (turns 6–13 min apart). **Phase 3 fix:** the meal
path must read today's meals *before* writing (update-or-recognize), and `manage_log` soft-delete is
necessary but not sufficient. Recorded in memory [[meal-dup-root-cause]]. Phase 2 (loop, no tools)
doesn't touch meal logging; noted so it isn't lost.

## 4. Context assembly to reuse

- **`coach.build_context`** (coach.py:70) returns (user_profile, conversation_history, training_history).
- **Watermark window** (coach.py:101-111): raw window = messages with `id > last_compressed_message_id`,
  capped at `CONVERSATION_HISTORY_LIMIT` (50); summary owns `id <= watermark`. **Reuse as-is.**
- **Incremental summarizer** `maybe_update_coaching_summary` (app.py:358), `_MIN_NEW_TO_COMPRESS=8`.
  Reuse; the loop's context builder must NOT double-cover summary + raw window (watermark already
  prevents overlap).
- **Prompt caching** already implemented: `coach._system_blocks` (coach.py:359) = block1 (stable,
  `cache_control: ephemeral`) + block2 (volatile). Reuse the pattern; gated by `PROMPT_CACHING_ENABLED`.

**Phase-2 unified builder** produces (now with real Phase-1 facts): rendered VALID memory (all
categories, safety universal preserved via `render_categories(include_safety_universal=True)`), safety
constraints, **today's events** (`events.todays_events`), **split pointer WITH provenance**
(`split_pointer.get_split_pointer` → day + confirmed/inferred, so the model hedges on inferred),
typed-column profile, the watermark window, coaching summary, delivered points, and a **known-gaps
line** (unconfirmed workout time, stale injury) with permission to ask ≤1 follow-up. Investigate the
per-agent slice map only to confirm nutrition-scoped allergies stay covered — already handled by the
universal safety render (Phase 0 finding).

## 5. Model + API decisions (verified against current docs via the claude-api skill)

- **Loop model = `claude-sonnet-5`** (current Sonnet; the code's `claude-sonnet-4-6` is previous-gen).
  Pricing $3/$15 per MTok ($2/$10 intro through 2026-08-31). **Add a NEW config key** (e.g.
  `AGENT_LOOP_MODEL`) — do NOT repoint the legacy `COACH_MODEL` (still `claude-sonnet-4-6`), or the
  legacy path could 400 (see next). All model strings live in config.py (B7).
- **Sonnet 5 breaking changes the new loop must honor:**
  - **No `temperature`/`top_p`/`top_k`** — any non-default value → 400. The loop passes none.
  - **Thinking control (verified against the adaptive-thinking doc, not guessed):** on Sonnet 5,
    manual `{type: "enabled", budget_tokens}` → 400; **`{type: "disabled"}` IS valid**; adaptive is
    the default when `thinking` is omitted. **Loop default: `thinking: {type: "adaptive"}` +
    `output_config: {effort: "low"}`** — skips thinking on trivial acks, reasons a little on
    judgment-heavy coaching turns. `disabled` is the lower-latency A/B alternative, chosen by
    *measured* cost. **Hold the mode CONSTANT** — switching adaptive↔disabled breaks the messages
    cache breakpoint (system/tools stay cached). `display` default `"omitted"` is fine (thinking is
    never surfaced to SMS; faster TTFT). effort levels: low|medium|high(default)|xhigh|max.
  - **New tokenizer (~30% more tokens vs 4.6)** — re-baseline cost, give `max_tokens` headroom
    (current `MAX_RESPONSE_TOKENS=400` is fine for SMS; non-streaming ok under 16k). Note adaptive
    thinking tokens count as **output tokens** and are billed even under `display:"omitted"` — they
    land in `usage.output_tokens`, so `cost_tracking` captures them; `usage.output_tokens_details.
    thinking_tokens` is the observable thinking portion.
- **Gate/extraction model = `claude-haiku-4-5`** ($1/$5) — the Phase-4 pre-gate + legacy extraction.
- **Prompt caching:** stable prefix = voice + domain prompt (frozen) with one ephemeral breakpoint;
  volatile suffix = per-user memory/events/window AFTER it. Verify `usage.cache_read_input_tokens > 0`
  across turns; min cacheable prefix ~2048 tokens (the merged prompt exceeds it). No `datetime.now()`
  / unsorted JSON in the prefix (silent invalidators).
- **Cost instrumentation — record at STANDARD pricing, not the intro meter.** Sonnet 5's $2/$10 intro
  rate runs only through **2026-08-31**; the beta's retention window extends past that, so a forecast
  built on July's metered rate under-prices by 50% the month beta is live. The Phase-2 summary records
  **both**: measured-at-intro ($2/$10) for bill reconciliation AND **projected-at-standard ($3/$15)** —
  and **Phase 4's heartbeat cost model consumes the standard number.** Convenient reality:
  `config.MODEL_PRICING` already holds standard ($3/$15 sonnet, $1/$5 haiku), so `cost_tracking.track`
  already logs `cost_usd` at the **standard/forecast** rate; the intro-actual is computed separately
  for reconciliation. Every loop call routes through `cost_tracking` (token counts are the ground
  truth; both cost figures are token_counts × the two rates).

## 6. Flag + fallback (task #12 is the gate)

- New flag `SINGLE_AGENT_LOOP_ENABLED` (os.getenv pattern). When on: `process_buffered_message` tries
  the loop; on ANY exception → fall back to `route_message` (legacy) + `logger.error` loudly; user
  never sees a gap. When off: legacy path unchanged.
- **Tier-1 fallback test (invariant #5, first flag in front of every inbound):** force the loop to
  raise → assert (a) a reply is still sent via legacy, (b) a loud error logged, (c) no user-visible gap.
- Post-reply memory-write threads (`extract_and_store_memory` etc. in `process_buffered_message`) run
  on BOTH paths and stay until the `remember` tool proves recall parity (Phase 3) — the loop does not
  remove them yet.

## 7. Open items / to verify at build
- Whether `coach.get_coach_response` passes `temperature` (if so, it's why legacy must stay on 4-6).
- Exact `write_response` two-stage contract (structured → voiced) for extracting the peer voice lines.
- Confirm the known-gaps inputs available from typed columns / pointer / events.
