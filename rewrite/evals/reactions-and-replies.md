# Reactions + threaded replies — live eval

Generated 2026-09-11 19:32 UTC through the real coach loop (`claude-opus-4-8`), tools react_to_message + reply_in_thread offered, sidecar mocked.

Founder's bar: react when a friend would, stay silent when a friend wouldn't, thread only where a paragraph would be ambiguous; a reaction never counts as unanswered.

## bare ack

**them:** Right right

**reaction:** 👍  
**text:** — (reaction only)  
**threaded:** no

mechanical: ✓ react · ✓ text · ✓ no thread · ✓ emoji fits · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS. 👍 on 'Right right', no text — rule 1 exactly. Two earlier runs leaked meta text ('No text needed.'); the [silent] sentinel + swallow fixed it.

## ack + sign-off

**them:** Alr alr / I'm on it bro

**reaction:** 👍  
**text:** — (reaction only)  
**threaded:** no

mechanical: ✓ react · ✓ text · ✓ no thread · ✓ emoji fits · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS. 👍, no text. One earlier run wrote 'react_to_message 👍' AS the text (low-effort Opus writing the tool call into prose); the leaked-call guard now executes it and never sends it.

## funny story

**them:** No yeah the late night gym sesh was great / I was feeling tired at first / And I was already lwk debating not going / But then one of the brothers at the frat had already told me he wanted to go / So I texted him if he was still down since his class ended at 9pm / And he was like "yeah im ready lets go" / So I was like "alr wtv, might as well" / Even tho I had to study for my quiz ngl 😬

**reaction:** 😂  
**text:** that accountability buddy thing works every time. but wait, how'd the quiz go if you traded study time for the gym 😬  
**threaded:** no

mechanical: ✓ text · ✓ no thread · ✓ emoji fits · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS. 😂 on the story, then the accountability text ('how'd the quiz go if you traded study time') — rule 2. Reaction choice varied across runs (‼️ once, none once, 😂 here); all acceptable under 'earned'.

## nerves

**them:** Hopefully I do good

**reaction:** —  
**text:** you've got this. go show it what you know.  
**threaded:** no

mechanical: ✓ text · ✓ no thread · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS with a note. No reaction, one warm line. Run 1 TEXTED a bare ❤️ — a tapback that lost its way; the lone-emoji conversion now turns that into the reaction. A ❤️ + text would also have been right here.

## multi-topic burst

**them:** Lowkey I get mostly meat tho, some greens, but I'm tryna get my moneys worth ngl / It's kinda expensive ngl / Like 25 dollars per bowl / Also I'm a junior so I don't got the dining hall pass like freshman's do / Rn prolly like 3-4 days a week for the gym / It used to be 5-6, but lowkey im getting more busy

**reaction:** —  
**text:** $25 a bowl is rough, that adds up fast. what spot is this, chipotle or one of the poke places on southside?

3-4 days is honestly fine for fat loss + muscle if we structure it right. busy how, just heavier class load this sem?  
**threaded:** no

mechanical: ✓ text · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS with a note. Text only, engaged the $25 bowl. Did NOT thread (allowed) — none of the runs threaded any case, so reply_in_thread is under-used rather than over-used. Watch on the phone; if bursts read ambiguous, tighten the thread rule's trigger. It also didn't know 'malatang' from a fresh synthetic user — fine.

## a question

**them:** Why does everyone think that c104 means data science?

**reaction:** —  
**text:** lol because it's literally cross-listed under Data — Data/History C104, the human contexts and ethics one. the "data" in the name does all the work even though half of it is history and philosophy. what's making it come up rn  
**threaded:** no

mechanical: ✓ react · ✓ text · ✓ no thread · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS after the fix. Run 3 laugh-reacted to 'why does everyone think c104 means data science?' — rule 3 is categorical, so it's now enforced in code: any '?' in the target refuses the tapback on every path.

## correction

**them:** Hmm, 2450 sounds kinda high for losing fat no?

**reaction:** —  
**text:** fair question, but that number's built for your recomp goal, not straight fat loss. you're trying to hold muscle while the fat comes off, so we don't gut the calories — too steep and you lose gains and the scale stalls anyway.

give it a real week of eating to that and hitting protein. if the weight's not budging we tune it after actual data, not a guess.  
**threaded:** no

mechanical: ✓ react · ✓ text · ✓ no thread · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS. No reaction, explained the number, and — after the new voice rule — did NOT invent a replacement target (run 1 announced '~2050 and 140g', the onboarding bug reappearing in the coach loop).

## workout without detail

**them:** hit pull

**reaction:** 👍  
**text:** how'd it go? what'd you pull today  
**threaded:** no

mechanical: ✓ no thread · ✓ emoji fits · ✓ ≤1 reaction · ✓ never a strike

hand review: PASS. 👍 AND asked for the rundown — the founder's explicit workout case: the reaction replaces the text, never the action; a log that needs numbers is a question → text.


---

## Mechanical floor, every case, every run
After each turn: `increment_unanswered` left the count unchanged unless a TEXT went out, and
`has_unanswered_outbound` was False after every reaction-only turn — the rule that bites held.

## Tuning log
1. Run 1 (7 fixes shy): reaction-only turns leaked 'Turn ended with reaction, no text needed.' as the text; a bare ❤️ was TEXTED instead of reacted; the coach loop invented '~2050 and 140g' on the calorie pushback.
2. Added the [silent] sentinel + meta swallow, lone-emoji → tapback conversion, and the 'targets are set in code' voice rule.
3. Run 2: 'react_to_message 👍' written as prose → leaked-tool-call guard executes it, never sends it.
4. Run 3: 😂 on a rhetorical question → rule 3 enforced in code on every path.
5. Run 4: 8/8.
Threaded replies never fired across four runs — under-used, not over-used. Phone judgment needed.
