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


def _send_imessage(phone: str, body: str) -> str:
    """POST {phone, text} to the sidecar's /send. Returns the Photon message id.
    Raises on non-2xx, on ok=false, or on any transport error — the caller
    writes the `failed` row and fails over."""
    resp = requests.post(
        config.SIDECAR_URL.rstrip("/") + "/send",
        json={"phone": phone, "text": body},
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


def send_sms(phone: str, body: str, user_id: int = None, message_type: str = "freeform"):
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
            sid = _send_imessage(phone, im_body)
            _log_message(user_id, im_body, message_type,
                         channel="imessage", provider_sid=sid, delivery_status="sent")
            return sid
        except Exception as e:  # noqa: BLE001 — every failure class fails over
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
