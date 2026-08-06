# Heartbeat Truncation + stop_reason Observability — Investigation

Spec: heartbeat decide call truncating at its `max_tokens` ceiling and silently
logging as a chosen-silence tick (`reason=no message composed`), plus the recurring
observability gap (no `stop_reason` logging anywhere but the agent loop). This doc
records what the code actually does, before any change.

## 1. The truncation is real, and the ceiling is the SMS cap

`heartbeat.py:271-275` (pre-fix):

```python
resp = client.messages.create(
    model=config.AGENT_LOOP_MODEL, max_tokens=config.MAX_RESPONSE_TOKENS,
    thinking={"type": "adaptive"}, output_config={"effort": "low"},
    system=system, messages=messages, tools=tools,
)
```

`config.MAX_RESPONSE_TOKENS = 400` (config.py:24) — the **legacy SMS reply length
cap**. The decide call is not an SMS reply: it must fit, in ONE output budget,

- adaptive-thinking tokens (on Sonnet 5 thinking counts against `max_tokens` —
  verified in rewrite/phase-2/INVESTIGATION.md §5 against the adaptive-thinking
  docs, re-confirmed against the current API reference: "`max_tokens` is a hard
  limit on total output (thinking + response text)"; observable portion in
  `usage.output_tokens_details.thinking_tokens`),
- the tool-call JSON for `send_text`/`stay_silent` (including the full composed
  message inside `send_text.message`), and
- any inline web_search reasoning.

