"""
Polls — inbound kinds initiative, steps 7 + 8 (2026-11-14). Plumbing only; the first
use-case (gym time today? / session feel / meal off the menu…) is the founder's pick.

Outbound: `send_poll_to_user` puts an iMessage poll in the thread through the sidecar's
/poll (iOS 26 polls) and logs ONE outbound row, message_type "poll", whose provider_sid
is the poll's Photon id — the prefix of every vote's id. An SMS user gets the same ask as
a numbered list ("1. … 2. …") on the same row type, so the open-poll gate and the history
window read both the same way. Rules the tool enforces in code: 2–POLL_MAX_OPTIONS short,
distinct options; a short title; setup finished (onboarding_step >= 3); and the gate:

  The gate keys on the QUESTION, not on "a poll exists" (founder 2026-11-14: someone who is
  engaged but skipped one poll must not be locked out of unrelated ones). Every poll carries a
  free-form `topic` — whatever the asker names, no vocabulary — defaulting to its title. A poll
  is OPEN until a later inbound names a pick on it or POLL_LIFETIME_HOURS pass. Same topic still
  open → blocked (that's the re-ask, the nag). POLL_MAX_OUTSTANDING open polls → blocked (two
  ignored taps is the signal they don't want tappable asks right now; ask in words).

The judgement rule — logistics and self-report only, never a programming decision for a
beginner — lives in voice.md ("you prescribe; you don't poll") and the tool description.

Inbound: a vote lands on /internal/inbound as text naming the pick ('[picked "5:30" on the
poll "gym time today?"]') plus a structured `poll` field; it is stored and answered like any
reply. A deselect is dropped before any model turn (app.py). Code routing of a vote to the
ask that sent it (set the time, log the meal) keys on the topic — for topics code knows;
anything else lands as text for the model, like today.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.polls")

POLL_MESSAGE_TYPE = "poll"
PICK_MARKER = " on the poll "          # what a vote's inbound text always carries
MAX_TITLE_CHARS = 120
MAX_OPTION_CHARS = 40
MAX_TOPIC_CHARS = 60
MIN_OPTIONS = 2

_ROW_RE = re.compile(r'^\[poll "(?P<title>.*?)"(?: \((?P<topic>[^()]*)\))?: ')


def normalize_topic(raw) -> str:
    """Loose key for "the same question": lowercase, punctuation out, spaces collapsed —
    'Gym time?' and 'gym time' collide, nothing else is inferred."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(raw or "").lower()).split())


def poll_row_body(title: str, options: list[str], topic: str | None = None) -> str:
    """'[poll "title": a / b]', or '[poll "title" (topic): a / b]' when the topic isn't just the
    title — the row is the only place the topic lives, and the window reads it as prose."""
    t = normalize_topic(topic) if topic else ""
    tag = f" ({t})" if t and t != normalize_topic(title) else ""
    return f'[poll "{title}"{tag}: ' + " / ".join(options) + "]"


def poll_title(body: str) -> str | None:
    m = _ROW_RE.match(body or "")
    return m.group("title") if m else None


def poll_topic(body: str) -> str | None:
    """The normalized topic a poll row carries (the explicit tag, else its title)."""
    m = _ROW_RE.match(body or "")
    if not m:
        return None
    return normalize_topic(m.group("topic") or m.group("title"))


def numbered_list(title: str, options: list[str]) -> str:
    """The SMS / no-poll fallback: the same ask as a list they answer by number or words."""
    return title + "\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))


def validate(title, options, topic=None) -> tuple[str, list[str], str] | str:
    """Clean (title, options, topic) or an 'error: …' line the model can act on. The topic is
    free-form (any short tag the asker picks) and defaults to the title."""
    title = " ".join(str(title or "").split())
    if not title:
        return "error: title is required — the one-line question the poll asks"
    if len(title) > MAX_TITLE_CHARS:
        return f"error: title over {MAX_TITLE_CHARS} chars — a poll question is one short line"
    topic_raw = " ".join(str(topic or "").split())
    if len(topic_raw) > MAX_TOPIC_CHARS:
        return f"error: topic over {MAX_TOPIC_CHARS} chars — a two-or-three word tag for what this asks"
    topic_n = normalize_topic(topic_raw) or normalize_topic(title)
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
    return title, opts, topic_n


def open_polls(session, user_id: int) -> list:
    """Outbound poll rows within POLL_LIFETIME_HOURS that no later inbound answered, newest
    first. 'Answered' = a later inbound row that names a pick ON THAT POLL (the vote text
    carries the poll's title), so one pick never closes a different poll."""
    from models import Message
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=config.POLL_LIFETIME_HOURS)
    rows = (session.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "out",
                    Message.message_type == POLL_MESSAGE_TYPE, Message.created_at >= since)
            .order_by(Message.id.desc()).all())
    still_open = []
    for row in rows:
        title = poll_title(row.body)
        answered = (session.query(Message.id)
                    .filter(Message.user_id == user_id, Message.direction == "in", Message.id > row.id,
                            Message.body.contains(f'{PICK_MARKER}"{title}"' if title else PICK_MARKER)).first())
        if not answered:
            still_open.append(row)
    return still_open


def gate(session, user_id: int, topic: str) -> str | None:
    """The reason this poll can't go out now, or None. Same topic open → the re-ask; too many
    open → they aren't tapping, ask in words."""
    open_ = open_polls(session, user_id)
    for row in open_:
        if poll_topic(row.body) == topic:
            return (f"error: that question is already out as a poll ({row.body}) — wait for their pick "
                    "or move on; don't re-ask it")
    if len(open_) >= config.POLL_MAX_OUTSTANDING:
        return (f"error: {len(open_)} polls are already unanswered — they aren't tapping right now, "
                "ask in words instead")
    return None


def send_poll_to_user(user_id: int, title, options, *, topic=None, source: str = "coach") -> str:
    """Validate, gate, send over the user's channel, log the row. Returns the status line
    for the coach loop: 'ok: …' or 'error: …'. Never raises."""
    import sms
    from models import get_session, User

    cleaned = validate(title, options, topic)
    if isinstance(cleaned, str):
        return cleaned
    title, opts, topic_n = cleaned

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.phone:
            return "error: no phone on file"
        if getattr(user, "archived_at", None):
            return "error: this account is archived"
        if source == "coach" and (user.onboarding_step or 0) < 3:
            return "error: no polls during setup — ask in words until their setup is done"
        blocked = gate(session, user_id, topic_n)
        if blocked:
            return blocked
        phone = user.phone
    finally:
        session.close()

    body = poll_row_body(title, opts, topic_n)
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
