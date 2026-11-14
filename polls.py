"""
Polls — inbound kinds initiative, steps 7 + 8 (2026-11-14). Plumbing only; the first
use-case (gym time today? / session feel / meal off the menu…) is the founder's pick.

Outbound: `send_poll_to_user` puts an iMessage poll in the thread through the sidecar's
/poll (iOS 26 polls) and logs ONE outbound row, message_type "poll", whose provider_sid
is the poll's Photon id — the prefix of every vote's id. An SMS user gets the same ask as
a numbered list ("1. … 2. …") on the same row type, so the open-poll gate and the history
window read both the same way. Rules the tool enforces in code: 2–POLL_MAX_OPTIONS short,
distinct options; a short title; setup finished (onboarding_step >= 3); one open poll per
user until they pick or POLL_OPEN_HOURS pass. The judgement rule — logistics and
self-report only, never a programming decision for a beginner — lives in voice.md
("you prescribe; you don't poll") and the tool description.

Inbound: a vote lands on /internal/inbound as text naming the pick ("[picked "5:30" on
the poll "gym time today?"]") plus a structured `poll` field; it is stored and answered
like any reply. `open_poll` treats any inbound row after the poll that names a pick as the
answer. A deselect is dropped before any model turn (app.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.polls")

POLL_MESSAGE_TYPE = "poll"
PICK_MARKER = " on the poll "          # what a vote's inbound text always carries
MAX_TITLE_CHARS = 120
MAX_OPTION_CHARS = 40
MIN_OPTIONS = 2


def poll_row_body(title: str, options: list[str]) -> str:
    return f'[poll "{title}": ' + " / ".join(options) + "]"


def numbered_list(title: str, options: list[str]) -> str:
    """The SMS / no-poll fallback: the same ask as a list they answer by number or words."""
    return title + "\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))


def validate(title, options) -> tuple[str, list[str]] | str:
    """Clean (title, options) or an 'error: …' line the model can act on."""
    title = " ".join(str(title or "").split())
    if not title:
        return "error: title is required — the one-line question the poll asks"
    if len(title) > MAX_TITLE_CHARS:
        return f"error: title over {MAX_TITLE_CHARS} chars — a poll question is one short line"
    if not isinstance(options, (list, tuple)):
        return f"error: options must be a list of {MIN_OPTIONS} to {config.POLL_MAX_OPTIONS} options"
    opts = [" ".join(str(o or "").split()) for o in options]
    if not (MIN_OPTIONS <= len(opts) <= config.POLL_MAX_OPTIONS):
        return f"error: a poll takes {MIN_OPTIONS} to {config.POLL_MAX_OPTIONS} options (got {len(opts)})"
    if any(not o for o in opts):
        return "error: every option must be a few words, none blank"
    if any(len(o) > MAX_OPTION_CHARS for o in opts):
        return f"error: keep each option under {MAX_OPTION_CHARS} chars — a tap target, not a sentence"
    if len({o.lower() for o in opts}) != len(opts):
        return "error: options must be distinct"
    return title, opts


def open_poll(session, user_id: int):
    """The latest outbound poll row within POLL_OPEN_HOURS that no later inbound answered,
    or None. 'Answered' = any inbound row after it whose body names a pick."""
    from models import Message
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=config.POLL_OPEN_HOURS)
    row = (session.query(Message)
           .filter(Message.user_id == user_id, Message.direction == "out",
                   Message.message_type == POLL_MESSAGE_TYPE, Message.created_at >= since)
           .order_by(Message.id.desc()).first())
    if not row:
        return None
    answered = (session.query(Message.id)
                .filter(Message.user_id == user_id, Message.direction == "in", Message.id > row.id,
                        Message.body.contains(PICK_MARKER)).first())
    return None if answered else row


def send_poll_to_user(user_id: int, title, options, *, source: str = "coach") -> str:
    """Validate, gate, send over the user's channel, log the row. Returns the status line
    for the coach loop: 'ok: …' or 'error: …'. Never raises."""
    import sms
    from models import get_session, User

    cleaned = validate(title, options)
    if isinstance(cleaned, str):
        return cleaned
    title, opts = cleaned

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.phone:
            return "error: no phone on file"
        if getattr(user, "archived_at", None):
            return "error: this account is archived"
        if source == "coach" and (user.onboarding_step or 0) < 3:
            return "error: no polls during setup — ask in words until their setup is done"
        open_ = open_poll(session, user_id)
        if open_ is not None:
            return (f"error: a poll is already open ({open_.body}) — wait for their pick or ask in words; "
                    "one poll at a time")
        phone = user.phone
    finally:
        session.close()

    body = poll_row_body(title, opts)
    channel = sms._resolve_channel(user_id)
    if channel != "imessage":
        # No polls over SMS: the same ask as a numbered list, same row type so the gate
        # and the window read it the same way.
        sms.send_sms(phone, numbered_list(title, opts), user_id=user_id, message_type=POLL_MESSAGE_TYPE)
        logger.info("POLL_SENT_AS_LIST user=%s source=%s title=%r n=%d", user_id, source, title, len(opts))
        return ("ok: they're on SMS so the ask went out as a numbered list (SMS can't do polls) — "
                "they'll answer with a number or words; your reply is the one line around it")
    try:
        if sms.outage_active():
            raise RuntimeError("imessage pipe is down (outage_active)")
        sid = sms._send_imessage_poll(phone, title, opts)
    except Exception as e:  # noqa: BLE001 — a poll failing must never kill the reply
        logger.warning("POLL_SEND_FAILED user=%s source=%s title=%r err=%s", user_id, source, title, e)
        return "error: couldn't send the poll — ask in words instead (a short question with the options)"
    sms._log_message(user_id, body, POLL_MESSAGE_TYPE, channel="imessage", provider_sid=sid, delivery_status="sent")
    logger.info("POLL_SENT user=%s source=%s title=%r n=%d id=%s", user_id, source, title, len(opts), sid)
    return ("ok: poll sent as its own bubble — your reply is the one line around it (why you're asking, "
            "or nothing); don't repeat the options or ask the same thing in words")
