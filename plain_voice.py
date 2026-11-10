"""plain_voice — standard English for the people who don't text in slang.

identity.md is a Berkeley friend: lowercase, "u / ur / rn / ngl", the whole register. Right for
a 19-year-old on campus, wrong for a 67-year-old the founder met at a print shop (live
2026-11-09, user 94). Founder: "for older folks, like robert, we drop the whole slang spelling
and all of that. we type out normally and text like a normal regular person."

One gate, two halves:
  • plain_voice(user): users.plain_voice set by hand wins (True / False); otherwise a sign-up
    age at or past PLAIN_VOICE_MIN_AGE. PLAIN_VOICE_ENABLED=false turns the whole thing off.
  • The model is TOLD (prompt_block → every system prompt that carries per-user context), and
    everything that reaches sms.send_sms for that user is RE-SPELLED in code (plainify):
    u → you, ur → your / you're, rn → right now, im → I'm, and a capital letter opens each
    sentence. The code pass is what covers the hundreds of code-owned templates (the kickoff,
    the setup steps, the pings, the briefing) without a second copy of each; the prompt is
    what keeps the model from writing slang the pass doesn't know.
Warmth, length, one question at a time: unchanged. This is spelling and register, not who the
coach is.
"""
from __future__ import annotations

import logging
import re

import config

logger = logging.getLogger(__name__)


# ─── the gate ────────────────────────────────────────────────────────────────

def plain_voice(user) -> bool:
    """Plain English for this person? A hand-set users.plain_voice decides; else their age."""
    if not getattr(config, "PLAIN_VOICE_ENABLED", True):
        return False
    override = getattr(user, "plain_voice", None)
    if override is not None:
        return bool(override)
    cap = int(getattr(config, "PLAIN_VOICE_MIN_AGE", 40) or 0)
    age = getattr(user, "age", None)
    return bool(cap) and isinstance(age, int) and age >= cap


def plain_voice_for(user_id) -> bool:
    """The gate from a user id (sms.send_sms has no row in hand). Never raises."""
    if not user_id or not getattr(config, "PLAIN_VOICE_ENABLED", True):
        return False
    try:
        from models import get_session, User
        session = get_session()
        try:
            row = session.query(User.age, User.plain_voice).filter(User.id == user_id).first()
        finally:
            session.close()
    except Exception as e:  # noqa: BLE001 — a lookup failure must never block a send
        logger.warning("PLAIN_VOICE_LOOKUP_FAILED user=%s err=%s", user_id, e)
        return False
    if not row:
        return False

    class _U:
        age, plain_voice = row[0], row[1]
    return plain_voice(_U)


# ─── what the model is told ──────────────────────────────────────────────────

def prompt_block(user) -> str | None:
    """The per-user system block that switches the register. None when the gate is off."""
    if not plain_voice(user):
        return None
    age = getattr(user, "age", None)
    who = f"They are {age}" if isinstance(age, int) and age else "They are a grown adult"
    return (
        "## VOICE FOR THIS PERSON — PLAIN ENGLISH, NOT THE CAMPUS REGISTER\n"
        f"{who} — an adult, not a college kid, and they text like one. Write the way a thoughtful "
        "adult texts a friend their own age: full words, standard spelling, a capital letter at the "
        "start of each sentence, normal punctuation. The abbreviations identity.md tells you to use "
        "(u, ur, r, rn, ngl, tbh, idk, ig, lw, lwk, atp, fr, wyd, wya, mb, alr, lmk, tmr) and the "
        "lowercase default are OFF for them: \"you\", \"your\", \"right now\", \"honestly\", \"I don't "
        "know\", \"alright\", \"let me know\", \"tomorrow\". No \"bruhh\", no \"lol\", no \"hella\", no "
        "\"gonna\" / \"wanna\" / \"ima\". Contractions are fine (it's, you're, don't). Everything else "
        "about how you talk holds — the same warmth, the same shortness, plain words, one question at a "
        "time, no rating what they said, and never corporate: \"Great question!\", \"I apologize\", "
        "\"Hi Robert!\" with the exclamation point are all wrong too. Think: a friend in their forties "
        "who knows training cold, texting back between things."
    )


# ─── what the code re-spells ─────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://\S+|\b[\w.-]+\.(?:fit|com|edu|org|net|codes|io|app)(?:/\S*)?|\S+@\S+\.\S+", re.I)

