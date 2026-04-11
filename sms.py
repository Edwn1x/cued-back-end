from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import config
from models import get_session, Message
import time

client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

SMS_SPLIT_DELAY = 2.5  # seconds between split messages


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

    # Cap at 3
    if len(parts) > 3:
        parts = parts[:2] + [" --- ".join(parts[2:])]

    return parts if parts else [body]


def send_sms(phone: str, body: str, user_id: int = None, message_type: str = "freeform"):
    """Send an SMS, splitting longer messages into sequential texts with a delay."""
    parts = split_message(body)

    last_sid = None
    for i, part in enumerate(parts):
        if i > 0:
            time.sleep(SMS_SPLIT_DELAY)
        last_sid = _send_single(phone, part)
        if user_id:
            _log_message(user_id, part, message_type)

    return last_sid


def log_incoming(user_id: int, body: str, message_type: str = "freeform"):
    """Log an incoming SMS to the database."""
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
