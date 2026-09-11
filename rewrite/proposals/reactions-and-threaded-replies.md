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
   something specific in the message, never a habit. **‼️ is the hype tapback** — a PR, the first
   workout after a gap, a streak, a hard day they showed up anyway. That's where a friend uses it
   and it's the most coach-native of the six.
   **Earned, not compliance:** when they answer the coach's question well, react only if the
   answer itself is something — first gym session, a streak, a PR, showing up on a brutal day
   (❤️ or ‼️). A routine "hit the gym" on a Tuesday gets nothing, or a text if there's a coaching
   point. A ❤️ on every completed task is a participation trophy and they'll feel it by day four.
3. **Never react to a question, an injury, a correction, or anything that needs an answer.** A
   question gets a text. **The workout case, explicitly:** "hit pull" → 👍 AND log_workout still
   fires (the reaction replaces the TEXT, never the action). But if the log needs a detail the
   message didn't give — weights, sets, which day — that makes it a question, so it's a text, not
   a tapback.
4. **At most ONE reaction per user turn**, on the single message that earned it. A burst of eight
   texts gets one tapback, not eight.
5. **👎 only when they'd expect it and laugh** — "i ate a whole pizza at 2am" from someone who's
   joking. Never on a real slip. When in doubt, no reaction.

No tapback is banned. A friend reacts from the first exchange — there is no warm-up period; the
over-reaction risk is handled by rule 4 and by "earned," not by holding back.

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
  the history window sees it and the coach doesn't re-ack. **A reaction NEVER counts as an
  unanswered outbound.** This is the one that bites: rule 1 exists to close loops, and if the
  engagement tracker sees the 👍 as an outbound the user didn't reply to, closure becomes a
  strike and a user who thumbs-ups back and forth with the coach reads as churning. Reaction
  rows are excluded by `message_type=reaction` in EVERY place that counts silence:
  `increment_unanswered`, `has_unanswered_outbound`, `has_unanswered_proactive`, and the
  heartbeat's unanswered gap. A failed reaction is not a strike either (keystone), and it does
  not trip the channel breaker — a stale message id is not a channel failure.

## Eval (before groupmates)

Replay eight real bursts from 2026-09-11 (the acks, the frat story, the malatang burst,
"hopefully I do good", the calorie pushback, a question, the "not a morning person 😭") through
the loop with the tools offered. Hand-read: did it react when a friend would, stay silent when a
friend wouldn't, and thread only where a paragraph would have been ambiguous. Plus one
mechanical check per case: **after a reaction-only turn, the user's unanswered count and every
silence gate are unchanged.** Binary: 8/8 or keep tuning.

## Decided (founder, 2026-09-11)

- Reactions never count as unanswered outbound — see Gating; in the eval too.
- No banned reactions.
- No onboarding warm-up period (old rule 6 dropped).
- ‼️ is the hype tapback (rule 2).
- React to a good answer only when it's earned by the content, never by compliance.
- 👍 on "hit pull" still logs; a log that needs a missing detail is a question → text.
