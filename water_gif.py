"""Water-break GIF — the standing water ping as a picture, not a line.

Founder 2026-10-26: "replace the drink water reminder with a cute little gif/avatar
instead of a boring old drink water text." The character is the cued ball in the site's
blue-ink sketch style (static/water/*.gif, one loop per pose); the pings rotate through
the files so two in a row never look the same. GIF alone — no caption (founder).

The polished set (founder 2026-11-04/05, after the sketch loops: "more professional and
polished ones"; then live on their phone: "smaller", "the blue is way too strong", and of
the fully transparent look, "which i do prefer"). static/water/polished/*.png — a progress
ring, a porthole of water, a drop and its ripple, a glass that pours, "Drink water." in
type, the logo's dot dripping, and the ball as a soft 3D figure. They are ANIMATED PNG, not
GIF: real alpha, so brand-blue art sits straight on a light or a dark thread with no card
and no jagged 1-bit edge. Each file is a 640x384 canvas with the art in the left 384 (60%):
iMessage scales an image to the bubble width by its proportions, so that padding is what
makes it land smaller on screen. iMessage only — an MMS flattens the animation and the
alpha, so an SMS user keeps the GIF loops. Which pool a ping draws from is
config.WATER_STICKER_SET: "sketch" (default — nothing changes on deploy), "polished", "both".

Where it sits: reminders._fire_one calls send_water_gif() for an interval (every_hours)
row that is about water, BEFORE composing the line. True → the GIF landed and the row
is logged as a water_reminder (same message_type as the line, so the 👍/"drank" ack
window, the heartbeat's anti-stack gate and the engagement tracker see exactly what they
saw before). False → the caller sends the plain line as it always has; the promise is
kept either way.

Transport: iMessage users get a real attachment bubble via the sidecar's
/send-attachment; SMS users get an MMS whose media URL points at this app's static
folder. Both go through sms.py helpers so channel routing, the opt-out backstop (the
caller's) and the Message row stay in one place.
"""
from __future__ import annotations

import logging
import os

import config
from engagement_tracker import WATER_REMINDER_TYPE

logger = logging.getLogger(__name__)

GIF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "water")
# Stored body for the GIF row. The conversation window shows the model its own recent
# turns; this is how it knows a water ping went out without a line to quote.
MARKER = "[water-break gif]"
MIME = "image/gif"
POLISHED_DIR = "polished"      # static/water/polished/*.png — the polished set (animated PNG)
_MIME_BY_EXT = {".gif": "image/gif", ".png": "image/png"}


def _mime(name: str) -> str:
    return _MIME_BY_EXT.get(os.path.splitext(name)[1].lower(), MIME)


def is_water_row(row) -> bool:
    """An interval row created by the offer, or one whose text is about water."""
    if not getattr(row, "every_hours", None):
        return False
    if getattr(row, "source", None) == "offer":
        return True
    return "water" in (getattr(row, "text", "") or "").lower()


def gif_files() -> list[str]:
    try:
        return sorted(f for f in os.listdir(GIF_DIR) if f.lower().endswith(".gif"))
    except FileNotFoundError:
        return []


def polished_files() -> list[str]:
    """The polished set, as names relative to GIF_DIR ('polished/water-ring.png') so the
    same name opens the file, builds the static URL and can be asked for at the admin
    preview. Empty when the folder isn't there."""
    try:
        d = os.path.join(GIF_DIR, POLISHED_DIR)
        return sorted(f"{POLISHED_DIR}/{f}" for f in os.listdir(d) if f.lower().endswith(".png"))
    except FileNotFoundError:
        return []


def _pool(channel: str | None) -> list[str]:
    """The files this ping may draw from. The polished set needs iMessage (see the module
    docstring) and config.WATER_STICKER_SET to ask for it; anything else is the GIF loops."""
    files = gif_files()
    mode = (getattr(config, "WATER_STICKER_SET", "sketch") or "sketch").strip().lower()
    polished = polished_files() if (channel == "imessage" and mode in ("polished", "both")) else []
    if polished and mode == "polished":
        return polished
    return files + polished


