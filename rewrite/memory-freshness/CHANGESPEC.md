# CHANGESPEC — Memory Freshness & Stand-Behind-Memory

Numbered, per the working rhythm: what's-there-now / what-it-becomes / why / where.
Scope guardrails honored: no new memory categories, no capture guidance, no episodic
window changes. Fix 4 is a documented no-op (INVESTIGATION §4: staleness structure is
real, not demonstrably feeding outputs — deferred to consolidation with the
safety-supersession dependency recorded).

## 1. Event store: date-only events keep their date (Fix 1 prerequisite)

- **Now:** `handle_log_event` → `_parse_local_dt` returns `None` when no time is
  given; `Event.occurred_at` then defaults to *now* (models.py:341). "exam friday"
  (no time) is stored as **today**. Under the new lifecycle reader this would render
  as *passed today* — a new wrong output — so the store must be fixed first.
- **Becomes:** in `handle_log_event`, a date-only item stores
  `occurred_at = local midnight` and `ends_at = local end-of-day (23:59)` of the
  resolved date — an all-day event with a real, correct day. Timed items unchanged.
  (`_parse_local_dt` itself is untouched — manage_log's `event_time` edits still
  mean "no time → no change".)
- **Why:** lifecycle state is computed from datetimes; datetimes must be right.
- **Where:** `agent_tools.handle_log_event`; test `test_event_lifecycle.py::
  test_date_only_event_keeps_its_date` (red first: asserts tomorrow's date, current
  code stores today).

## 2. Lifecycle readers: `upcoming_events` / `recently_passed_events` (Fix 1)

- **Now:** the only reader is `todays_events()` — future events invisible until
  their day; passed events indistinguishable from upcoming, then gone at midnight.
- **Becomes:** two new readers in `events.py`, both `source='model'` +
  `active()`-filtered, both computing state from datetimes (**no stored lifecycle
  column** — INVESTIGATION §design-question):
  - `upcoming_events(user_id, days=7)`: events with `occurred_at` after the user's
    local **end of today** and within `days` local days ahead.
  - `recently_passed_events(user_id, hours=48)`: events whose **effective end**
    (`ends_at`, else `occurred_at` + 90-min default — same default duration
    discipline as `in_class`) is in `[now − hours, now)` and **before local start
    of today** (today's passed events stay in TODAY'S EVENTS, see §3).
  - Effective-end helper `event_end(ev)` shared by both and by the §3 render.
- **Why:** the reader gap is the direct cause of failure #2's class; forward
  visibility is the audit's UPCOMING gap.
- **Where:** `events.py`; tests: upcoming-renders-forward, passed-never-upcoming,
  48h age-out, and the tz day-boundary case (a 5pm-PT event is not "passed" at 11pm
  UTC of the same local day).

## 3. Context render: lifecycle-aware blocks (Fix 1)

