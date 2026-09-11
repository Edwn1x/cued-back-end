"""
iMessage typing indicator ("Cued is typing…").

The Spectrum cloud provider exposes startTyping/stopTyping on the DM; the sidecar
wraps it as POST /typing {phone, state}. Flask fires "start" the moment it begins
GENERATING a reply for an iMessage user — after the read-buffer, never during it
(a friend reads for a bit, then the dots appear, then the text) — and "stop" on
any path where no iMessage reply will follow (exception, SMS failover). The
bubble also clears itself when the reply lands (/send stops it too).

Everything here is fire-and-forget and best-effort: a typing signal must never
delay, block, or fail a reply. Twilio has no equivalent, so SMS users get nothing.
"""

from __future__ import annotations

import logging
import threading

import requests

import config

logger = logging.getLogger("cued.typing")

TIMEOUT_S = 2.0  # a typing signal that takes longer than this isn't worth having


def _phone_and_channel(user_id: int):
    from models import get_session, User
    from sms import _resolve_channel
    session = get_session()
    try:
        row = session.query(User.phone).filter(User.id == user_id).first()
    finally:
        session.close()
    if not row:
        return None, "sms"
    return row[0], _resolve_channel(user_id)


def _post(phone: str, state: str, user_id: int) -> bool:
    try:
        resp = requests.post(
            config.SIDECAR_URL.rstrip("/") + "/typing",
            json={"phone": phone, "state": state},
            headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
            timeout=TIMEOUT_S,
        )
        ok = resp.status_code < 300
        logger.info("TYPING_SIGNAL user=%s state=%s ok=%s", user_id, state, ok)
        return ok
    except Exception as e:  # noqa: BLE001 — never let a typing signal surface
        logger.info("TYPING_SIGNAL user=%s state=%s ok=False err=%s", user_id, state, e)
        return False


def signal_typing(user_id: int, state: str = "start", *, wait: bool = False) -> bool:
    """Show ("start") or clear ("stop") the typing bubble for `user_id` if — and only
    if — the flag is on, a sidecar is configured, and the user's resolved channel is
    iMessage right now (same router the send uses, so a tripped breaker = no bubble).
    Returns True if a signal was dispatched. Runs in a daemon thread unless `wait`."""
    if state not in ("start", "stop"):
        return False
    if not config.TYPING_INDICATOR_ENABLED or not config.SIDECAR_URL or not user_id:
        return False
    try:
        phone, channel = _phone_and_channel(user_id)
    except Exception as e:  # noqa: BLE001
        logger.info("TYPING_SIGNAL user=%s state=%s skipped err=%s", user_id, state, e)
        return False
    if not phone or channel != "imessage":
        return False
    if wait:
        return _post(phone, state, user_id)
    threading.Thread(target=_post, args=(phone, state, user_id), daemon=True).start()
    return True


def typing_start(user_id: int) -> bool:
    return signal_typing(user_id, "start")


def typing_stop(user_id: int) -> bool:
    return signal_typing(user_id, "stop")
