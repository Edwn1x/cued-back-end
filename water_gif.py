"""Water-break GIF — the standing water ping as a picture, not a line.

Founder 2026-10-26: "replace the drink water reminder with a cute little gif/avatar
instead of a boring old drink water text." The character is the cued ball in the site's
blue-ink sketch style (static/water/*.gif, one loop per pose); the pings rotate through
the files so two in a row never look the same. GIF alone — no caption (founder).

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


def pick_gif(row, user_id: int | None = None) -> str | None:
    """Next GIF for this row: shuffled per user and per ping (founder: 'a few distinct
    ones so it never gets repetitive'), never the same file twice in a row."""
    files = gif_files()
    if not files:
        return None
    count = int(getattr(row, "sent_count", 0) or 0)
    uid = user_id if user_id is not None else getattr(row, "user_id", 0)
    return files[_slot(uid, len(files), count)]


def gif_url(name: str) -> str:
    base = (config.WATER_GIF_BASE_URL or config.INTEGRATIONS_BASE_URL).rstrip("/")
    return f"{base}/static/water/{name}"


def send_water_gif(user, row) -> bool:
    """Send the next GIF in the rotation to `user` for interval reminder `row`.
    Returns True only when it landed and the Message row is written. Any failure is
    logged and returns False — the caller falls back to the plain line, whose own
    send path owns failover / hold semantics."""
    if not config.WATER_GIF_ENABLED:
        return False
    name = pick_gif(row, user.id)
    if not name:
        logger.warning("WATER_GIF_NO_FILES dir=%s", GIF_DIR)
        return False
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
            sid = sms._send_imessage_attachment(user.phone, data, name, MIME)
        else:
            sid = sms._send_single_mms(user.phone, gif_url(name))
        sms._log_message(user.id, MARKER, WATER_REMINDER_TYPE, channel=channel,
                         provider_sid=sid, delivery_status="sent")
        logger.info("WATER_GIF_SENT user=%s channel=%s gif=%s", user.id, channel, name)
        return True
    except Exception as e:  # noqa: BLE001 — never lose the ping to a media failure
        logger.warning("WATER_GIF_FAILED user=%s channel=%s gif=%s err=%s — falling back to text",
                       user.id, channel, name, e)
        return False
