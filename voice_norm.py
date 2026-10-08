"""voice_norm — the lowercase friend voice, enforced in code.

identity.md: "Lowercase default. Capitalize for emphasis only." Live 2026-10-05/06 (user 48),
all from the normal agent_loop path: "What", "Nice. what's the hook", "Nice. go film it",
"Yeah, you're at 980 for the day, 35g protein." — a sentence-initial capital (and the
pronoun I) slips in whenever the model drifts to a formal register. This pass lowers ONLY
what the voice would lower anyway:
  • the first letter of the reply and of each sentence (after . ! ? or a newline) when
    that word is an ordinary Capitalized word ("Yeah", "Nice", "What") — never an acronym
    (RSF, HW4), never a word with digits, never a URL;
  • the pronoun I and its contractions (I'm, I'll, I've, I'd) anywhere.
Everything else (proper nouns mid-sentence, ALLCAPS emphasis, numbers, links) is untouched.
"""
from __future__ import annotations

import re

_SENTENCE_START_RE = re.compile(r"(^|[.!?]\s+|\n\s*)([A-Z][a-z]*(?:'[a-z]+)?)(?=\b)")
_PRONOUN_I_RE = re.compile(r"\bI(?=(?:'(?:m|ll|ve|d)\b)|\b)")
_URL_RE = re.compile(r"https?://\S+|\S+\.(?:fit|com|edu|org|net)(?:/\S*)?", re.I)


def lowercase_lead(text: str) -> str:
    """The lowercase friend voice applied to a reply. Idempotent; '' → ''."""
    if not text:
        return text
    # keep URLs byte-for-byte: lower them out of the way, restore after
    urls: list[str] = []

    def _hold(m):
        urls.append(m.group(0))
        return f"\x00{len(urls) - 1}\x00"
    t = _URL_RE.sub(_hold, text)

    def _lower_start(m):
        word = m.group(2)
        if word == "I":               # the pronoun is handled below (contractions too)
            return m.group(0)
        return m.group(1) + word[0].lower() + word[1:]
    t = _SENTENCE_START_RE.sub(_lower_start, t)
    t = _PRONOUN_I_RE.sub("i", t)
    for i, u in enumerate(urls):
        t = t.replace(f"\x00{i}\x00", u)
    return t