# Whole-word swaps. Keys are lowercase; a Capitalized original keeps its capital ("U eat yet"
# → "You eat yet"); an ALLCAPS word is emphasis or an acronym and is left alone.
_WORDS = {
    "u": "you", "r": "are", "rn": "right now", "tmr": "tomorrow", "tmrw": "tomorrow", "tmrrw": "tomorrow",
    "tmmrw": "tomorrow", "ngl": "honestly", "tbh": "honestly", "idk": "I don't know", "ig": "I guess",
    "lw": "a little", "lwk": "a little", "lowkey": "a little", "atp": "at this point", "fr": "really",
    "tho": "though", "wyd": "what are you up to", "wya": "where are you", "mb": "my bad", "alr": "alright",
    "lmk": "let me know", "hella": "really", "gonna": "going to", "wanna": "want to", "gotta": "have to",
    "ima": "I'm going to", "imma": "I'm going to", "yea": "yeah", "nah": "no", "bc": "because",
    "cuz": "because", "cus": "because", "prolly": "probably", "abt": "about", "rly": "really",
    "srsly": "seriously", "pls": "please", "plz": "please", "thx": "thanks", "ppl": "people",
    "msg": "message", "nvm": "never mind", "omw": "on my way", "smth": "something", "ik": "I know",
    "jk": "just kidding", "np": "no problem", "yk": "you know", "ps": "P.S.", "urs": "yours",
    "im": "I'm", "ive": "I've", "ill": "I'll", "dont": "don't", "cant": "can't", "wont": "won't",
    "didnt": "didn't", "isnt": "isn't", "wasnt": "wasn't", "doesnt": "doesn't", "havent": "haven't",
    "hasnt": "hasn't", "wouldnt": "wouldn't", "shouldnt": "shouldn't", "couldnt": "couldn't",
    "arent": "aren't", "werent": "weren't", "whats": "what's", "thats": "that's", "theres": "there's",
    "hows": "how's", "wheres": "where's", "whos": "who's", "lets": "let's", "youre": "you're",
    "theyre": "they're", "youll": "you'll", "youve": "you've", "youd": "you'd", "hes": "he's",
    "shes": "she's", "i": "I",
}
# Lowercase proper nouns the voice writes flat. Small on purpose — a wrong capital reads worse
# than a missing one.
_PROPER = {
    "berkeley": "Berkeley", "rsf": "RSF", "bcourses": "bCourses", "imessage": "iMessage",
    "iphone": "iPhone", "google": "Google", "monday": "Monday", "tuesday": "Tuesday",
    "wednesday": "Wednesday", "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday",
    "sunday": "Sunday", "january": "January", "february": "February", "march": "March", "april": "April",
    "june": "June", "july": "July", "august": "August", "september": "September", "october": "October",
    "november": "November", "december": "December",
}
_WORD_RE = re.compile(r"(?<![\w'’])([A-Za-z]+)(?![\w'’])")
_U_CONTRACTION_RE = re.compile(r"\b[uU](['’](?:re|ll|ve|d))\b")
_W_SLASH_RE = re.compile(r"\bw/(o)?(?=\s|$)")
# the pronoun, with its contractions (the word pass skips a token that runs into an apostrophe)
_PRONOUN_I_RE = re.compile(r"(?<![\w'’])i(?=['’](?:m|ll|ve|d)\b|\b)")
# "ur" is "you're" before a state ("ur in", "ur done", "ur a …", "ur making …") and "your"
# before a thing ("ur day", "ur calendar"). Default is "your"; this list is the exceptions.
_URE_NEXT = re.compile(
    r"^(?:in|out|good|great|fine|ok|okay|not|at|on|done|up|down|here|there|right|wrong|welcome|all|"
    r"the|a|an|set|sick|serious|free|away|around|late|early|ahead|behind|over|under|low|high|hungry|"
    r"tired|sore|close|about|still|already|just|basically|probably|definitely|def|prolly|also|only|"
    r"really|so|too|very|pretty|gonna|going|supposed|allowed|able|ready|sure|solid|through|like|"
    r"exhausted|wiped|drained|stressed|sorted|covered|logged|locked|booked|good|better|worse|off|"
    r"\w+in')$", re.I)
_URE_ING_RE = re.compile(r"^\w+ing$", re.I)
_URE_NOT_ING = {"morning", "evening", "thing", "something", "nothing", "anything", "everything", "training",
                "programming", "timing", "building", "standing", "king", "string", "swing", "wing", "ring",
                "spring", "bring", "cooking"}
_UR_RE = re.compile(r"(?<![\w'’])([uU]r)(?![\w'’])(\s+([\w'’]+))?")
_SENTENCE_START_RE = re.compile(r"(^|[.!?…]\s+|\n\s*)([a-z])")


def _ur(m) -> str:
    cap = m.group(1)[0] == "U"
    nxt = m.group(3)
    if nxt is None:
        word = "you are"           # "idk where ur" → "where you are"
    elif (_URE_NEXT.match(nxt) or (_URE_ING_RE.match(nxt) and nxt.lower() not in _URE_NOT_ING)):
        word = "you're"
    else:
        word = "your"
    if cap:
        word = word[0].upper() + word[1:]
    return word + (m.group(2) or "")


def _word(m) -> str:
    w = m.group(1)
    if len(w) > 1 and w.isupper():
        return w                   # RSF, HW4-ish emphasis, OK as an acronym: not ours
    low = w.lower()
    rep = _WORDS.get(low)
    if rep is None:
        return _PROPER.get(low, w)
    if w[0].isupper() and rep[0].islower():
        rep = rep[0].upper() + rep[1:]
    return rep


def plainify(text: str) -> str:
    """The slang spelling → standard English. Idempotent; '' → ''. URLs, emails, ALLCAPS,
    numbers, emoji and the `---` bubble separators are untouched."""
    if not text:
        return text
    held: list[str] = []

    def _hold(m):
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"
    t = _URL_RE.sub(_hold, text)
    t = _U_CONTRACTION_RE.sub(lambda m: "you" + m.group(1), t)
    t = _W_SLASH_RE.sub(lambda m: "without" if m.group(1) else "with", t)
    t = _UR_RE.sub(_ur, t)
    t = _PRONOUN_I_RE.sub("I", t)
    t = _WORD_RE.sub(_word, t)
    t = _SENTENCE_START_RE.sub(lambda m: m.group(1) + m.group(2).upper(), t)
    for i, u in enumerate(held):
        t = t.replace(f"\x00{i}\x00", u)
    return t


def for_user(user, text: str) -> str:
    """plainify when the gate says so, else the text as written."""
    return plainify(text) if plain_voice(user) else text
