import logging
import re
import time

import requests
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

import config
from models import get_session, Message, User
from sms_encoding import normalize_for_sms, residual_non_gsm, estimate_segments

logger = logging.getLogger("cued.sms")

client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

SMS_SPLIT_DELAY = 2.5  # seconds between split messages
SMS_SEGMENT_WARN_THRESHOLD = 6  # ~900+ GSM-7 chars; log when bodies get this large

# Appended to the stored inbound body when the MMS carried media. The image bytes
# only ever exist inside the live turn's API call, so this marker is the ONE durable
# trace that an image arrived — it's what lets a later turn honestly say "you sent a
# pic earlier but i didn't save the detail" instead of "nothing came through".
# voice.md's retrieval-gap honesty rule references this literal string.
IMAGE_MARKER = "[image attached]"


def _send_single(phone: str, body: str) -> str:
    """Send one SMS segment via Twilio and return the SID."""
    message = client.messages.create(
        body=body,
        from_=config.TWILIO_PHONE_NUMBER,
        to=phone,
    )
    return message.sid


def _log_message(user_id: int, body: str, message_type: str,
                 channel: str = "sms", provider_sid: str | None = None,
                 delivery_status: str = "sent"):
    """Log an outbound message to the database, stamped with which pipe carried it
    and whether it landed. `delivery_status='failed'` rows are what the keystone
    (engagement_tracker.increment_unanswered) reads — write them, never skip them."""
    session = get_session()
    try:
        session.add(Message(
            user_id=user_id, direction="out", body=body, message_type=message_type,
            channel=channel, provider_sid=provider_sid, delivery_status=delivery_status,
        ))
        session.commit()
    finally:
        session.close()


# ─── Photon migration Phase 4A: channel router (above _send_single) ──────────
# send_sms() stays the single chokepoint; ~20 call sites are untouched. The
# router decides per-send from code-owned state (flag, sidecar configured,
# user.preferred_channel, user.channel_failed_over) — never from the model.

def _resolve_channel(user_id) -> str:
    """'imessage' only when the flag is on, a sidecar is configured, and the user
    asked for it AND their breaker is closed. Everything else is 'sms'."""
    if not user_id or not config.IMESSAGE_CHANNEL_ENABLED or not config.SIDECAR_URL:
        return "sms"
    session = get_session()
    try:
        row = (session.query(User.preferred_channel, User.channel_failed_over)
               .filter(User.id == user_id).first())
    finally:
        session.close()
    if row and row[0] == "imessage" and not row[1]:
        return "imessage"
    return "sms"


