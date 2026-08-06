# INVESTIGATION — Life-Context Audit

**What this is:** the ground-truth audit upstream of the heartbeat "friend who coaches"
rework. Investigation only — no code, prompt, flag, or test changes shipped; the live
burn-in signal is untouched.

**Method:** full read of the capture/decision code paths (`memory.py`, `episodic.py`,
`events.py`, `agent_tools.py`, `agent_loop.py`, `heartbeat.py`, `app.py`,
`consolidation.py`, `prompts/voice.md`), plus read-only SQL against the **production**
database (Railway `athletic-heart` / Postgres, via the public TCP proxy) for the
founder's real data, user_id 25 ("Eddie"). Data as of **2026-08-05 ~06:10 UTC**. All
timestamps below UTC unless noted. Scale of the corpus: 1,427 messages (Jun 26 – Aug 4),
247 heartbeat ticks (Jul 28 – Aug 5), 26 live memory entries, 3 events, 9 episodic
digests.

---

## 0. Two corrections to the spec's framing (surfaced per playbook §III)

The spec is a hypothesis; two of its premises drifted from reality. Neither weakens the
conclusion — both sharpen it.

**1. Episodic is ON in prod, and it reaches the heartbeat.** The warmth-spec
investigation (and the spec's implicit prior) dates from when `EPISODIC_ENABLED`
defaulted off and `episodic_digests` was empty. In production today the flag is **true**
(Railway env), the digest job has run since **Jul 31**, there are **9 rows**, and
`build_loop_context` injects them as `## RECENT LIFE CONTEXT` (agent_loop.py:156-161)
into both the reactive loop and `heartbeat.decide`. Spec item B4: **updated — it does
reach the decision.**

**2. The heartbeat has already tried to be a friend — and half its attempts were
wrong.** "It has never behaved like a friend: no 'how'd the midterm go'" is not what the
tick log shows. The heartbeat has spoken 4 times ever; **3 of the 4 were life
check-ins**, textually almost exactly the spec's example:

| When | Message (abridged) | Outcome |
|---|---|---|
| Jul 31 18:15 | "how'd the appointment go this morning? and did the interview yesterday end up going ok?" | **Stale misfire.** Eddie: "What appointment? And What interview?" — the interview was that afternoon (not "yesterday"); the appointment was days prior, stored as an undated "this morning" fact. |
| Jul 31 22:45 | "hey, interview should be wrapped by now - how'd it go?" | **Clean.** Landed; Eddie engaged. The one fully successful friend-text of the burn-in. |
| Aug 3 04:00 | "you're only at 805/2450 cal... what's around?" | Accountability speak, correct. |
| Aug 3 18:15 | "you've got the interview this afternoon - eat something real beforehand" | **Stale misfire.** Eddie: "What interview?" — it had passed 3 days earlier. Source: the Jul 31 episodic note "Coding interview at 2:15pm **today**" still inside the 5-day context window; the model resolved the note's deictic "today" as the current day. |

So the observed failure is not "the heartbeat never reaches for friend material." It is:
**the system holds exactly one life thread (the interview), the heartbeat reached for it
three times, and twice it was wrong because the material carries no reliable dating.**
The felt experience on the founder's phone is worse than absence — it's a friend who
confabulates your life. The spec's core hypothesis (no friend-material, not
threshold-too-high) is **confirmed**, with a second, equally load-bearing finding
attached: **freshness, not just volume, is missing.**

---

## A. What the system captures today, per category

### A.1 The capture mechanisms (code inventory)

