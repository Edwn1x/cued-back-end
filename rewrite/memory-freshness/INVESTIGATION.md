# INVESTIGATION — Memory Freshness & Stand-Behind-Memory

Tight-slice trust-bug fix (mid-burn-in, deliberate). Upstream artifact:
`rewrite/life-context-audit/INVESTIGATION.md`. Method: code trace + prod data
(read-only, user 25, via the Railway proxy — same access as the audit). The founder's
correction to the audit's framing is adopted throughout: **the data was correct; the
handling destroyed it.**

---

## 1. How time-bound facts are stored and surfaced today

### Where "interview / appointment"-class facts actually landed (prod, user 25)

The designed home for dated one-offs is `log_event` (voice.md §"Remembering vs
scheduling" routes them there explicitly). In practice they landed everywhere else:

| Fact | Where it landed | Form |
|---|---|---|
| The coding interview | memory `schedule` → "has a code interview this afternoon" (later invalidated to `__history__`); memory `identity` → "code interview is complete"; episodic note #2 → "Coding interview at 2:15pm **today**"; coaching summary → "Coding interview passed — Jul 31 ~2:15pm" | three timeless strings + one correctly dated summary line |
| The Li Ka Shing appointment | memory `schedule` → "has a medical appointment at Li Ka Shing at 9am" AND "had a medical appointment at Li Ka Shing this morning" (twice, one "(safety)") | timeless, deictic, duplicated across tense |
| Startup school / exam prep | `events` (3 rows, Jul 24, correct datetimes) | the one correct landing — from a calendar screenshot |

**Why they miss `log_event`:** two paths write memory with no `log_event` equivalent.
(a) The **legacy per-turn extraction** (`extract_and_store_memory`, app.py:203) is
still live in parallel with the remember tool (Phase 6 parity deletion pending). It
runs post-turn with **no tools** — any time-bound fact it catches can *only* become a
timeless memory string. Its prompt has an anti-transient rule ("Do NOT emit temporary
states") but no date-resolution rule and no concept of routing dated items elsewhere;
the live "this afternoon"/"this morning" entries are exactly what it produces.
(b) In-conversation, when the model does reach for a store, nothing *forces* the
log_event route for a spoken (non-screenshot) mention; the burn-in shows it chose
`remember` for the interview and appointment.

### How each store reaches the two decision surfaces

All of it funnels through `build_loop_context` (agent_loop.py:77-233), which serves
BOTH the reactive loop and `heartbeat.decide` (heartbeat.py:197 wraps it):

- **Events** → `## TODAY'S EVENTS` via `todays_events()` (events.py:167) — local-today
  window only. **No forward reader** (an event for Friday is invisible until Friday)
  and **no passed handling** (a 2:15pm event at 6pm renders identically to one at 9am
  — nothing marks it occurred; and yesterday's event just vanishes, no follow-up
  affordance). Additional bug found (matters for Fix 1, which reuses this store):
  **a date-only event loses its date** — `_parse_local_dt` (agent_tools.py:344)
  returns `None` when no time is given, and `record_event` then lets `occurred_at`
  default to **now** (models.py:341). "orgo exam friday" (no time) is stored as
  *today*. The store is sound only when a time is passed.
- **Memory entries** → `## WHAT YOU REMEMBER` — rendered without their `ts`
  timestamps (memory.render_categories); the model sees only the timeless text.
- **Episodic notes** → `## RECENT LIFE CONTEXT` — date-stamped at render
  ("Jul 31: <text>") but the text itself carries deixis (see §2).
- **Coaching summary** → dated correctly by its prompt's example format; the Aug 3
  context contained "Coding interview passed — Jul 31" *and* the episodic "2:15pm
  today" — the model followed the deictic one. Staleness loses to deixis even when
  fresh info is present.

## 2. The deixis mechanism — confirmed, with the writers enumerated

Confirmed exactly as the audit hypothesized, plus a sharper artifact: **the episodic
prompt's own example teaches the bug.** `DIGEST_PROMPT` (episodic.py:30-34) says
"no date — it's stamped automatically" and then models the output as *"Big orgo
midterm **tomorrow** morning — was stressed about it."* The render stamps the note's
*written-on* date, but nothing resolves the *text's* relative words, and the digest
call passes **no date anchor at all** — the Haiku writer couldn't resolve "tomorrow"
even if told to.

Every path where relative time enters a durable store as literal text:

| Writer | Path | Deixis guard today |
|---|---|---|
| Episodic digest | `_run_digest` → `EpisodicDigest.text` | none — example actively models it; no `now` anchor in the call |
| Legacy extraction | app.py:203 → `apply_facts` → memory JSON | none — no date guidance, no anchor in prompt |
| `remember` tool | model-authored `text` → `apply_facts` | none in tool description; voice.md routes *dated one-offs* away but says nothing about dating the text of facts it does store |
| Safety regex pre-pass | `apply_safety_signals_task` → verbatim message snippets into `constraints` | none — stores user phrasing literally ("currently experiencing…") |
| Coaching summary | app.py prompt | effectively guarded (dated-bullet examples); low risk |
| Events `raw_text` | user message snippet | harmless — the row carries a real datetime; renderers use it |

The model resolves stored deictic words against **now**: demonstrated live (Aug 3
"you've got the interview this afternoon" from the Jul 31 "2:15pm today" note, over a
same-context "passed — Jul 31" summary line).

## 3. Where the coach "folds" — it's an absence, not an over-trigger

Full fold transcript (Jul 31 18:15–18:31 UTC, prod): heartbeat raises appointment +
interview (both real) → "What appointment? What interview?" → coach: "All good - just
tell me what's actually on your plate today" → "What?" → **"my bad, mixing things up.
forget that"** → user asks again → **"my bad, that was a mixup on my end - forget it,
not worth chasing"** → user: "Tell me?" (third push) → coach finally stands behind
both, with specifics → **user confirms the interview is real and gives its time.**
The memory was right the whole way; the coach retracted a true fact twice, and
standing firm was rewarded the moment it happened.

Searched `voice.md` for an instruction that would *produce* the retreat: **there is
none** — no "back off when questioned," nothing over-triggering. The fold is produced
by an absence plus two near-miss rules that each cover an adjacent case but not this
one:

1. **"Verify before you concede"** (voice.md §web search, lines 239-245) — exactly the
   right instinct, but scoped to *search-checkable factual claims* ("a health fact, a
   number, a name"). "What interview?" is **context-checkable**, not search-checkable;
   the rule as written doesn't reach it.
2. **§"Your own memory and gaps (honesty)"** (lines 207-224) — covers the *miss* case
   ("can't find something they say they told you → say you don't have it") and
   agreeing-something-was-lost ("verify-before-conceding applies here too"). It never
   states the *hit* case: **when the user questions a fact you DO hold, cite it and
   stand on it.** The honesty section is one-directional.

Two aggravators, both fixable elsewhere in this slice:
- **Undated stores make standing firm feel unsafe.** The heartbeat had already said
  "interview *yesterday*" (wrong — the undated "this afternoon" entry gave it no way
  to place the date). A model that has just been burned on timing plausibly treats
  its own memory as suspect and retreats. Fix 2 (dated text) is partially what makes
  Fix 3 (stand behind, cite the source) *executable* — you can only say "the
  interview you mentioned — saved Thursday" if the store carries dates.
- "**my bad" is cheap and terminal** in the current voice ("my bad once is the
  ceiling" caps apologizing but nothing prevents apologize-and-drop as the default
  move under challenge; the Jul 31 fold used "my bad" three times).

**Conclusion for Fix 3: an addition, not a removal** — a stand-behind rule in the
honesty section (question ⇒ cite your memory and its date/source; correction ⇒
accept and write the update; never retract-to-soothe), plus widening
verify-before-conceding to include checking your own context, which line 223 already
gestures at.

## 4. The immortal-stale safety interaction — real as a structure, NOT feeding the bug

Structure confirmed: all 13 `constraints` entries are `safety:true` (1,428 chars vs
the 400-char soft cap; eviction skips safety, memory.py:471-475; consolidation leaves
active safety untouched by design). Superseded states coexist — "gut has fully
recovered" (ts Jul 31) alongside five "currently experiencing…" (ts Jul 20-22) — and
all render into every prompt on both surfaces.

Behavior check (prod outbound, Jul 25 → Aug 4): every gut/illness reference tracks
the **live conversation**, not the stale entries — "gut's still settling" (Jul 25,
true then), "gut still cooperating?" (Jul 31, he was still confirming recovery), and
**zero illness references after the Jul 31 full-recovery entry.** The arc resolved
*in* conversation and the coach followed it correctly; the stale entries have not
(yet) produced a wrong output.

**Per the spec's own guardrail: Fix 4 is note-and-defer.** The staleness class is
real and belongs to consolidation (a "recovered" entry superseding "currently ill"
safety entries needs the safety-invalidation audit guard and deserves its own
tested pass there, not a rider here). Recorded as a dependency for the consolidation
work: safety-supersession with recorded trigger.

## Wrong hypotheses, recorded

1. **"Confabulated intimacy" (the audit's framing).** Wrong in the way the founder
   said: the underlying facts were real; the failures are handling (fold, lifecycle,
   deixis). Kept here so the framing correction is durable.
2. **"The fold comes from an explicit back-off instruction over-triggering."** No —
   no such instruction exists in voice.md. It is a gap between two correctly-scoped
   adjacent rules (§3).
3. **"Stale safety entries are feeding confabulation."** Checked against every
   post-recovery outbound — not demonstrated. Deferred to consolidation (§4).
4. **"log_event's store is sound; only the reader is missing"** (the audit's and the
   spec's working assumption). Mostly true, but the store drops the date on
   time-less events (occurred_at defaults to now — §1). Fix 1 must correct the store
   for that case too, or "exam friday" logged today would render as *passed today*
   under the new reader — a new wrong output.

## Design question the spec asked: stored column vs computed lifecycle

**Computed, from the row's datetimes — with one nuance.** `upcoming` vs `passed` is
pure arithmetic over `occurred_at`/`ends_at` vs now in the user's local tz (the
existing `timefmt` discipline); a stored state column could drift from the datetimes
and needs writes nothing else needs. For `done` (follow-up happened): detecting "the
coach followed up" semantically in code is not possible without over-attributing
(render ≠ said; spoke-during-window ≠ spoke-about-it). Rather than a write-on-render
column, the once-and-retire behavior composes from two mechanisms that already exist
and are already tested: a **bounded passed-window** in code (a passed event renders
as follow-up material for a fixed window after it ends, then ages out — deterministic,
tier-1 testable) and the **anti-repetition machinery** (tick history + RECENT
PROACTIVE + the "never send the same nudge twice" rule pinned in PR #18) for
once-ness within the window. `manage_log` delete already gives the model an explicit
retire path. No new stored state.