def _send_imessage(phone: str, body: str, reply_to: str = None) -> str:
    """POST {phone, text[, reply_to]} to the sidecar's /send. `reply_to` is the
    Photon id of one of THEIR messages → a threaded iMessage reply quoting it.
    Returns the Photon message id. Raises on non-2xx, on ok=false, or on any
    transport error — the caller writes the `failed` row and fails over."""
    payload = {"phone": phone, "text": body}
    if reply_to:
        payload["reply_to"] = reply_to
    resp = requests.post(
        config.SIDECAR_URL.rstrip("/") + "/send",
        json=payload,
        headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
        timeout=config.SIDECAR_TIMEOUT_S,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"sidecar /send {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(f"sidecar /send not ok: {resp.text[:200]}")
    return data.get("provider_message_id")


def _mark_failed_over(user_id: int):
    """Trip the user's circuit breaker: subsequent sends go straight to SMS until
    an operator clears channel_failed_over (admin). Code-owned, deliberate."""
    from datetime import datetime, timezone
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user and not user.channel_failed_over:
            user.channel_failed_over = True
            user.channel_failover_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()


def _imessage_body(body: str) -> str:
    """iMessage gets the whole message as ONE bubble: the coach's `---` part
    boundaries become blank lines (never a literal `---`), and there is no GSM-7
    normalization — iMessage is unicode."""
    parts = [p.strip() for p in re.split(r"\s*---\s*", body) if p.strip()]
    return "\n\n".join(parts) if parts else body


def split_message(body: str) -> list[str]:
    """Split a coach message into SMS parts using --- as the delimiter.

    The AI is instructed to separate messages with ---. Each part maps to
    one text: msg 1 = main content, msg 2 = context, msg 3 = CTA/question.
    Falls back to the full body as a single message if no delimiter found.
    Caps at 3 parts.
    """
    import re
    parts = [p.strip() for p in re.split(r"\s*---\s*", body) if p.strip()]

    # Cap at 2
    if len(parts) > 2:
        parts = parts[:1] + [" --- ".join(parts[1:])]

    return parts if parts else [body]


TAPBACKS = {"love": "❤️", "like": "👍", "dislike": "👎", "laugh": "😂", "emphasize": "‼️", "question": "❓"}


def react_to_message(user_id: int, provider_sid: str, emoji: str) -> bool:
    """Tapback on one of the user's iMessages (by the Photon id stored on its row).
    Logs its own outbound row with message_type="reaction" so the history window
    sees it and the coach doesn't re-ack — and so every silence gate can EXCLUDE
    it (engagement_tracker._not_reaction). A failed reaction is logged and is
    neither a strike nor a channel failure (a stale message id ≠ a dead pipe):
    the breaker is NOT tripped. Returns True on success."""
    if not user_id or not provider_sid or not emoji:
        return False
    if _resolve_channel(user_id) != "imessage":
        return False
    session = get_session()
    try:
        row = session.query(User.phone).filter(User.id == user_id).first()
    finally:
        session.close()
    if not row:
        return False
    shown = TAPBACKS.get(emoji.strip().lower(), emoji.strip())
    body = f"[reacted {shown} to their message]"
    try:
        resp = requests.post(
            config.SIDECAR_URL.rstrip("/") + "/react",
            json={"phone": row[0], "message_id": provider_sid, "emoji": emoji},
            headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
            timeout=config.SIDECAR_TIMEOUT_S,
        )
        data = resp.json() if resp.status_code < 300 else {}
        if resp.status_code >= 300 or not data.get("ok"):
            raise RuntimeError(f"sidecar /react {resp.status_code}: {resp.text[:200]}")
        _log_message(user_id, body, "reaction",
                     channel="imessage", provider_sid=data.get("provider_message_id"), delivery_status="sent")
        logger.info("REACTION_SENT user_id=%s emoji=%s on=%s", user_id, shown, provider_sid)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("REACTION_FAILED user_id=%s emoji=%s on=%s err=%s — not a strike, breaker untouched",
                       user_id, shown, provider_sid, e)
        _log_message(user_id, body, "reaction",
                     channel="imessage", provider_sid=None, delivery_status="failed")
        return False


CONSENT_GATE_MARKER = "Target not allowed"


def _is_consent_gate(err) -> bool:
    """Photon's shared-pool refusal for a user who hasn't texted their line yet."""
    return CONSENT_GATE_MARKER.lower() in str(err).lower()


IMESSAGE_INVITE = "ps — i can text you on iMessage instead. tap this once and say hey: {link}"


def _with_imessage_invite(user_id: int, body: str) -> str:
    """Append the one-tap opt-in link to an SMS body (onboarding hook only)."""
    try:
        from photon import imessage_link_for_user
        link = imessage_link_for_user(user_id)
    except Exception:  # noqa: BLE001 — the hook must go out regardless
        link = None
    if not link:
        return body
    return f"{body}\n\n{IMESSAGE_INVITE.format(link=link)}"


def send_sms(phone: str, body: str, user_id: int = None, message_type: str = "freeform",
             reply_to_sid: str = None):
    """Send an SMS, splitting longer messages into sequential texts with a delay.

    Body is normalized to GSM-7 here (before split + dispatch) so the carrier
    encodes our outbound as 1-segment GSM-7 (160 chars/seg) instead of the
    UCS-2 fallback (67 chars/seg) that gets triggered by a single em-dash or
    smart quote. The transform is the LAST thing we do before split so any
    upstream finalization (orchestrator → personality layer → send_sms) is
    captured. Logging-mode acks and templated stats lines benefit too — any
    `✓` glyph would force UCS-2 if it slipped through.

    See sms_encoding.py for the character map and why we don't rely solely
    on Twilio's server-side Smart Encoding toggle.
    """
    # Photon migration 4A: route first. iMessage → sidecar, one call, full body.
    # On ANY failure: write the `failed` row FIRST (the keystone reads it), trip
    # the breaker, then fall through to Twilio so the same message still lands.
    if _resolve_channel(user_id) == "imessage":
        im_body = _imessage_body(body)
        try:
            # Threaded only when the coach asked; the 2-arg form is preserved so
            # every existing caller (and test double) of _send_imessage still works.
            sid = (_send_imessage(phone, im_body, reply_to_sid) if reply_to_sid
                   else _send_imessage(phone, im_body))
            _log_message(user_id, im_body, message_type,
                         channel="imessage", provider_sid=sid, delivery_status="sent")
            return sid
        except Exception as e:  # noqa: BLE001 — every failure class fails over
            # The bubble was up for an iMessage reply that isn't coming — clear it
            # before the green one goes out (best-effort; the DM may be unreachable).
            try:
                from typing_indicator import typing_stop
                typing_stop(user_id)
            except Exception:  # noqa: BLE001
                pass
            if _is_consent_gate(e):
                # Shared-pool consent gate: THEY haven't texted their line yet. Not a
                # dead pipe — a distinct, non-alarming line; the breaker still trips
                # (every send would fail the same way) and their first inbound
                # iMessage resets it. The onboarding hook carries the one-tap link
                # so the invitation reaches them on the very first text.
                logger.info("IMESSAGE_NOT_OPTED_IN user_id=%s message_type=%s — they haven't texted "
                            "their line yet; falling over to SMS", user_id, message_type)
                if message_type == "onboarding":
                    body = _with_imessage_invite(user_id, body)
            else:
                logger.error("IMESSAGE_SEND_FAILED user_id=%s message_type=%s err=%s — failing over to SMS",
                             user_id, message_type, e)
            _log_message(user_id, im_body, message_type,
                         channel="imessage", provider_sid=None, delivery_status="failed")
            _mark_failed_over(user_id)

    # Last transform before dispatch — normalize once on the full body so the
    # warning log (next 6 lines) reports per-logical-message, not per-segment.
    body = normalize_for_sms(body)

    # Telemetry: residual non-GSM (e.g. an emoji slipped through) forces UCS-2
    # and roughly halves capacity. Oversized GSM-7 bodies risk delivery limits.
    # Logged, never blocked — coaching content keeps flowing.
    residual = residual_non_gsm(body)
    enc, segs = estimate_segments(body)
    if residual:
        logger.warning(
            "SMS_UCS2 user_id=%s message_type=%s segments=%d chars=%d residual=%s",
            user_id, message_type, segs, len(body), residual,
        )
    elif segs > SMS_SEGMENT_WARN_THRESHOLD:
        logger.warning(
            "SMS_LARGE_GSM user_id=%s message_type=%s segments=%d chars=%d",
            user_id, message_type, segs, len(body),
        )

    parts = split_message(body)

    last_sid = None
    for i, part in enumerate(parts):
        if i > 0:
            time.sleep(SMS_SPLIT_DELAY)
        try:
            last_sid = _send_single(phone, part)
        except Exception:
            # The row is the keystone's evidence that this didn't land. Write it,
            # then re-raise — callers' existing error handling is unchanged.
            if user_id:
                _log_message(user_id, part, message_type,
                             channel="sms", provider_sid=None, delivery_status="failed")
            raise
        if user_id:
            _log_message(user_id, part, message_type,
                         channel="sms", provider_sid=last_sid, delivery_status="sent")

    return last_sid


def log_incoming(user_id: int, body: str, message_type: str = "freeform",
                 has_image: bool = False, channel: str = "sms", provider_sid: str | None = None):
    """Log an incoming SMS to the database. `has_image` appends IMAGE_MARKER so the
    stored row (the only thing the conversation window ever sees) records that media
    was attached — a captionless MMS logs the marker alone, never an empty body."""
    if has_image:
        body = f"{body} {IMAGE_MARKER}" if body else IMAGE_MARKER
    session = get_session()
    try:
        msg = Message(
            user_id=user_id,
            direction="in",
            body=body,
            message_type=message_type,
            channel=channel,
            provider_sid=provider_sid,
            delivery_status="delivered",  # an inbound we hold is, by definition, delivered to us
        )
        session.add(msg)
        session.commit()
    finally:
        session.close()


def get_twiml_response(body: str = None):
    """Build a TwiML response. If body is None, return empty (we'll respond async)."""
    resp = MessagingResponse()
    if body:
        resp.message(body)
    return str(resp)