| Mechanism | What it holds | Life-context posture |
|---|---|---|
| `user_profile_memory` (JSON, 6 categories — memory.py:56) | durable facts via the `remember` tool + extraction; 2000-char hard cap, 400/category soft cap; safety entries eviction-exempt | Categories are fitness-shaped: `identity, constraints, training_preferences, communication_preferences, schedule, goals`. No bucket for people, interests, coursework, projects. The tool description says "life context" (agent_tools.py:31-33) but offers nowhere to put it except `schedule`/`identity`. |
| Typed columns (models.py) | body, diet, restrictions, targets, food_context | Pure trainee data by design. |
| `events` table | regex floor (`went_to_gym`, `in_class`) + model `log_event` (dated one-offs) | The designed home for deadlines/plans — `log_event`'s own docstring says "'orgo exam friday 9am'". But the only reader is `todays_events()` (events.py:167): **an event is invisible until the day it happens** — no forward visibility, so no "good luck tomorrow," and the model gets no reinforcement that logged events matter. |
| `episodic_digests` | 1–2 sentence Haiku note per quiet-ended conversation | The only mechanism *scoped to* life ("ESPECIALLY non-fitness life context" — episodic.py:30). Sliding **5-day** context window (`EPISODIC_RECENT_DAYS`). |
| `coaching_summary` (app.py:419-446) | rolling relationship summary | Explicitly scoped **away** from life: "do NOT duplicate those here. Focus only on the COACHING ARC." Working as designed — 0% life on purpose. |
| `delivered_coaching_points` | anti-repetition list | 17/17 lines are macro/logging ops. Nothing life. |
| Conversation window | last ≤50 raw messages | Life context passes through here and **dies at the summarization watermark** — the summarizer keeps only the coaching arc. |

### A.2 The founder's real data, against the 8 categories

Live memory: 26 entries (+1 history). By content: **~23 fitness / GI-illness / gym
logistics, ~3 life-adjacent** ("code interview is complete", "currently busy with school
and has limited training time", "is budget-conscious about meal costs" — the last filed
under `goals`). That is **~10% life by entry count**, and the life entries are
thin, undated, and fitness-framed.

| # | Founder category | Captured today? | Evidence (prod, user 25) |
|---|---|---|---|
| 1 | Schedule / classes | **Essentially no.** | `schedule` has 8 entries — gym hours, Saturday session, "class runs late, pushing leg workout later" (class-as-training-constraint). Eddie did a whole CS 61B homework session in-thread (Jul 21, B-Trees/Red-Black Trees) — **zero durable trace**; "61B" appears nowhere outside dead messages. No course list, times, or academic calendar. |
| 2 | Social life | **No durable capture.** | Nothing in memory. Episodic caught two incidents in passing (sister made donuts; Stockton family visit Aug 1) — both expired from the 5-day window or about to. No friends, roommates, or social plans anywhere. |
| 3 | Interests / hobbies | **Nothing, anywhere.** | No mechanism even aims at it; no entry, note, or column contains one. |
| 4 | Mood / emotional state | **Partially — episodic only, and it drifts coach-ward.** | 3 of 9 notes are mood, but mood *about the coaching interaction* ("tight and dismissive when pressed on food," "questioned if I was 'stupid'") — the spec's predicted drift, confirmed. No mood-about-his-life capture (the 2am 10-mile run note is the one exception that gestures at it). |
| 5 | Current events in their world | **No.** | Heartbeat web_search exists (3/day budget) but there are no stored interests/world anchors to seed a "saw this, thought of you" search from. |
| 6 | Career / projects | **The single richest thread — and still thin, stale, and mostly lost.** | The interview arc (memory + episodic + coaching summary) is the ONLY life thread the heartbeat has ever used. **YC Startup School:** 3 events logged Jul 24 ("finish startup school social app", "61b exam prep", SF pre-events), all expired same-day; on Jul 26 Eddie was *at the event* ("Do you even know what yc startup school is?") and the coach canceled his push day for it — **no durable fact exists** that Eddie is a founder, is building an app, or attended. |
| 7 | Assignments / deadlines | **Mechanism exists, unused.** | `log_event` is purpose-built for this; **3 rows ever, one soft-deleted, all from a single day (Jul 24)**, none since — across 12 more days of burn-in including an exam-prep mention and a homework session. |
| 8 | Upcoming events / plans | **Same 3 expired rows.** | The Stockton trip existed only as an episodic note. Nothing forward-looking is ever *visible* even when logged (today-only reader, see A.1). |

**Honest ratio:** by any measure — entry count (~10%), characters, or category coverage
(0 of 8 fully, 2 of 8 partially) — **the system knows Eddie-the-trainee in detail and
Eddie-the-person barely at all.** The strong prior in the spec is confirmed.

