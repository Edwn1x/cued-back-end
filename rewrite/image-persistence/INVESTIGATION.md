# Image-Fact Persistence — Investigation

Spec: the burn-in bug where a fact that appeared *only in an image* (the chicken-tenders
package weight) was read and spoken but never persisted, plus the "nothing came through
on my end" confabulation that explained the gap. Four questions were posed; answers below,
with the hypotheses that died along the way.

---

## 1. Where does the `read_image` path write, and what does it drop?

Traced end to end:

- **Webhook** (`app.py` `/webhook`): Twilio MMS → `MediaUrl0` downloaded, base64'd into
  `image_data`. The *text* body is logged via `log_incoming(user.id, body)` — the Message
  row records **no trace that media was attached** (see §4). Body + image go through the
  message buffer (`image_url` field carries the base64 dict) to `process_buffered_message`.
- **Loop** (`agent_loop.run_agent_loop`): with `READ_IMAGE_ENABLED`, the image block is
  prepended to the user content and the model routes it in-call — there is no
  pre-classifier and no code-side image handler. **All routing is prompt-driven**, by the
  `## Images (MMS)` section of `prompts/voice.md`:
  - food → `log_meal`
  - calendar/schedule screenshot → `log_event`
  - workout whiteboard → `log_workout`
  - **"Anything else → just react to it conversationally … Store nothing structured."**

**The drop mechanism is confirmed, and it is prompt-mandated, not a missing tool.** A
loose fact from an image — a package weight, a nutrition label, a sleep summary — is not
a consumed meal, not a calendar item, not a whiteboard, so it falls into the fourth
bucket, whose instruction is literally to store nothing. Two adjacent instructions
reinforce the drop:

- `remember`'s tool description says it's "not for transient chatter" and "Do NOT log
  meals or workouts here" — a package weight reads as meal-adjacent transient chatter,
  so the model is steered *away* from the one tool that could hold it.
- Nothing anywhere states the load-bearing principle that *reading a fact into the reply
  does not persist it* — the model behaves as if speaking the weight is enough, which is
  exactly the failure §V of the playbook predicts.

The write paths themselves (`remember`, `log_event`, `log_meal`, all in `agent_tools.py`)
are built, flag-gated, and tested. No new tool is needed; the routing rules never send
image-borne loose facts to any of them.

## 2. Does the meal path only write on a *consumed* meal?

Yes — and correctly so. `log_meal`'s description is "Log a meal the user reports
**eating**"; `handle_log_meal` stamps `eaten_at=now` and the row immediately feeds the
code-computed `TODAY'S TOTALS` block. Logging an *uneaten* 1.5 lb package as a Meal would
corrupt today's totals (the sum is authoritative and code-owned). So the model was right
not to `log_meal` the pre-cooking photo; it just had nowhere else sanctioned to put the
weight.

**Does pending-meal state exist?** Only in the legacy pipeline: `User.pending_photo_meal`
(a JSON blob of the nutrition pipeline's initial photo estimate, held for one
clarification round-trip, read in `orchestrator.py` only). It is single-purpose, cleared
on the next answer, and not read or written anywhere in the agent-loop path. Extending it
would mean wiring the new path into a legacy column scheduled for retirement — worse than
the alternative.

**Hypothesis that died:** "route pre-consumption food to `log_event` (it's day-scoped and
auto-expires, and TODAY'S EVENTS would surface it at eating time)." Attractive for the
same-day case, but wrong at the edges: food on hand is not bound to a day. Tenders
photographed Monday and eaten Tuesday would expire out of TODAY'S EVENTS at midnight and
reproduce the exact bug one day later. The trace must live until the *user's action*
(eating it) closes it, not until the clock does — that's a memory-fact lifecycle
(`remember` → later `update`/`invalidate`), not an event lifecycle.