def _slot(user_id: int, n: int, count: int) -> int:
    """Deterministic pseudo-random index for ping number `count`: varies per user and
    per ping (so the order isn't the same fixed cycle for everyone), and never lands
    on the slot the previous ping used. Recomputable, so no state to store."""
    if n <= 1:
        return 0
    def h(c):
        x = (int(user_id or 0) * 1_000_003 + c * 7_919) & 0xFFFFFFFF
        x ^= x >> 13; x = (x * 0x5BD1E995) & 0xFFFFFFFF; x ^= x >> 15
        return x
    idx = h(count) % n
    if count > 0:
        prev = _slot(user_id, n, count - 1)
        if idx == prev:
            idx = (idx + 1 + h(count) % (n - 1)) % n
    return idx


BED_GIF_HINT = "sleepy"      # the "sip before bed" loop (water-04-sleepy-stretch.gif, #270)


def _bed_window_open(user, now=None) -> bool:
    """True in the last WATER_GIF_BED_WINDOW_H hours before the user's sleep_time (an
    after-midnight bedtime counts as the same evening); after 21:00 local when the profile
    has no clock. `now` naive UTC. Fail-open → True (the old time-blind shuffle)."""
    try:
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from reminders import parse_local_time
        tz = ZoneInfo(getattr(user, "user_timezone", None) or "America/Los_Angeles")
        n = now or datetime.now(timezone.utc).replace(tzinfo=None)
        local = n.replace(tzinfo=timezone.utc).astimezone(tz)
        now_m = local.hour * 60 + local.minute
        bed = parse_local_time(getattr(user, "sleep_time", None))
        if not bed:
            return local.hour >= 21
        bed_m = bed[0] * 60 + bed[1]
        return (bed_m - now_m) % 1440 <= int(getattr(config, "WATER_GIF_BED_WINDOW_H", 3)) * 60
    except Exception:  # noqa: BLE001
        return True


def pick_gif(row, user_id: int | None = None, *, user=None, now=None, channel: str | None = None) -> str | None:
    """Next GIF for this row: shuffled per user and per ping (founder: 'a few distinct
    ones so it never gets repetitive'), never the same file twice in a row. With `user`,
    the bed loop (BED_GIF_HINT) is only in the draw during the evening window — live
    2026-10-28 "sip before bed" went out at 17:05. `channel` decides whether the polished set
    is in the pool (_pool); without it the pool is the GIF loops, as before."""
    files = _pool(channel)
    if not files:
        return None
    if user is not None and getattr(config, "WATER_GIF_BED_WINDOW_ENABLED", True) and not _bed_window_open(user, now):
        day_files = [f for f in files if BED_GIF_HINT not in f.lower()]
        if day_files:
            files = day_files
    count = int(getattr(row, "sent_count", 0) or 0)
    uid = user_id if user_id is not None else getattr(row, "user_id", 0)
    return files[_slot(uid, len(files), count)]


def gif_url(name: str) -> str:
    base = (config.WATER_GIF_BASE_URL or config.INTEGRATIONS_BASE_URL).rstrip("/")
    return f"{base}/static/water/{name}"