**Adjacent finding (context crowding, flag for design):** all 13 `constraints` entries
are one GI-illness arc, all `safety:true`, therefore eviction-immune (memory.py:471-475)
— 1,428 chars against a 400-char soft cap, and most are superseded states ("gut has
fully recovered" coexists with five "currently experiencing…" entries). This is the same
staleness class that caused the misfires, living permanently in every prompt.

---

## B. What the episodic digest is actually doing

**Prompt scope: correct.** DIGEST_PROMPT (episodic.py:30-34) asks for "ESPECIALLY
non-fitness life context: exams, deadlines, travel, relationships, mood, big events,
work stress," explicitly excludes coaching decisions, and allows NONE. The scope is not
the problem.

**Real output: ~half life, half coaching-ops.** All 9 rows, classified:

| # | Date | Note (gist) | Class |
|---|---|---|---|
| 1 | Jul 31 | stressed about Friday code interview | **life/career** |
| 2 | Jul 31 | interview 2:15pm "today"; make sure he eats | **life/career** (+ deictic "today" — the Aug 3 misfire's source) |
| 3 | Aug 1 | passed interview; wants concrete benchmarks | **life** + coaching-pref |
| 4 | Aug 1 | 10+ miles at 2am — what's keeping him up? | life-adjacent (sleep) |
| 5 | Aug 1 | heading home to Stockton, family visit, train | **life** |
| 6 | Aug 2 | sister made homemade donuts | **life** |
| 7 | Aug 2 | mixed up sister's meal — ask whose plate before logging | coaching-ops |
| 8 | Aug 4 | tight and dismissive today, told me to "fuck off"; lighter touch | coaching-friction |
| 9 | Aug 4 | frustrated twice during check-in; sharp mood | coaching-friction |

Roughly **5–6 of 9 genuinely about the person**; the drift that exists is toward
*coaching friction notes*, exactly as the spec predicted — but it's partial, not total.
For the 5 days it has existed, episodic is the best life-capture surface in the system
by a wide margin.

**Cadence: fine — the 30-minute log line is the sweep, not the writer.** `digest_all`
runs every `EPISODIC_SWEEP_MINUTES=30`, but a digest only fires per-user when the
conversation has been quiet ≥90 min AND ≥4 messages sit above the watermark
(episodic.py:71-74); the watermark makes it idempotent. It is effectively
session-triggered. The real structural limits are elsewhere:

- **One 120-token note per quiet session** — a long, rich session (the whole Stockton
  weekend) compresses to one line; material is discarded at capture time.
- **The 5-day window** (`EPISODIC_RECENT_DAYS`) — life context *evaporates weekly*.
  Notes 1–4 have already aged out as of Aug 5. Nothing consolidates episodic notes into
  durable life facts; the layer is a sliding window, not an accumulating model of the
  person.
- **Deictic rot inside the window** — notes are date-stamped at render ("Jul 31: …")
  but their *text* contains "today"/"tomorrow", and the model demonstrably resolves
  those against *now* (the Aug 3 misfire). Same class as the memory entries "has a
  medical appointment at Li Ka Shing at 9am" / "…this morning" (undated relative
  language in a timeless store) that caused the Jul 31 misfire.
- **A 4-day blind spot** — the flag went live ~Jul 31; the Jun 26–Jul 30 corpus
  (including the entire YC Startup School weekend) was never digested and never will be.

**Reaching the heartbeat: yes** (see §0.1). Both confirmed in code
(agent_loop.py:156-161 renders it; heartbeat's `_proactive_context` starts from
`build_loop_context`, heartbeat.py:197) and in behavior (the interview check-ins).

---

## C. What reaches `heartbeat.decide`, exactly

The decision call's system prompt is `voice.md` (cached) + `HEARTBEAT_PROMPT` + the
context string. The context is `build_loop_context()` (agent_loop.py:77-233) plus
heartbeat-only blocks (heartbeat.py:196-242). Complete block enumeration:

| Block | Source | Content class |
|---|---|---|
| WHAT YOU REMEMBER ABOUT EDDIE | memory, all 6 categories | ~90% fitness/illness (§A.2) |
| PROFILE + Body/Dietary/Food-context lines | typed columns | trainee |
| TODAY'S EVENTS | `todays_events` — **today only** | trainee + (rarely) life; empty almost every tick since Jul 24 |
| SPLIT POINTER | code | trainee |
| COACHING SUMMARY | rolling summary | trainee **by design** |
| ALREADY TOLD THEM | delivered points | trainee ops |
| RECENT LIFE CONTEXT | episodic, 5-day window | **the one life-dedicated block** |
| RECENT CONVERSATION | last ≤50 raw messages | mixed, transient |
| TODAY'S LOGGED MEALS + TOTALS | code-computed | trainee |
| RECENT WORKOUTS | last 5 | trainee |
| KNOWN GAPS | `_known_gaps` (agent_loop.py:67-74) | **fitness-only gaps** — the gap mechanism itself cannot represent "you don't know what he's studying" |
| NOW | code anchor | — |
| MOMENTUM (heartbeat-only) | code-computed 7-day workout count | trainee (the win signal) |
| TIME SINCE LAST MESSAGE / RECENT PROACTIVE / TICK HISTORY / PROACTIVE STATUS (heartbeat-only) | code | ops |

**Verdict: hypothesis confirmed.** Of ~14 blocks, exactly **one** is dedicated life
material, and it is a 5-day sliding window of 1-line notes, currently ~half coaching
friction. Everything else is trainee state or decision ops. The 8 founder categories are
present in the decision context only as: career (one interview thread, now expired),
mood-about-coaching, and whatever survives in the raw 50-message window.

**Tick-log evidence (Jul 28 – Aug 5):** 247 ticks → 4 spoke (1.6%), 145 model-chose
silence, 98 guardrail-blocked (72 of those are `unanswered_gap`, all Jul 28–31 — the
pre-PR-#18 deadlock era; current-code blocks are 22 `active_conversation` + 4
`proactive_stack`). The 145 silence reasons read overwhelmingly as "quiet on-track day,
conversation settled since 'Ok', nothing new" — which, given the context above, is the
model *correctly* reporting that it has nothing to reach for. The warm triggers shipped
in PR #19/#20 are live and demonstrably fire when material exists (Jul 31). **The
bottleneck is upstream of the heartbeat, exactly as hypothesized.**

---

## D. Where each category could live (findings only — nothing designed here)

The through-line: **very little new machinery is needed.** Most categories reduce to
(a) a place in the existing memory taxonomy, (b) extraction/voice guidance that
currently never asks for life material, and (c) one missing reader. Sizes are gut-feel
S/M/L for the eventual spec, not commitments.

| # | Category | Most natural home | Rough size |
|---|---|---|---|
| 1 | Schedule / classes | Recurring timetable → memory `schedule` (it's the designed bucket, currently crowded with gym logistics) or a recurrence notion on Events; one-off academic dates → `log_event`. | S–M |
| 2 | Social life | A people-shaped memory bucket (new category or widened `identity`) + extraction guidance; episodic already catches incidents. | S |
| 3 | Interests / hobbies | Same as 2 — memory + guidance. Also the seed store for any "saw this, thought of you" search. | S |
| 4 | Mood | Episodic already owns it; the work is keeping it scoped to *his* mood/life rather than coaching friction (prompt nuance), not new machinery. | S |
| 5 | Current events in their world | No new capture layer — heartbeat web_search exists and is budgeted; it needs #3's interest seeds to have anything to look up. | S |
| 6 | Career / projects | Memory (`identity`/`goals`: "founder, building a fitness-coaching startup; doing YC Startup School") + episodic for the running thread. Pure extraction-guidance gap — the schema already fits. | S |
| 7 | Assignments / deadlines | **`log_event` — it already exists and is the documented home.** The gaps are usage (guidance never triggers it from plain text mentions) and an UPCOMING reader (next-N-days) so future events are visible before their day. | S |
| 8 | Upcoming events / plans | Same as 7. | S |

**Cross-cutting findings that size the real build:**

- **Capture guidance, not capture schema, is the main gap.** `voice.md`'s
  remember/log_event guidance is food-and-calendar-screenshot-toned; nothing tells the
  model that a homework question, a startup-school mention, or "my sister" is worth a
  durable write. Eddie *volunteered* categories 1, 2, 6, 7 in plain text and the system
  wrote none of them. This is prompt + category work on a **shared surface** (voice.md
  is also the reactive loop's and heartbeat's cached prefix), so it carries tier-2 and
  burn-in-interference weight: **M**.
- **Freshness discipline is its own workstream** and arguably the higher-priority one:
  dated/expiring representations for time-bound facts (log_event got this right — the
  misfires both came from timeless stores), de-deicticized episodic note text, and some
  answer to the superseded-but-immortal safety entries. Without it, *more* life material
  means *more* confabulated check-ins: **M**.
- **Episodic accumulation** — the 5-day window means the system will never *know* Eddie
  better next month than this week; some consolidation of episodic into durable life
  facts (or a longer horizon) is where "knows Eddie-the-person" actually compounds: **M**.
- **Heartbeat-side work is small.** The warm triggers, the higher-bar-for-warmth rule,
  and the episodic injection all shipped in PR #18–#20 and work. Part 2 of the eventual
  build is roughly: an UPCOMING block, whatever new memory categories render into
  context, and re-calibration passes: **S**.

So the spec's anticipated shape — (1) richer capture, (2) heartbeat uses it — is
confirmed but re-weighted: **part 2 is mostly already built; part 1 (capture +
freshness + accumulation) is nearly all of the work.**

---

## E. The wedge check — protecting the accountability spine

Places where the friend expansion could dilute the coaching wedge, for the design phase
to guard explicitly:

1. **Warm and accountability speaks share one budget and one anti-stack window.** A warm
   text consumes `HEARTBEAT_MAX_PER_DAY` (5) and arms the 180-minute `proactive_stack`
   window exactly like a skip nudge (heartbeat.py:127-147). With today's material the
   heartbeat speaks 1.6% of ticks and this never binds; give it real friend material and
   a chatty morning can **structurally silence the evening skip nudge**. The design
   phase must decide the priority rule (e.g., accountability never blocked by spent
   warmth budget) — in code, not prompt.
2. **Keep skip-detection code-computed.** The accountability signals the model acts on
   (MOMENTUM, split pointer, gap-vs-pattern) are code-derived facts. A fatter life
   context must never turn "did he skip?" into model inference over a longer prompt —
   the wedge stays deterministic-in, judgment-out.
3. **The observed drift risk is not softness — it's confabulated intimacy.** The burn-in
   already produced the failure texture: 2 of 3 friend-attempts were wrong about his
   life, and the replies were "What interview?" and "Dumbass." A friend layer built on
   undated material erodes trust *faster* than a tracker that stays silent — and it
   spends credibility the accountability wedge needs ("the coach that invents
   appointments" is not a coach you let call out your skips). Freshness discipline (§D)
   is therefore wedge protection, not polish.
4. **The prompt's higher-bar-for-warmth rule is load-bearing — keep it through
   re-calibration.** HEARTBEAT_PROMPT already encodes "warm only with specific, real
   material; generic pleasantries stay silent" and "accountability is the job; fun is
   the delivery." The re-calibration should treat those lines as invariants with tier-2
   coverage, the same way the anti-nag guardrails are pinned today.

---

## Bottom line

**How much does the system know about Eddie-the-person vs Eddie-the-trainee?** Almost
nothing vs almost everything. ~10% of durable memory entries are life-adjacent (and
undated); the calendar surface holds 3 expired rows from one day; the one life-scoped
layer (episodic) is 5 days old, 9 one-liners, half of them about coaching friction, and
forgets on a 5-day sliding window. Of the founder's 8 categories: **0 fully captured, 2
partially (career-via-one-interview, mood-about-coaching), 6 absent.** Meanwhile Eddie
volunteered his class (61B), his homework, his startup, his YC weekend, his family, and
his travel **in plain text over the burn-in — and the system durably captured none of
it.**

**The hypothesis is confirmed and sharpened.** The heartbeat's friend-wiring works — it
fired warm check-ins the moment episodic gave it one thread to hold. The gap is
upstream (capture), plus a second gap the spec didn't name: **freshness** — both live
misfires came from timeless/deictic representations of time-bound facts, and that class
of bug scales with every new life fact captured.

**Sizing for the founder's build/wait decision:** Part 1 (capture: categories +
extraction guidance on the shared voice surface, log_event usage + an UPCOMING reader,
episodic accumulation, freshness discipline) is the substantial piece — a Phase-2/3-scale
effort, most of it on prompts and one reader, but touching the shared reactive surface,
so it costs tier-2 runs and interacts with the burn-in if shipped mid-stream. Part 2
(heartbeat uses it) is small — the triggers and injection already shipped in PR #18–#20.
One datum for the timing call: the burn-in has already produced two live
confabulation incidents from stale life data, so the freshness slice has
trust-protection value *independent of* the friend expansion, and is the piece that
most plausibly justifies going mid-burn-in rather than after.
