# SUMMARY — heartbeat speak-calibration (PR2)

## What this fixes

PR1 (#17) opened the code gate; this PR fixes what the open gate exposed: **given the
chance to speak, the pre-calibration model chose silence on the clearest possible
accountability moment** (0/4 in the tier-2 burn-in, including a ~10-day fall-off for a
user who asked to be called out). Three structural biases, addressed at their loci:

| Bias | Locus | Fix |
|------|-------|-----|
| #1 empty history → caution | code (`_proactive_context`) | 2a: render empty history as explicit PERMISSION, not a void |
| #2 "no new info since last tick" | prompt (`HEARTBEAT_PROMPT`) | 2b: standing conditions are valid triggers absent new input |
| #3 threshold too high | prompt (`HEARTBEAT_PROMPT`) | 2b: concrete SPEAK/SILENT anchors; anti-nag is code, not self-suppression |

Judgment in the prompt, limits in code. The anti-stack window + daily cap (PR1) are the
floor that keeps a loosened prompt from becoming a nag.

## Separability

Confirmed the heartbeat decision surface is cleanly separable from the reactive loop:
edits touch only the SECOND system block (`HEARTBEAT_PROMPT` + `_proactive_context`),
which nothing reactive reads. `prompts/voice.md` (block 1, shared) and
`build_loop_context` (shared) are untouched. Full trace + grep evidence in
INVESTIGATION.md.

## Verification status

### Tier-1 (deterministic) — RUN, GREEN ✅
- `pytest tests/tier1/test_heartbeat.py -q` → **31 passed**.
- `pytest tests/tier1/ -q` → **181 passed, 2 skipped** (PR1's 179 + 2 new; anti-nag
  guardrail tests still green).
- Red-first confirmed: `test_proactive_context_empty_history_renders_permission` FAILS
  on the pre-2a code (verified by stashing `heartbeat.py` and re-running).

### Tier-2 (live anchors) — NOT RUN in this workspace ⚠️
**This Conductor worktree has no `.env` / funded `ANTHROPIC_API_KEY`, so the live
anchors could not be executed here.** They are written, collect cleanly (7 tests), and
are ready to run. The founder must run them on a keyed machine:

```
pytest tests/tier2/test_heartbeat_proactive.py --run-tier2 -s   # anthropic==0.116.0
```

**Interpret the yes-anchor HONESTLY — it is BINARY. Re-run it 2–3×:**
```
for i in 1 2 3; do pytest tests/tier2/test_heartbeat_proactive.py \
  -k speaks_on_accountability_gap --run-tier2 -s; done
```
- 3/3 speaks = solid, calibration landed.
- **2/3 is NOT "close enough."** It means the coach stays silent on a third of the
  clearest accountability moments — keep tuning the prompt, do not lower the bar.

Expected after 2a+2b:
- **YES-anchor** (`speaks_on_accountability_gap`): flips 0/4 → speaks (was RED).
- **NO-anchors** (empty-state, on-track-quiet, mid-conversation): stay silent.
- Blended speak rate moves to a small non-zero on warranted days (not "speaks more" —
  "speaks when a good coach would"). Record before (0/4) vs after here once run.
- Per-track cost (searched vs unsearched) from `test_heartbeat_speak_rate_and_cost_summary`.

### Founder-phone (the real gate) — pending
With `LEGACY_SCHEDULER_ENABLED=false` and the allowlist on the founder's number, watch
for the first proactive message that reads like a coach who noticed a 10-day skip — not
a bot on a timer — while quiet days stay quiet. Only after that lands do
allowlist-widening / Phase-6 deletions become eligible.

## Before/after speak rate (fill in after the live run)

| Anchor | Before (pre-calibration) | After (this PR) |
|--------|--------------------------|-----------------|
| accountability-gap (YES) | 0/4 silent | _run 2–3×, record here_ |
| empty-state (NO) | silent ✓ | _record_ |
| on-track-quiet (NO) | n/a (new) | _record_ |
| mid-conversation (NO) | n/a (new) | _record_ |
