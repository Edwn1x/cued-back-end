"""
Tone Analyzer — Cued
=====================
Two-layer adaptive tone system:

Layer 1 — Profile-based default
    Uses age and occupation to set a starting tone before any exchanges happen.
    Younger (18-25): direct, energetic, lean into slang naturally
    Mid (26-54): warm, straightforward, peer-level
    Older (55+): encouraging, clear, no slang, slightly more formal

Layer 2 — Mirror the user (after 4+ replies)
    Analyzes the user's last 5-8 incoming messages for:
    - Capitalization habits (all lowercase, proper, mixed)
    - Sentence length (short fragments vs full sentences)
    - Abbreviation/slang usage
    - Punctuation style
    Updates user.communication_style with a short descriptor string
    that gets injected into every system prompt.
"""

import re
from models import get_session, User, Message


# ── Layer 1: Profile-based default ──────────────────

def default_tone_from_profile(user: User) -> str:
    """Return a tone descriptor based on age and occupation."""
    age = user.age or 30  # default to mid-range if unknown

    occupation = (user.occupation or "").lower()
    is_student = any(kw in occupation for kw in ["student", "college", "university", "school", "undergrad", "grad"])
    is_trade = any(kw in occupation for kw in ["barber", "mechanic", "construction", "electrician", "plumber", "driver"])
    is_professional = any(kw in occupation for kw in ["engineer", "doctor", "lawyer", "manager", "analyst", "executive", "director"])

    if age <= 25:
        base = "direct and energetic — use natural slang when it fits, keep it punchy, short sentences"
        if is_student:
            base += ", relate to the grind of school + training"
        elif is_trade:
            base += ", respect the physical work they already do, zero fluff"
        return base

    elif age <= 54:
        base = "warm and peer-level — treat them like a capable adult with a busy life, no hype"
        if is_professional:
            base += ", they appreciate precision and data over motivation, be efficient"
        return base

    else:  # 55+
        return (
            "encouraging and clear — no slang, no abbreviations, slightly more formal. "
            "They've earned respect. Be warm but don't be patronizing. Full words, not fragments."
        )


# ── Layer 2: Mirror from message history ────────────

def analyze_user_style(user_id: int, min_messages: int = 4) -> str | None:
    """
    Analyze the user's recent incoming messages and return a style descriptor.
    Returns None if not enough messages yet.
    """
    session = get_session()
    try:
        messages = (
            session.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "in")
            .order_by(Message.created_at.desc())
            .limit(8)
            .all()
        )

        if len(messages) < min_messages:
            return None

        bodies = [m.body for m in messages if m.body]

        # Lowercase tendency
        total_chars = sum(len(b) for b in bodies)
        lower_chars = sum(sum(1 for c in b if c.islower()) for b in bodies)
        upper_chars = sum(sum(1 for c in b if c.isupper()) for b in bodies)
        lowercase_ratio = lower_chars / max(total_chars, 1)
        mostly_lowercase = lowercase_ratio > 0.92 and upper_chars < 5

        # Average message length
        avg_len = total_chars / len(bodies)
        terse = avg_len < 15
        verbose = avg_len > 60

        # Abbreviation/slang signals
        abbrev_patterns = [r'\bu\b', r'\bur\b', r'\bidk\b', r'\blol\b', r'\bomg\b',
                           r'\bngl\b', r'\bfr\b', r'\bbtw\b', r'\bimo\b', r'\bhbu\b',
                           r'\bwdym\b', r'\bthx\b', r'\bplz\b', r'\bnvm\b']
        abbrev_count = sum(
            1 for b in bodies
            for p in abbrev_patterns
            if re.search(p, b, re.IGNORECASE)
        )
        uses_abbrevs = abbrev_count >= 2

        # Full sentence signals (ends with punctuation, has subject+verb structure roughly)
        punctuated = sum(1 for b in bodies if b.strip().endswith(('.', '!', '?')))
        uses_full_sentences = punctuated >= len(bodies) * 0.5 and not mostly_lowercase

        # Build descriptor
        parts = []

        if mostly_lowercase and uses_abbrevs:
            parts.append("mirror their casual lowercase texting style and abbreviations (u, ur, idk, etc.)")
        elif mostly_lowercase:
            parts.append("match their relaxed all-lowercase style")
        elif uses_full_sentences:
            parts.append("match their more formal full-sentence style — write in complete sentences")

        if terse:
            parts.append("keep responses very short — they text in fragments, match that energy")
        elif verbose:
            parts.append("they write more, so slightly longer responses are fine")

        if uses_abbrevs:
            parts.append("abbreviations are welcome — they use them naturally")

        if not parts:
            return None  # no strong signal yet, keep default

        return "; ".join(parts)

    finally:
        session.close()


# ── Update stored style ──────────────────────────────

def maybe_update_style(user_id: int):
    """
    After enough exchanges, analyze the user's style and persist it.
    Called from the webhook after each incoming message.
    Only updates if a new style is detected (avoids unnecessary DB writes).
    """
    style = analyze_user_style(user_id)
    if style is None:
        return

    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if user and user.communication_style != style:
            user.communication_style = style
            session.commit()
    finally:
        session.close()


# ── Build the full tone instruction ─────────────────

def get_tone_instruction(user: User) -> str:
    """
    Return the complete tone instruction to inject into the system prompt.
    Combines the profile-based default with any learned mirroring style.
    """
    layer1 = default_tone_from_profile(user)

    if user.communication_style:
        return (
            f"## ADAPTIVE TONE\n"
            f"Base tone (from profile — age {user.age}, {user.occupation or 'no occupation listed'}): {layer1}\n"
            f"User's actual texting style (mirror this): {user.communication_style}\n"
            f"When these conflict, the mirroring style wins — always match how they actually text."
        )
    else:
        return (
            f"## ADAPTIVE TONE\n"
            f"Base tone (from profile — age {user.age}, {user.occupation or 'no occupation listed'}): {layer1}\n"
            f"(Not enough exchanges yet to mirror their style — stick to the base tone for now.)"
        )