def send_gif(user, name: str, *, message_type: str = WATER_REMINDER_TYPE) -> bool:
    """Send static/water/<name> to `user` over their channel and write the Message row.
    True only when it landed. Any failure is logged and returns False — callers decide
    the fallback (the reminder sends its plain line; the admin preview reports it)."""
    import sms
    channel = "sms"
    try:
        channel = sms._resolve_channel(user.id)
        if channel == "imessage":
            if sms.outage_active():
                # Photon is known-down: don't burn an upload on it; the text path
                # knows how to hold on their line.
                logger.info("WATER_GIF_SKIPPED user=%s reason=outage_active", user.id)
                return False
            with open(os.path.join(GIF_DIR, name), "rb") as f:
                data = f.read()
            sid = sms._send_imessage_attachment(user.phone, data, os.path.basename(name), _mime(name))
        else:
            sid = sms._send_single_mms(user.phone, gif_url(name))
        sms._log_message(user.id, MARKER, message_type, channel=channel,
                         provider_sid=sid, delivery_status="sent")
        logger.info("WATER_GIF_SENT user=%s channel=%s gif=%s type=%s", user.id, channel, name, message_type)
        return True
    except Exception as e:  # noqa: BLE001 — never lose the ping to a media failure
        if _ambiguous_dispatch(e) and getattr(config, "WATER_GIF_AMBIGUOUS_AS_SENT_ENABLED", True):
            # Photon's "dispatch may have completed": the GIF usually landed; a text now doubles
            # it (live 2026-10-28 08:01, user 53). Count it as sent, write the row, no fallback.
            try:
                sms._log_message(user.id, MARKER, message_type, channel=channel, provider_sid=None,
                                 delivery_status="sent")
            except Exception as le:  # noqa: BLE001
                logger.warning("WATER_GIF_AMBIGUOUS_LOG_FAILED user=%s err=%s", user.id, le)
            logger.warning("WATER_GIF_AMBIGUOUS user=%s channel=%s gif=%s err=%s — counted as sent, no text",
                           user.id, channel, name, e)
            return True
        logger.warning("WATER_GIF_FAILED user=%s channel=%s gif=%s err=%s — falling back to text",
                       user.id, channel, name, e)
        return False


def _ambiguous_dispatch(err) -> bool:
    return "dispatch may have completed" in str(err).lower()


def send_water_gif(user, row) -> bool:
    """The reminder path: next GIF in this user's shuffle for interval reminder `row`.
    False → the caller sends the plain line, whose own send path owns failover / hold."""
    if not config.WATER_GIF_ENABLED:
        return False
    try:
        import sms
        channel = sms._resolve_channel(user.id)
    except Exception:  # noqa: BLE001 — unknown channel → the GIF loops, which every channel can show
        channel = None
    name = pick_gif(row, user.id, user=user, channel=channel)
    if not name:
        logger.warning("WATER_GIF_NO_FILES dir=%s", GIF_DIR)
        return False
    ok = send_gif(user, name)
    if ok and name.startswith(POLISHED_DIR + "/"):
        announce_new_stickers_once(user)
    return ok


def announce_new_stickers_once(user) -> bool:
    """The first polished sticker a user gets comes with one line under it — "new sticker
    update!" (founder 2026-11-05: "next time the water stickers go out send out an
    accompanying message") — and never again: a later ping finds the line already in their
    thread and stays a sticker alone.

    It is typed as the water ping it belongs to, not as a line of its own. reminders.
    is_water_ack reads the LAST outbound, so with any other type their 👍 right after would
    stop being a water ack; and no gate should read it as the coach speaking. Only the
    reminder path calls this (never the admin preview), only after the sticker landed, and a
    failure here never touches the ping. A failed send leaves no 'seen' row, so the next
    polished ping tries again."""
    text = (getattr(config, "WATER_STICKER_NOTE_TEXT", "") or "").strip()
    if not getattr(config, "WATER_STICKER_NOTE_ENABLED", True) or not text:
        return False
    try:
        import sms
        from models import Message, get_session
        from sms_encoding import normalize_for_sms
        session = get_session()
        try:
            seen = (session.query(Message.id)
                    .filter(Message.user_id == user.id, Message.direction == "out",
                            Message.body.in_({text, normalize_for_sms(text)}),
                            Message.delivery_status != "failed").first())
        finally:
            session.close()
        if seen:
            return False
        sms.send_sms(user.phone, text, user_id=user.id, message_type=WATER_REMINDER_TYPE)
        logger.info("WATER_STICKER_NOTE_SENT user=%s text=%r", user.id, text)
        return True
    except Exception as e:  # noqa: BLE001 — the line is extra; the sticker already went
        logger.warning("WATER_STICKER_NOTE_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return False
