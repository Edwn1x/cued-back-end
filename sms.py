import logging
import time

from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

import config
from models import get_session, Message
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


def _log_message(user_id: int, body: str, message_type: str):
    """Log an outbound message to the database."""
    session = get_session()
    try:
        session.add(Message(user_id=user_id, direction="out", body=body, message_type=message_type))
        session.commit()
    finally:
        session.close()


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
        last_sid = _send_single(phone, part)
        if user_id:
            _log_message(user_id, part, message_type)

    return last_sid


def log_incoming(user_id: int, body: str, message_type: str = "freeform",
                 has_image: bool = False):
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
