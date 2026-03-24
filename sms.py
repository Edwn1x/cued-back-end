from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import config
from models import get_session, Message

client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def send_sms(phone: str, body: str, user_id: int = None, message_type: str = "freeform"):
    """Send an SMS and log it to the database."""
    # Twilio send
    message = client.messages.create(
        body=body,
        from_=config.TWILIO_PHONE_NUMBER,
        to=phone,
    )

    # Log to DB
    if user_id:
        session = get_session()
        try:
            msg = Message(
                user_id=user_id,
                direction="out",
                body=body,
                message_type=message_type,
            )
            session.add(msg)
            session.commit()
        finally:
            session.close()

    return message.sid


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
