# Heartbeat Truncation + stop_reason Observability — Summary

Review artifact. Spec: heartbeat decide truncating at `max_tokens` and logging as
chosen silence; observability gap made universal, not per-surface.

## What shipped

**Fix 1 — the bug.** The decide call ran under `MAX_RESPONSE_TOKENS` (400, the SMS
reply cap) even though thinking + tool JSON + the composed message share that one
budget on Sonnet 5. New `HEARTBEAT_DECIDE_MAX_TOKENS = 2000` (env-overridable,
sized like `AGENT_LOOP_MAX_TOKENS` — same workload shape, proven live since Phase
2). The decide loop gets an explicit `stop_reason == "max_tokens"` branch:
`HEARTBEAT_TRUNCATED` warning with block types, tick recorded as
`truncated:max_tokens (…not chosen silence)`, and **partial text is never sent**
(the old fallback would have texted a mid-sentence fragment). The clean-stop
no-output terminal now logs `HEARTBEAT_NO_OUTPUT stop=… blocks=…`.

**Fix 2 — the recurring class.** `cost_tracking.track` (already the chokepoint at
17/20 Anthropic call sites) now accepts the full response and logs `stop=` on the
TOKENS line; every call site passes the response; the 3 untracked nutrition photo
sites got `track(...)` added (they were also missing cost telemetry). Truncation
on ANY surface is now `grep 'TOKENS .* stop=max_tokens'`. The `HEARTBEAT_TICK`
line gains `stop=` so truncated vs. chosen-silence is distinguishable at the tick
log itself. No DB schema change.

## Evidence

- **Aug 6 tick:** output climbed 117 → 290 → 400 across the three post-deploy
  ticks; 400 == the cap exactly; the only path to `"no message composed"` is a
  non-tool stop with zero text — the truncation signature. `stop_reason` was not
  logged (that WAS the bug), so confirmation is circumstantial; tier-2 case 6
  reads it directly.
- **Red-first:** all 7 tier-1 cases written and run RED for the expected reasons
  before the fix — the truncated tick recorded `reason='no message composed'`,
  and the mid-compose case actually SENT the cut-off text
  (`so about that midter`) through the bare-text fallback. Both prod failure
  modes reproduced, then fixed, then green.

## Tests

Tier-1 (`tests/tier1/test_heartbeat_truncation.py`, 7 cases): truncated ≠ chosen
silence; mid-compose partial never sends; stay_silent reason stays distinct;
send_text regression; `stop=` in TOKENS line (truncated + clean); `stop=` in
HEARTBEAT_TICK line; raised ceiling applied + floor ≥ 2000 (silent-revert guard).
Full suite: **240 passed, 2 pre-existing skips, no new xfails.**

Tier-2 (`tests/tier2/test_heartbeat_truncation_live.py`, spec case 6): one real
decide against a PR-#22-sized context (events in all three lifecycle blocks,
momentum, memory, conversation), asserting the decision terminates without
`stop=max_tokens`. **NOT RUN** — no funded key in this workspace's .env; run with
`pytest tests/tier2/test_heartbeat_truncation_live.py --run-tier2 -s` where one
exists.

## Deploy note (from the spec's definition of done)

After ship, watch the next prod ticks for: no `output=<ceiling>` +
`no message composed` pairs; TOKENS lines carrying `stop=end_turn`/`tool_use`;
any `HEARTBEAT_TRUNCATED` at 2000 (would mean the ceiling needs another look).
Also visible now (parked, out of scope): `cache_read=0` on every decide tick —
the cache-never-hits cost issue.
