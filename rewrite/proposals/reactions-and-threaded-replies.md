# Reactions + threaded replies — proposed rules (for the founder's read before wiring)

Mechanics are done in the sidecar (`POST /react {phone, message_id, emoji}`, `reply_to` on
`/send`). This is the part that decides whether it feels like a friend or a bot: **when**.

## Reactions (tapbacks)

The six iMessage tapbacks by name: love ❤️, like 👍, dislike 👎, laugh 😂, emphasize ‼️,
question ❓. Rules, in priority order:

1. **React INSTEAD of texting when the only honest reply is an acknowledgment.** "Right right",
   "Ok cool, looks good", "Yeah ok", "Alr alr", "I'm on it bro" → 👍 and nothing else. Today each
   of those got a full text back. A friend thumbs-ups.
2. **React AND text when something deserves warmth before the coaching point.** The frat-brother
   story → 😂 on the message, then the accountability text. "Hopefully I do good" → ❤️, then the
   one line. Warmth is where the annoying-bot risk lives, so the reaction has to be earned by
   something specific in the message, never a habit.
3. **Never react to a question, an injury, a correction, or anything that needs an answer.** A ❓
   tapback is banned outright — it reads as sarcasm from a coach.
4. **At most ONE reaction per user turn**, on the single message that earned it. A burst of eight
   texts gets one tapback, not eight.
5. **👎 only when they'd expect it and laugh** — "i ate a whole pizza at 2am" from someone who's
   joking. Never on a real slip. When in doubt, no reaction.
6. **No reaction during the first three exchanges of onboarding.** Let them learn the coach texts
   like a person before it starts reacting like one.

## Threaded replies

Reply on a specific bubble only when the thread would otherwise be ambiguous:

1. **A burst with two or more distinct topics** ("malatang is $25" / "I'm a junior, no meal plan" /
   "3-4 days a week for the gym") → thread the reply on the one you're answering, not a paragraph
   that covers all three.
2. **Answering something from earlier in the conversation** that isn't the latest message → thread
   on it so they know which thing you mean.
3. **Never thread on the latest message when it's the only topic** — a threaded reply to the text
   right above it is noise.
4. **Never thread a coaching call-out.** "you've skipped twice this week" is a conversation, not a
   quote-reply.

## Gating

- Tools are offered only when the user's resolved channel is iMessage (same gate as web search;
  a tripped breaker = no tools). SMS users never see them.
- Both tools take a short message ref from context (the last ~10 inbound messages carry refs);
  code maps ref → stored Photon id → `space.getMessage`.
- A reaction is logged as its own outbound row (`channel=imessage`, `message_type=reaction`) so
  the engagement tracker and the history window see it; the keystone rules apply (a failed
  reaction is not a strike).

## Eval (before groupmates)

Replay eight real bursts from 2026-09-11 (the acks, the frat story, the malatang burst,
"hopefully I do good", the calorie pushback, a question, the "not a morning person 😭") through
the loop with the tools offered. Hand-read: did it react when a friend would, stay silent when a
friend wouldn't, and thread only where a paragraph would have been ambiguous. Binary: 8/8 or
keep tuning.

## Open for the founder

- Should the coach react when the user answers ITS question well (e.g. ❤️ when they say they hit
  the gym)? My lean: yes — that's rule 2.
- A 👍 on "hit pull" replaces the TEXT, not the action: log_workout still fires. Agree?
