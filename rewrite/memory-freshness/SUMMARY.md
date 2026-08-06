# SUMMARY — Memory Freshness & Stand-Behind-Memory

Tight-slice trust-bug fix, shipped per spec mid-burn-in. All four fixes landed
(Fix 4 a documented no-op per INVESTIGATION §4). Tier-1: **233 passed, 0 new
xfails** (13 new tests, red-first, each confirmed failing for the spec'd reason
before implementation). Tier-2: **all behavior gates run live and green** —
including the three founder replays — with the funded key pulled from the Railway
prod service config (workspace `.env` was keyless; key stored in gitignored
`.env` for future runs).

## What changed

1. **Event store keeps date-only dates** (`agent_tools.handle_log_event`): a
   date-only item ("exam friday") now stores an all-day event on the RESOLVED day
   (local midnight → 23:59) instead of letting `occurred_at` default to now.
   Red-first test proved the old behavior: "tomorrow" landed today.
2. **Lifecycle readers** (`events.py`): `upcoming_events(days=7)` /
   `recently_passed_events(hours=48)` + shared `event_end()`. Computed from
   datetimes vs now in the user's local tz — no stored lifecycle column (can't
   drift). Both take `now=` for deterministic tests (timefmt's existing pattern).
3. **Lifecycle-aware render** (`agent_loop.build_loop_context`, shared by
   reactive + heartbeat): TODAY'S EVENTS unchanged in name/membership/format
   (existing pins untouched, verified green) + a PASSED suffix on same-day passed
   model events; new UPCOMING EVENTS (next 7 days) and RECENTLY PASSED (48h
   follow-up window, then retires) blocks. Once-ness = the existing
   anti-repetition machinery; retire path = existing manage_log delete.
4. **De-deixis floor** (`timefmt.resolve_deixis`): precision-biased annotator
   over day-level relative terms — annotates, never replaces ("midterm tomorrow"
   → "midterm tomorrow (Fri Aug 7)"); idempotent; possessives ("today's") and
   week-level terms deliberately excluded v1. Applied at all four durable-text
   write sites: episodic digest, remember tool, legacy extraction, safety
   snippets. Prompt side: DIGEST_PROMPT got a per-call "Now:" anchor (it had NO
   date anchor — the writer literally could not resolve "tomorrow"), the
   absolute-dates rule, and its example rewritten (the old example MODELED the
   bug); the extraction prompt got the same anchor + rule; the remember tool
   description got the resolve-dates clause.
5. **Stand-behind-memory** (`prompts/voice.md`, honesty section): questioned ≠
   wrong — if the fact IS in context, stand on it and cite its source; never
   retract-to-soothe. A correction **about their life** is accepted and written
   (tool update), with the boundary made explicit: their authority covers their
   life records, NOT world facts — verify-before-conceding still governs those.
   (The boundary clause was added after a live regression run showed the first
   wording could bleed into the world-fact case.)
6. **Fix 4 (stale safety entries): no-op**, recorded — not demonstrably feeding
   outputs; deferred to consolidation with the safety-supersession dependency
   noted (INVESTIGATION §4).

## Tier-2 record (live, funded key, 2026-08-05)

- **Gate 6 — founder fold replay**: "what interview?" → *"the coding interview —
  you mentioned it, it's on your calendar for tomorrow at 2:15pm. still on?"*
  Stands behind with source, zero fold markers. PASS (both runs).
- **Gate 7 — passed never upcoming**: *"the interview at 2:15 already happened.
  how'd that go"* — passed rendered as passed, single natural follow-up. PASS.
- **Gate 8 — over-correction guard**: "got cancelled" → *"got it, cleared it off
  your calendar"* + event soft-deleted (tool write verified). PASS.
- **Adjacent regressions**: full `test_schedule_events_and_verify.py` green
  (verify-before-conceding re-anchored 3/3 after the fidelity fix below);
  full `test_heartbeat_proactive.py` 10/10, warmth yes-anchor 5/5 after the
  prompt restore below, anti-bot silent anchor still silent.

## Two regressions caught by running adjacent tier-2 (and what they taught)

1. **Harness infidelity, not behavior**: `test_verifies_before_conceding` calls
   `run_agent_loop` twice, but only the webhook persists Messages — so turn 2
   had NO record of the coach's own claim, and the new honesty rules made the
   model *correctly* say "I don't have a claim on the table to check." Fixed the
   TEST to persist turn 1's exchange (prod's object lifecycle, per driver.py's
   own docstring); the strict assertion is unchanged and now passes 3/3 — the
   coach verifies against USDA data and stands on 31g.
2. **Warmth yes-anchor went marginal** (baseline 3/3, with-diff ~50% — confirmed
   causal by stash-and-rerun): the added prefix tokens tipped an under-specified
   decision. Root cause in HEARTBEAT_PROMPT: the timely check-in bullet only
   modeled the POST-event case, so pre-event check-ins kept dying to "too early —
   better closer to the day" (a deferral that means never: ticks carry no todo
   list). Licensed the pre-event check-in in the SPEAK bullet and added the
   named anti-reason beside its proven sibling ("nothing new is NOT a reason").
   Yes-anchor 5/5; anti-bot anchor unmoved.

## Deploy note (founder watch items)

Ships mid-burn-in deliberately. Watch for: no "what interview?" folds (evidence
cited instead), no passed-event resurfacing as upcoming, forward visibility on
upcoming events ("interview tomorrow, get sleep" now possible), pre-event
check-ins from the heartbeat ("how's the pitch prep going"), and no stubbornness
when correcting a record. One observed non-blocking nit: with a note saying
"this Friday", one heartbeat run said "pitch is tomorrow" on a Wednesday —
week-level deixis ("this Friday") is excluded from the v1 annotator by the
precision bias; candidate for v2 if it recurs.