**Conclusion:** per the spec's stated preference (pending-meal state if it exists, a fact
if not): the durable trace is a **fact via `remember`**. On consumption the model logs
the meal from the stored weight and updates/invalidates the fact. Category: `constraints`
is the least-bad fit ("what shapes their food options right now"); eviction can't touch
safety entries (`_enforce_caps` never targets `safety:true`), so allergies are not at
risk from these facts sharing the bucket.

## 3. Why didn't the existing "don't invent technical explanations" voice rule catch the confabulation?

**Because it does not exist.** The spec says a rule was added after the "glitchy
connection" incident; the repo says otherwise — surfacing the discrepancy per the
playbook rather than silently reconciling:

- `grep -ri glitch` across the working tree: no prompt hit.
- `git log --all --grep=glitch` and a grep of `prompts/` across all revisions: empty.
  `voice.md`'s full history (7 commits) contains no such rule.
- Nearest relatives, neither of which covers this: `coach.py:329` "Rule 3 — No
  hallucinated causes" is **legacy-pipeline only** and is about coaching causes (don't
  invent a backstory for bad sleep), not the model's own I/O; `voice.md`'s manage_log
  block ("never claim you did") covers claiming *actions*, not explaining *gaps*.

So the question "does the rule not cover retrieval gaps, or was it not applied?" resolves
to: **there was no rule to apply.** Fix 2 is a new voice.md rule, written to cover the
whole class (invented technical causes for the model's own behavior, with retrieval gaps
as the named instance), not a patch to an existing line.

## 4. What does the conversation window retain about a past image?

Effectively nothing, and nothing durable:

- The inbound Message row stores only the caption text (`log_incoming(user.id, body)`).
  An image sent with no caption logs an **empty body** — the RECENT CONVERSATION render
  shows a blank user line. There is no marker that media was ever attached.
- The image bytes exist only inside the live turn's API call; they never reach any row.
- The only trace after the turn is *incidental*: the coach's own outbound reply (logged
  by `send_sms`), which may or may not quote the detail, and survives only within
  `CONVERSATION_HISTORY_LIMIT` (50 messages) and until the watermark summarizer
  compresses past it.

So "I saw a package photo earlier but didn't save the weight" is **not reliably
expressible** — the model can't distinguish "no image ever arrived" from "an image
arrived and I didn't save its contents." That ambiguity is part of what licensed the
confabulation. Minimal code fix: when media is attached, the logged inbound body carries
a deterministic ` [image attached]` marker (code-owned, at `log_incoming`), so the window
itself testifies that an image was received even after its contents are gone.

---

## Wrong hypotheses, recorded

1. **"The tenders fact was written but evicted"** (the schedule-facts failure mode) —
   no: nothing was ever written; the drop is upstream of memory entirely.
2. **"`log_event` is the right home for pre-consumption food"** — no: it expires with
   the day; food on hand doesn't (see §2).
3. **"Fix 2 extends an existing rule"** — no: the rule the spec references was never
   landed in this repo (see §3).
4. **"The honesty failure needs a code guard"** — not here: the §V code-guard idea
   (state-change language must co-occur with tool success) is about *claimed actions*;
   "nothing came through" is a claimed *cause*, which no output filter can cheaply
   detect. It stays a prompt rule, verified by the tier-2 honesty case.

## What this bounds

- No new tables, no new tools, no schema change. Fix 1 is `voice.md` routing + the
  `remember` tool description + a one-line inbound-marker change; fix 2 is a `voice.md`
  rule. All behind the existing flags (`READ_IMAGE_ENABLED`, `REMEMBER_TOOL_ENABLED`,
  …); the legacy fallback path is untouched.
- Sleep stays out of scope: a sleep screenshot is just another loose-fact image and
  persists via the same `remember` branch — no subsystem.
- Model *compliance* with the new routing is tier-2 ground (live tenders replay +
  retrieval-honesty case); tier-1 pins the deterministic parts (marker, write paths,
  regressions).
