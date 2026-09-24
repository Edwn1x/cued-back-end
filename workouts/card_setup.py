"""
Card setup — how someone meets the workout card (founder, 2026-09-24).

Before this, nothing in the product said the card needs the Spectrum iMessage
extension: the site promises "no app", the first card landed as an unexplained
bubble, and the tap dead-ended on Apple's install sheet at the gym. The founder's
sister opened one and "didn't know what it was for or why it mattered".

Three code-sent pieces, each once, all flag-gated (CARD_SETUP_ENABLED):
  - EXTENSION_INTRO: right before their first card — it's an iMessage extension, the
    same kind of thing as GamePigeon, one tap to add, nothing on the home screen,
    they never leave Messages; texting sets still works, the card is the full
    version. Sent again only as the one-line EXTENSION_REMINDER while
    users.card_opened_at is still empty (the card page's first fetch sets it).
  - The card itself at onboarding completion (run_onboarding_setup), so the install
    happens at home and the numbers get checked before workout time. If nothing is
    known about their lifts, the first-card ask goes first (code-sent, by level) and
    the answer sends the card through the existing pending-card pre-pass.
  - BREAKDOWN: after the first card lands — what the blocks / rows / numbers are, how
    to tap, fix, add, swap and finish, and why it matters (next week's numbers come
    from what they actually lifted). users.card_explained_at.

SMS users see none of this (no extension, per-exercise texts as before).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger("cued.card_setup")

MESSAGE_TYPE = "card_setup"

EXTENSION_INTRO = (
    "last thing. ur workouts show up right here as a card u tap as u go",
    "it runs on a small imessage extension, same kind of thing as gamepigeon. one tap to add, "
    "nothing on ur home screen, and u never leave messages for any of it. without it u can "
    "still text me ur sets, the card's just the full version",
)
EXTENSION_REMINDER = ("heads up, the card needs that imessage extension (like gamepigeon, one tap to add). "
                      "or just text me ur sets")
BREAKDOWN = (
    "quick tour: each block is an exercise, each row is a set, weight × reps",
    "tap a row when u finish the set. number off? tap it, fix it, save. + set adds one, swap changes the exercise",
    "slide the bar at the bottom when ur done and i log the whole thing. that's how next week's "
    "numbers come from what u actually lifted, not guesses",
)
ASK_TRAINED = "before i build ur first card, what do u bench and squat for like 5?"
ASK_NEW = ("before i build ur first card, got any number at all? heaviest u've benched or squatted, "
           "even just the bar. 'no clue' is fine too")
REFUSED_LINE = "card didn't go through on my end. no stress, i'll text u the exercises when u lift"


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return bool(config.CARD_SETUP_ENABLED)


def _user(user_id: int):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, user_id)
    finally:
        s.close()


def _stamp(user_id: int, column: str) -> None:
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, user_id)
        if u is not None and not getattr(u, column, None):
            setattr(u, column, _naive_utcnow())
            s.commit()
    finally:
        s.close()


def send_extension_intro_if_due(user_id: int, phone: str) -> list[str]:
    """Before a card: the full framing the first time, the one-line reminder after
    that, nothing once the card has ever been opened. Returns what was sent."""
    if not enabled():
        return []
    u = _user(user_id)
    if u is None or getattr(u, "card_opened_at", None):
        return []
    from sms import send_sms
    lines = list(EXTENSION_INTRO) if not getattr(u, "card_setup_at", None) else [EXTENSION_REMINDER]
    for line in lines:
        send_sms(phone, line, user_id=user_id, message_type=MESSAGE_TYPE)
    _stamp(user_id, "card_setup_at")
    logger.info("CARD_SETUP_EXTENSION_SENT user=%s lines=%d", user_id, len(lines))
    return lines


def send_breakdown_if_due(user_id: int, phone: str) -> list[str]:
    """After the first card lands: the tour, once ever."""
    if not enabled():
        return []
    u = _user(user_id)
    if u is None or getattr(u, "card_explained_at", None):
        return []
    from sms import send_sms
    for line in BREAKDOWN:
        send_sms(phone, line, user_id=user_id, message_type=MESSAGE_TYPE)
    _stamp(user_id, "card_explained_at")
    logger.info("CARD_SETUP_BREAKDOWN_SENT user=%s", user_id)
    return list(BREAKDOWN)


def context_line(user) -> str | None:
    """The loop's WORKOUT CARD block. A card was sent and never opened → the extension
    isn't installed yet, and that is what 'it won't open' means (live 2026-09-24, 0/3:
    left to itself the model asked 'u on iphone?' and suggested force-quitting Messages)."""
    if not enabled() or not getattr(user, "card_setup_at", None):
        return None
    if getattr(user, "card_opened_at", None):
        return ("## WORKOUT CARD\nthey've opened a card before — the iMessage extension is installed. "
                "'won't open' now is a real glitch: say to try the tap again, and offer to take their sets by text.")
    return ("## WORKOUT CARD\nsent, NEVER opened on their phone — the iMessage extension isn't added yet. "
            "'it won't open' / 'nothing happens' / 'what is this' = that, not a bug: tapping the card offers "
            "the one-tap add (like GamePigeon), nothing on their home screen, they stay in Messages. Say that "
            "once, offer to take their sets by text, and never troubleshoot (no 'are u on iphone', no "
            "'restart messages' — the card only reaches iPhones).")


def ask_text(user) -> str:
    lvl = (getattr(user, "experience", None) or "").strip().lower()
    return ASK_TRAINED if lvl in ("intermediate", "advanced") else ASK_NEW


def setup_card_refused(user_id: int, phone: str, session_id: int) -> None:
    """Photon refused the card during setup: one honest line, the session retired.
    Never the per-exercise texts — those are for workout time."""
    from sms import send_sms
    from models import get_session, WorkoutSession
    s = get_session()
    try:
        ws = s.get(WorkoutSession, session_id)
        if ws is not None and ws.status in ("planned", "active"):
            ws.status = "abandoned"
            s.commit()
    finally:
        s.close()
    send_sms(phone, REFUSED_LINE, user_id=user_id, message_type=MESSAGE_TYPE)
    logger.warning("CARD_SETUP_REFUSED user=%s session=%s", user_id, session_id)


def run_onboarding_setup(user_id: int) -> str:
    """The setup step at onboarding completion. Returns 'sent' (framing + card + tour
    went out), 'asked' (the first-card ask went out; the answer sends the rest through
    workouts.calibrate.handle_pending_card_reply), or 'skipped:<why>'."""
    if not enabled():
        return "skipped:flag"
    if not config.START_WORKOUT_TOOL_ENABLED:
        return "skipped:start_tool_off"
    from sms import _resolve_channel, send_sms
    if _resolve_channel(user_id) != "imessage":
        return "skipped:sms"
    from workouts.session_ops import active_session_id
    if active_session_id(user_id):
        return "skipped:open_session"
    from workouts.start import start_workout_session, NeedsAnchors
    u = _user(user_id)
    if u is None:
        return "skipped:no_user"
    try:
        r = start_workout_session(user_id, setup=True)
    except NeedsAnchors:
        send_sms(u.phone, ask_text(u), user_id=user_id, message_type=MESSAGE_TYPE)
        logger.info("CARD_SETUP_ASKED user=%s", user_id)
        return "asked"
    except ValueError as e:      # split not mapped to days, etc. — workout time handles it
        logger.info("CARD_SETUP_SKIPPED user=%s why=%s", user_id, e)
        return f"skipped:{e}"
    return "sent" if r.get("surface") == "card" else f"skipped:{r.get('surface')}"
