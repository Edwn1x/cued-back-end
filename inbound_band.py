"""
Inbound buffer band by text shape (founder 2026-11-20: "takes forever to reply… but we
don't want to miss texts either").

The buffer exists to catch a double-text. 45 days of prod iMessage turns (2026-11-20):

    first text shape          turns   got a follow-up   p50 gap
    ends in ?                    65         22%           8.8s
    ends in . or !               15         13%          25.4s
    ≤2 words, no punctuation    332         26%           6.6s
    3–6 words, no punctuation   264         34%           6.6s
    7+ words, no punctuation    259         28%          10.2s

A text that reads COMPLETE — a question, a direct ask, terminal punctuation — is the one
least likely to be the first half of a thought, so it waits the shorter
REPLY_BUFFER_COMPLETE_S. Everything else (fragments, bare acks, trailing conjunctions,
an ellipsis) keeps REPLY_BUFFER_S. A follow-up that lands after the short band flushes
is NOT lost: it becomes a continuation turn (message_buffer), the model sees the reply
that already went out, and answers only what's new (or 👍s if it was covered).

Photos and onboarding are decided in app.py and never come through here.
"""
from __future__ import annotations

import re

import config

# A direct ask / imperative that is a whole request on its own.
_DIRECT_ASK = re.compile(
    r"^(?:(?:hey|yo|ok|okay|also|and|so|wait|quick q|question)[,!.\s]+)?"
    r"(?:what|whats|what's|what’s|how|when|where|which|why|who|can|could|would|will|"
    r"do|does|did|is|are|am|should|wyd|wya|send|show|gimme|give me|get me|text me|"
    r"pull up|log|add|move|cancel|delete|remind|set|make|build|change|swap|"
    r"tell me|lmk|let me know)\b",
    re.IGNORECASE,
)
# Trailing words that say "more is coming".
_DANGLING = re.compile(
    r"\b(?:and|but|or|so|then|because|cuz|cause|since|also|plus|like|with|for|to|the|a|an|my|"
    r"i|im|i'm|it|its|it's|that|this|when|if|at|in|on|of)$",
    re.IGNORECASE,
)
# Closing acks / one-word reactions: ambiguous on their own ("yeah" → "I did legs").
_BARE = re.compile(r"^(?:ok|okay|k|kk|ya|yea|yeah|yep|yup|nah|no|nope|sure|bet|lol|lmao|haha|ha|"
                   r"wait|hmm|hm|oh|ohh|true|fr|facts|word|ight|aight|alr|cool|nice|same|"
                   r"yes|y|n|ty|thx|thanks|np|wow|damn|ugh|idk|mb|rip)[.!]*$", re.IGNORECASE)


def looks_complete(text: str) -> bool:
    """True when the text reads as a whole thought: a question, a direct ask, or a
    sentence with terminal punctuation. False for fragments, trailing conjunctions,
    an ellipsis, bare acks, and anything empty."""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("...") or t.endswith("…"):
        return False
    if _BARE.match(t):
        return False
    if t.endswith("?"):
        return True
    words = t.split()
    if _DANGLING.search(t):
        return False
    if t[-1] in ".!" and not t.endswith(".."):
        return True
    if len(words) <= 1:
        return False
    if _DIRECT_ASK.match(t):
        return True
    return False


def pick_reply_band(text: str) -> tuple:
    """The post-onboarding text band: REPLY_BUFFER_COMPLETE_S for a complete-looking text
    when ADAPTIVE_REPLY_BAND_ENABLED, else REPLY_BUFFER_S."""
    if getattr(config, "ADAPTIVE_REPLY_BAND_ENABLED", True) and looks_complete(text):
        return config.REPLY_BUFFER_COMPLETE_S
    return config.REPLY_BUFFER_S