This is the **exact** mistake the agent loop already fixed for itself:
`AGENT_LOOP_MAX_TOKENS = 2000` exists (config.py:64-69) with a comment naming this
failure ("400 truncates a multi-item turn … giving stop_reason=max_tokens with no
text"), and `agent_loop.py:437-441` has a dedicated `stop == "max_tokens"` branch
that logs `AGENT_LOOP_TRUNCATED` with block types. The heartbeat decide call —
written later, doing strictly more per call (thinking + decision tools + optional
search) — kept the 400 cap and got neither the headroom nor the branch.

### Evidence for the Aug 6 tick (circumstantial but tight)

`stop_reason` was not logged anywhere on this path — that IS the observability bug —
so the prod tick cannot be retroactively confirmed as `max_tokens`. What the record
shows:

| tick (UTC) | output tokens | result |
|---|---|---|
| 06:27 | 117 | silent, reasoned ("quiet day, conversation settled…") |
| 07:12 | 290 | silent, reasoned ("only ~2.5 days gap…") |
| 07:57 | **400** | silent, `reason=no message composed` |

- 400 == the ceiling exactly. Output below the cap terminates wherever the model
  stops; landing on the cap to the token is the truncation signature.
- The only code path producing `"no message composed"` (heartbeat.py:326) is a
  non-`tool_use`, non-`pause_turn` stop with zero text blocks. A model that chose
  silence calls `stay_silent` (both outcomes are explicit tools; PR #18) and logs
  its reason. A response with *neither* decision tool *nor* text is what a
  mid-generation cutoff looks like — thinking consumed the budget before any
  tool block was emitted (Truncated fixture in tests/_fake_anthropic.py models
  exactly this: `stop_reason="max_tokens"`, `content=[]`).
- The freshness slice (PR #22, deployed ~5.5h before the tick) added three new
  context blocks (UPCOMING EVENTS, RECENTLY PASSED, PASSED marks) + de-deixis
  annotations — more material to reason over, hence the 117 → 290 → 400 climb.
  More context does not raise output *directly*; it raises the reasoning the model
  chooses to do, and reasoning tokens bill against the same 400.

Definitive confirmation is tier-2 case 6 (run the decide against a prod-sized
context and read `stop_reason` directly) — see tests, NOT RUN in this workspace
(no funded key in .env).

## 2. Call-site audit — every Anthropic `messages.create`

20 sites (sms.py:28 is Twilio's `client.messages.create`, not Anthropic — excluded).
"tracks?" = routes through `cost_tracking.track` (aliased `track_usage` in
app.py/onboarding_agent.py), the single shared instrumentation point.

| # | site | max_tokens | inspects stop_reason? | tracks? |
|---|---|---|---|---|
| 1 | coach.py:401 `coach.get_coach_response` | 400 (MAX_RESPONSE_TOKENS) | no | yes |
| 2 | coach.py:533 `coach.generate_scheduled_message` | 400 | no | yes |
| 3 | coach.py:580 `coach.parse_workout_log` | 300 | no | yes |
| 4 | agent_loop.py:400 `agent_loop.run` | 2000 (AGENT_LOOP_MAX_TOKENS) | **YES** — max_tokens, refusal, tool_use, pause_turn, + anomaly log | yes |
| 5 | memory.py:1050 `memory.extract_coaching_points` | 300 | no | yes |
| 6 | app.py:113 `extract_and_store_decisions` | 250 | no | yes |
| 7 | app.py:308 `extract_and_store_memory` | 600 | no | yes |
| 8 | app.py:470 `maybe_update_coaching_summary` | 600 | no | yes |
| 9 | onboarding_agent.py:397 `onboarding.extract_data_from_message` | 400 | no | yes |
| 10 | onboarding_agent.py:498 `onboarding.generate` | 400 (MAX_RESPONSE_TOKENS) | no | yes |
| 11 | orchestrator.py:71 `orchestrator.classify_message` | 200 | no | yes |
| 12 | episodic.py:118 `episodic.digest` | 120 | no | yes |
| 13 | **heartbeat.py:271 `heartbeat.decide`** | **400 ← THE BUG** | pause_turn/tool_use only — **no max_tokens branch** | yes |
| 14 | agents/meal_extractor.py:118 `meal_extractor` | 400 | no | yes |
| 15 | agents/nutrition.py:255 `nutrition.handle` | 400 | no | yes |
| 16 | agents/nutrition.py:441 `handle_food_photo` | 400 | no | **NO** |
| 17 | agents/nutrition.py:553 `handle_photo_refinement` | 400 | no | **NO** |
| 18 | agents/nutrition.py:685 `handle_receipt_photo` | 600 | no | **NO** |
| 19 | agents/weight_extractor.py:59 `weight_extractor` | 200 | no | yes |
| 20 | agents/personality.py:91/185, training.py:304, readiness.py:172 (legacy) | 400 | no | yes |

Findings:

1. **Exactly one site out of 20 inspects `stop_reason`** (the agent loop). The class
   recurred on the heartbeat because the check was a per-surface fix, not a rule.
2. **17 of 20 route through one chokepoint** — `cost_tracking.track` is called
   immediately after every tracked `create` with `resp.usage`. That makes universal
   `stop_reason` logging a one-function change (have `track` accept the full
   response) + a one-line-per-site update, rather than 20 bespoke log lines.
3. **Three sites are completely untracked** (nutrition photo/receipt paths, #16-18)
   — contra cost_tracking's own contract ("Use this at every messages.create()
   site"). They get `track(resp)` added, which closes both the stop_reason gap and
   the missing cost telemetry at once. These are legacy-router paths (dormant when
   SINGLE_AGENT_LOOP_ENABLED routes traffic to the loop) but still reachable code.
4. **Bounded-output JSON extractors (250-600) are lower risk but same class**: a
   truncated JSON blob fails `json.loads` and those sites degrade via their except
   paths. They get observability (stop= in TOKENS line), not raised ceilings —
   raising every cap is out of scope; the tick log shows which sites actually hit
   their caps, which is the data a future raise would need.

## 3. Why the fix values

- `HEARTBEAT_DECIDE_MAX_TOKENS = 2000` (env-overridable): matches
  AGENT_LOOP_MAX_TOKENS, whose rationale is byte-for-byte the same workload shape
  (adaptive thinking + tool JSON + a composed SMS on the same model at the same
  effort). The loop has run live at 2000 since Phase 2 without a truncation log.
  Cost bound: ~32 ticks/user/day × worst-case 2000 output tokens on Sonnet 5 is
  cents/day, vs. a silently dropped proactive message — the product's core wedge.
  Headroom for the life-context blocks the spec says are coming.
- Truncation returns `(False, "truncated:max_tokens …", search)` — never sends
  partial text. Unlike the agent loop (which returns `last_text` because a reply is
  owed), a heartbeat owes nothing: the safe degrade is a silent tick that is
  *labeled* as truncated, distinct from chosen silence in the DB reason, the
  HEARTBEAT_TICK line, and the TICK HISTORY context the next tick reads (so a
  truncated tick can't masquerade as "already considered and declined").

## 4. Out of scope (per spec guardrails)

Heartbeat calibration/thresholds; life-context capture; cache-never-hits cost issue
(the TOKENS lines show cache_read=0 on every decide — real, parked, separate);
week-level deixis v2. Raising the legacy extractors' caps (see finding 4).
