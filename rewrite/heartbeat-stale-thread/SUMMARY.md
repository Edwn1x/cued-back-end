# heartbeat-stale-thread — Summary (Fixes 1–3)

Review artifact. Three live defects, one umbrella: the heartbeat re-deadlocking on
its own unanswered question (Fix 1), and the constraints-category crowding pair —
transient grocery inventory stored immortal (Fix 2) because stale immortal illness
states were squatting the cap (Fix 3). All three shipped mid-burn-in, evidence-first.

## Fix 1 — model-layer unanswered-gap deadlock (shipped first, commit cefff5f)

**Live evidence (Aug 6–7):** five straight ticks over 3 hours chose silence citing
"just asked eddie what he's cooking, waiting on his reply - mid conversation" with
the last inbound 1–4 hours old; a 3-tick episode earlier the same day did the same.
8 of ~12 decide ticks muted. Every stuck tick logged `stop=tool_use` (the model
CALLED stay_silent) — the new stop= observability confirms this is a
reasoning/context defect, **not** a truncation recurrence.

**Root cause:** `_proactive_context` dated only the coach's last OUTBOUND. With no
inbound age, a transcript ending on the coach's own question read as a live
exchange, and each tick's "waiting on reply" reason re-entered the next tick via
TICK HISTORY — the code-layer deadlock PR #17 fixed, rebuilt by model inference
(stale deixis included: "just asked", 4 hours on).

**Fix (code computes, model judges):** code-computed `TIME SINCE THEIR LAST
MESSAGE` block (clean-absent when no inbound exists) + the mid-conversation
silence bullet bound to that number — a tick only reaches the model past the
30-min pre-gate; an hours-unanswered coach question is an OPEN THREAD (speak
material: nudge it or drop it); a held-over tick-history reason must be re-verified
against the timestamps.

**Evidence:** tier-1 pins both context branches; tier-2 binary yes-anchor
reproduces the prod state (accountability gap + reactive question 6h unanswered +
seeded "waiting on reply" tick echo) — **3/3 live green**; all prior anchors re-run
green, critically the mid-conversation NO-anchor (3-min-old inbound still silent —
no double-text leak).

## Fix 2 — grocery/on-hand food is inventory, not an immortal constraint

**Live evidence (Aug 7, 01:35 UTC):** `REMEMBER_TOOL … category=constraints` stored
a TJ's haul; `MEMORY_EVICT … reason=category_soft_cap` evicted the PRIOR
food-on-hand list — real image-persisted food facts silently dropped because
transient inventory was landing in the eviction-immune safety bucket (undoing the
tenders-persistence fix at the cap layer).

**Fix:** new `food_on_hand` category — never safety, TTL-aged
(`FOOD_ON_HAND_TTL_DAYS`, default 14, 0=off). Expiry reuses the freshness
discipline: `expire_stale_entries` INVALIDATES into `__history__` (auditable, never
a silent delete; `MEMORY_TTL_EXPIRE` logged), swept on every `apply_facts` write
AND nightly by consolidation (`_expire_ttl`, reported in the human-readable
summary). Routing updated at both write mouths: the remember tool description and
the extraction prompt (grocery → `food_on_hand`, NEVER constraints, never
safety_critical). The live unified loop renders all categories, so inventory
reaches the SMS loop + heartbeat with no map change; nutrition's per-agent slice
adds it explicitly.

## Fix 3 — superseded safety states close (stale-entry invalidation reopened)

**Live evidence (prod DB, read-only):** 13 `constraints` entries, ALL one
cyclospora/GI arc (Jul 20–31), all safety:true → eviction-immune; ~1,430 chars
against the 400-char soft cap; "gut has fully recovered" (Jul 31, uses=101)
coexisting with five "currently experiencing…" states. This crowding is WHY Fix
2's eviction hit real food data — demonstrably not a no-op.

**Mechanism (deterministic, precision-first, A5 regex-floor style):** a
RESOLUTION-phrased entry ("recovered/resolved/back to normal…") closes OLDER
same-category safety entries only when ALL hold: the older entry is
transient-phrased ("currently/recently/still/holding off…") or an older
resolution; the two share a body-system topic bucket (gi ≠ shoulder ≠ knee…); no
allergy/intolerance vocabulary (allergies are never machine-closed). The resolver
need NOT be safety-flagged — live-gate finding: the model emits recovery with
`safety_critical` either way. Every closure goes through the untouched Phase-1
`invalidate_entry` guard: trigger recorded (`resolved_by:<id>:<text>`), WARNING
logged, trigger-less safety closure still REJECTED. Durable-phrased facts ("bad
knee - doctor said no squats") are never machine-closed.

**Where it runs:** end of every `apply_facts` write (a recovery fact closes its
arc in the same turn) and nightly consolidation (`_supersede_safety` — covers
already-settled arcs like prod's, where no new write may arrive). Consolidation's
dropped-safety invariant now accepts EXACTLY the audited supersession ids and
aborts on anything else; closures appear in the founder's one-line summary. Flag:
`MEMORY_SAFETY_SUPERSESSION_ENABLED` (ship-on + instrumented; the flag is the
rollback lever). The prod arc closes on user 25's first write — or the first
nightly consolidation — after deploy: 13 → 1 live entry, ~1,300 chars freed.

## Tests

- **Red-first:** both tier-1 files written and run RED (10 failed) before
  implementation; the 3 pre-green cases were the never-close negative guards,
  trivially satisfiable pre-mechanism, kept as regressions.
- **Tier-1 (all green, 255 passed):** food_on_hand category + tool enum + routing
  pin; non-immortal/evictable; TTL expiry (stale expires, fresh survives, 0
  disables, safety and non-TTL categories untouched); consolidation TTL sweep with
  summary line. Supersession: prod-replica arc closes with recorded triggers +
  WARNINGs; non-safety resolver closes the arc; newer resolution supersedes older;
  unrelated active safety entry NEVER closed; allergy vocab never closed;
  durable-phrased never closed; Phase-1 no-trigger rejection intact; flag-off
  disables; consolidation closes a settled arc, reports it, and the invariant
  accepts only audited ids.
- **Tier-2 gates (binary, 3/3 after one tuning round):** grocery haul routes to
  `food_on_hand`, constraints stays empty (the Aug 7 miss, replayed); "stomach's
  fully back to normal" against seeded active GI states closes them in-turn. The
  2nd run of round one caught the safety-flagged-resolver hole (model emitted
  recovery with safety_critical=False → nothing closed) — fixed, pinned in tier-1,
  then 3/3. Image-persistence tier-2 re-run green (the remember-description
  surface).
- **Isolation:** Fixes 2+3 touch `memory.py`, `consolidation.py`, `agent_tools.py`,
  `app.py`, `config.py` only — no `heartbeat.py`, no `voice.md`, no heartbeat
  prompt text; heartbeat sees `food_on_hand` only as ordinary rendered memory.

## Known flakes (pre-existing, not addressed here)

- `tier1/test_event_lifecycle.py::test_todays_passed_event_carries_passed_suffix`
  fails near local midnight (a "+40 min" fixture event crosses the local-day
  boundary) — fails identically on the clean tree; needs a frozen-clock fixture.
- `tier2/test_image_fact_persistence.py` honesty assert was phrase-brittle
  ("don't actually have" ≠ "don't have") — robustified to a negation+verb regex in
  this branch since it gated our re-run.
