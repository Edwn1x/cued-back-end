"""
GSM-7 SMS encoding normalization.

Background:
  SMS defaults to GSM-7: 160 chars / single segment, 153 / segment when
  concatenated. The moment a body contains any character outside the GSM-7
  set, the whole message re-encodes as UCS-2 (Unicode), which drops capacity
  to 67 chars / segment. US carriers cap concatenated SMS at ~10 segments,
  so an em-dash inside a long onboarding body can push it past that ceiling
  and the carrier rejects it (Twilio error 30019).

This module is pure and dependency-free so it's trivially unit-testable and
rail-agnostic. The Twilio dispatch path in sms.py:send_sms calls
normalize_for_sms() as the last body transform before split + send. When a
future rail (e.g. Linq) lands post-beta, it inherits the same normalized
body for free by going through the same dispatch seam.

Why not rely solely on Twilio's Smart Encoding (the Messaging Service
toggle)?
  1. Current outbound sends bypass the Messaging Service (they use
     from_=TWILIO_PHONE_NUMBER, not messaging_service_sid=...), so the
     server-side toggle does nothing today.
  2. Even routed through the service, Smart Encoding doesn't handle emojis
     and gives no delivery observability — we still need the residual /
     segment-count signal.
  3. Code-side normalization is unit-testable; we know exactly what
     reaches the carrier.

What this module does NOT do:
  - It does not change message content beyond the character map below
    (no rewording, no summarization, no length capping).
  - It does not split messages into multiple sends. split_message() in
    sms.py handles the `---` delimiter; normalization runs before that.
  - It does not translate emojis. Emojis have no GSM-7 equivalent and are
    intentionally left in the body so residual_non_gsm() can surface them.
"""

# GSM-7 basic + extension character sets (3GPP TS 23.038 §6.2.1).
# Everything in these two strings encodes to GSM-7. Extension chars cost
# 2 septets (1 escape + 1 char) instead of 1.
_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXT = "^{}\\[~]|€"
_GSM_CHARS = set(_GSM_BASIC) | set(_GSM_EXT)

# Non-GSM characters the coach LLM commonly emits → GSM-7 equivalents.
# This map covers the offenders we've seen in production. Emojis are NOT
# mapped — they have no GSM-7 equivalent, so they're flagged by
# residual_non_gsm() instead and surfaced via warning log.
_TRANSLITERATE = {
    # Em dash / en dash → hyphen
    "—": "-",   # — (EM DASH)
    "–": "-",   # – (EN DASH)
    # Smart single quotes → straight apostrophe
    "’": "'",   # ’ (RIGHT SINGLE QUOTATION MARK)
    "‘": "'",   # ‘ (LEFT SINGLE QUOTATION MARK)
    # Smart double quotes → straight double quote
    "“": '"',   # “ (LEFT DOUBLE QUOTATION MARK)
    "”": '"',   # ” (RIGHT DOUBLE QUOTATION MARK)
    # Ellipsis → three dots
    "…": "...", # … (HORIZONTAL ELLIPSIS)
    # Non-breaking space → regular space
    " ": " ",   #   (NO-BREAK SPACE)
    # Bullet → hyphen
    "•": "-",   # • (BULLET)
}
_TRANS_TABLE = str.maketrans(_TRANSLITERATE)


def normalize_for_sms(text: str) -> str:
    """Transliterate common non-GSM-7 chars to GSM-7 equivalents.

    Pure function. Does not modify length-related behavior or split bodies;
    just substitutes the characters in _TRANSLITERATE. Anything not in the
    map passes through unchanged (including emojis — those are surfaced by
    residual_non_gsm()).
    """
    if not text:
        return text
    return text.translate(_TRANS_TABLE)


def residual_non_gsm(text: str) -> list[str]:
    """Return sorted unique characters still outside GSM-7 after normalization.

    Empty list means the body will dispatch as GSM-7. A non-empty list (e.g.
    an emoji) means UCS-2 will still be used and segment capacity drops to
    67 chars/segment. Callers should log this as a UCS-2 warning.
    """
    return sorted({c for c in (text or "") if c not in _GSM_CHARS})


def estimate_segments(text: str) -> tuple[str, int]:
    """Return (encoding, segment_count) for an already-normalized body.

    Encoding is "GSM-7" if every character is in the GSM-7 set, otherwise
    "UCS-2". Segment count uses the official 3GPP capacities:
      - GSM-7: 160 (single) / 153 (multi-part).
      - UCS-2: 70 (single) / 67 (multi-part).

    GSM-7 extension characters (e.g. `{`, `}`, `€`) cost 2 septets each;
    the unit count accounts for that. UCS-2 capacity is computed against
    BMP characters (1 unit / char), matching how Twilio bills segments.
    Surrogate-pair emojis (e.g. 🏋️) actually cost 2 UCS-2 units each, so
    this estimate is a lower bound when the body contains them — but the
    spec's primary goal is to land in GSM-7 anyway, so this approximation
    is fine for the warning threshold.
    """
    if not text:
        return "GSM-7", 1
    is_gsm = all(c in _GSM_CHARS for c in text)
    if is_gsm:
        units = sum(2 if c in _GSM_EXT else 1 for c in text)
        single, multi = 160, 153
    else:
        units = len(text)  # UCS-2 BMP: 1 unit/char (see surrogate caveat above)
        single, multi = 70, 67
    enc = "GSM-7" if is_gsm else "UCS-2"
    if units <= single:
        return enc, 1
    # Ceiling division: -(-x // y) without importing math.
    return enc, -(-units // multi)
