# Heartbeat Truncation + stop_reason Observability — Change Spec

What's there now → what it becomes → why. Investigation (incl. the full 20-site
audit): INVESTIGATION.md.

## Fix 1 — decide ceiling + explicit truncation outcome

### config.py
- **Now:** decide runs under `MAX_RESPONSE_TOKENS = 400` (the SMS reply cap).
- **Becomes:** `HEARTBEAT_DECIDE_MAX_TOKENS = int(os.getenv(..., "2000"))`, placed
  in the heartbeat block with a comment naming the Aug 6 failure.
- **Why 2000:** identical workload shape to `AGENT_LOOP_MAX_TOKENS` (adaptive
  thinking + tool JSON + composed message, same model, same low effort), which has
  run live at 2000 since Phase 2 without a truncation log. Env-overridable for the
  life-context growth to come. Cost bound: worst case cents/day per user.

### heartbeat.py `decide()`
- **Now:** `max_tokens=config.MAX_RESPONSE_TOKENS`; branches on pause_turn /
  tool_use; anything else falls to the bare-text fallback → empty text becomes
  `(False, "no message composed")` — truncation is indistinguishable from chosen
  silence, and a *partial* compose would have been SENT as a cut-off SMS.
- **Becomes:**
  - `max_tokens=config.HEARTBEAT_DECIDE_MAX_TOKENS`.
  - `stop` captured once per iteration; recorded into the decision-metadata dict
    (`search["stop"]`) so the tick log can carry it.
  - New branch BEFORE the bare-text fallback: `stop == "max_tokens"` →
    `logger.warning("HEARTBEAT_TRUNCATED user=… stop=max_tokens blocks=… max_tokens=…")`
    and return `(False, "truncated:max_tokens (decide hit the output cap
    mid-generation — not chosen silence)", search)`. **Never sends partial text**
    (behavior change vs. the old fallback, which would have texted a mid-sentence
    fragment — the "glitchy connection" class). Unlike agent_loop (which returns
    `last_text` because a reply is owed), a heartbeat owes nothing; the safe
    degrade is a labeled silent tick.
  - The labeled reason flows into TICK HISTORY on later ticks, so a truncated tick
    can't read as "already considered and declined."
  - The residual `"no message composed"` terminal (clean stop, no tool, no text)
    now logs `HEARTBEAT_NO_OUTPUT user=… stop=… blocks=…` — the response-shape
    anomaly log whose absence let this hide.

### heartbeat.py `_log_tick()`
- **Now:** `HEARTBEAT_TICK user=… spoke=… reason=… search_available=… search_used=…`
  — a truncated tick and a chosen-silence tick log identically.
- **Becomes:** `… reason=… stop=<stop_reason> search_available=…`. `stop=None`
  means no model call (guardrail tick). No DB schema change — the reason string
  plus the log line carry the distinction; a column can follow if we ever want to
  query truncation rates historically.

## Fix 2 — universal stop_reason logging at the chokepoint

### cost_tracking.py
- **Now:** `track(user_id, site, model_str, usage)`; the TOKENS line logs token
  buckets only. 17/20 Anthropic call sites already route through it.
- **Becomes:** `track(user_id, site, model_str, usage_or_response)` — accepts the
  full Message response (usage + stop_reason extracted) or a bare usage object
  (back-compat; stop logs as None). TOKENS line gains ` stop=%s`. `record_usage`
  (the DB row) is unchanged — no migration; the log line is the observability
  surface, greppable as `TOKENS .* stop=max_tokens` across every site.

### Call sites (all of them)
- 17 tracked sites: pass `response` / `resp` instead of `.usage` (one-token diff
  per site; `track_usage` in app.py / onboarding_agent.py is an import alias of
  the same function).
- 3 untracked sites (agents/nutrition.py `handle_food_photo`,
  `handle_photo_refinement`, `handle_receipt_photo`): `track(...)` added — closes
  the stop_reason gap AND the missing cost telemetry, per cost_tracking's own
  "every messages.create() site" contract. New site keys:
  `nutrition.handle_food_photo` / `.handle_photo_refinement` /
  `.handle_receipt_photo`.

## Out of scope (spec guardrails)
Heartbeat calibration/thresholds; life-context capture; the cache-never-hits cost
issue (decide TOKENS lines show cache_read=0 — real, parked); week-level deixis
v2; raising the legacy JSON-extractor ceilings (they now log their stop, which is
the data a future raise needs). If the raised ceiling visibly changes decide
*behavior* (not just prevents truncation), that's calibration — note, don't chase.