- **Now:** one `## TODAY'S EVENTS` block; no forward block; no passed handling.
- **Becomes (`build_loop_context`):**
  - `## TODAY'S EVENTS` keeps its name, membership, and `[id N]` format (existing
    tests pin these), with one addition: a **passed model event** gains the suffix
    `— PASSED (already happened; never treat as upcoming, at most one natural
    follow-up)`.
  - New `## UPCOMING EVENTS (next 7 days — logged ahead of time; you may reference
    or prep them)` from `upcoming_events`, rendered with weekday + date + time
    (`timefmt.render_date`/`render_time`).
  - New `## RECENTLY PASSED (happened, not followed up on yet — at most ONE natural
    "how'd it go", then let it go; NEVER mention as still upcoming)` from
    `recently_passed_events`.
  - Once-ness and retirement: bounded 48h window in code + the already-pinned
    anti-repetition machinery (tick history / RECENT PROACTIVE, PR #18) + existing
    `manage_log` delete as the explicit retire path. No write-on-render state.
- **Why:** a passed event must never render as upcoming (failure #2); an upcoming
  event should be visible before its day (audit gap). Both surfaces (reactive +
  heartbeat) get this for free via the shared builder.
- **Where:** `agent_loop.build_loop_context`; tests assert block membership and the
  passed-annotation; existing `test_log_event_tool` pins must stay green untouched.

## 4. De-deixis at capture — code annotator + prompt anchors (Fix 2)

- **Now:** four writers store relative-time words as durable text with no guard
  (INVESTIGATION §2). The episodic prompt's own example models the bug; the digest
  call has no date anchor at all.
- **Becomes:**
  - **Code guarantee** (guardrails live outside the model): `timefmt.resolve_deixis
    (text, user, now=None) -> str` — precision-biased regex over day-level relative
    terms (`today`, `tonight`, `this morning/afternoon/evening`, `tomorrow` (+
    morning/afternoon/evening), `yesterday`, `last night`) that **annotates** each
    with the resolved absolute date in the user's local tz: `"midterm tomorrow"` →
    `"midterm tomorrow (Fri Aug 7)"`. Annotation, not replacement (meaning survives
    a rare mis-resolve); idempotent (a term already followed by `(` is skipped);
    week-level terms ("this weekend/week") deliberately excluded v1 — precision
    over recall, same bias as events.py's regex floor.
  - Applied at the four write sites: `episodic.digest_user` (note text before
    store), `agent_tools.handle_remember` (add/update text),
    `app.extract_and_store_memory` (each fact text), `memory.
    apply_safety_signals_task` (constraint snippet). Call sites, not `apply_facts`
    — the chokepoint is pure and tz-blind; changing its signature ripples into
    consolidation for no gain.
  - **Prompt anchors:** `DIGEST_PROMPT` gains a "Now: {local day, date}" line
    (injected per call) + the rule "write absolute dates, never bare
    today/tomorrow" + the example rewritten to model it ("Big orgo midterm Fri
    morning (Aug 7)…"). The extraction prompt gains the same anchor + rule. The
    `remember` tool description gains one clause ("resolve relative dates —
    'tomorrow' → the actual date").
- **Why:** failure #3, and the mechanism behind failure #2. The code annotator is
  the deterministic floor (tier-1 testable); the prompt changes raise native
  quality above it.
- **Where:** `timefmt.py`, `episodic.py`, `agent_tools.py`, `app.py`, `memory.py`;
  tests: annotator unit cases (resolution, idempotency, tz-correct dates), digest
  stores annotated text (fake-anthropic returns deictic note), remember stores
  annotated text.

## 5. Stand behind real memories (Fix 3 — voice.md, shared surface)

- **Now:** no rule for "user questions a fact you hold." The two adjacent rules are
  scoped elsewhere (verify-before-conceding → search-checkable claims; honesty
  section → the miss case only). Live result: the Jul 31 double-fold on true facts.
- **Becomes:** one tightly-scoped addition inside `## Your own memory and gaps
  (honesty)` (placement keeps it off the accountability/voice sections and applies
  to both loops):
  - questioned ≠ wrong: if the questioned fact IS in your context, stand behind it
    and say where it comes from ("the coding interview — you mentioned it thursday");
    never retract-to-soothe; back off only if you genuinely have nothing.
  - a correction is different: "that got cancelled / I never had that" → accept,
    write the update (remember update/invalidate, manage_log edit/delete), confirm,
    move on. Questioned → show your evidence once; pushed again after showing it →
    treat as a correction. (The over-correction guard the spec requires.)
- **Why:** the missing mirror of the honesty invariant — folding on a true memory
  is its own dishonesty.
- **Where:** `prompts/voice.md` only; no other prompt surface touched. Verified by
  tier-2 replays (spec tests 6 & 8); tier-1 confirms the shared-prefix render still
  loads (smoke) and no reactive/heartbeat isolation change (voice.md was already
  shared — the addition is surface-neutral by construction).

## 6. Fix 4 — stale safety entries: no-op, recorded

- Not demonstrably feeding outputs (INVESTIGATION §4). Left for consolidation, with
  the dependency recorded there: safety-supersession ("recovered" closes "currently
  ill") must carry the Phase-1 audit trigger. No code here.

## Test plan (red first)

Tier-1, new `tests/tier1/test_event_lifecycle.py` + `test_deixis.py`:
1. date-only `log_event date='tomorrow'` stores tomorrow (red on current code: lands today)
2. future event renders in UPCOMING before its day; absent from TODAY'S EVENTS
3. passed prior-day event: absent from UPCOMING, present in RECENTLY PASSED within 48h, absent everywhere after 48h
4. today's passed model event carries the PASSED suffix in TODAY'S EVENTS; today's future one doesn't
5. tz boundary: 5pm-PT event queried at 11pm UTC same local day → not passed
6. `resolve_deixis`: each term resolves against a fixed now + user tz; idempotent; annotation not replacement
7. episodic digest stores annotated text; remember add stores annotated text
8. regression: full tier-1 suite green, no existing pin modified

Tier-2 (funded key), new `tests/tier2` cases mirroring the spec:
6. founder replay: coach surfaces a held fact → "what interview?" → stands behind with source, no fold
7. passed event never raised as upcoming; at most one "how'd it go"
8. over-correction guard: "that got cancelled" → accepts + updates, doesn't argue
