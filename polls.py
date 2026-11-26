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
  POLL_UNTAPPED_STREAK real polls in a row with no tap → blocked for POLL_UNTAPPED_RETRY_DAYS:
  nothing tells us a recipient's iOS version, and a phone below iOS 26 can't show a poll at
  all — never tapping is the only signal we get (2026-11-26, before opening polls to everyone).

The proactive paths (heartbeat, reminders) offer a poll only where it can land as a poll
(`proactive_ok`: flag, setup done, iMessage, no untapped streak, room under the open cap) —
an SMS numbered list is the reactive coach's call, never an unprompted format.

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
# The question, as its own text bubble right before the poll. iMessage polls have NO title
# (founder 2026-11-14: ours showed choices and no question, and a poll he sent his sister
# person-to-person shows none either), so the poll never carries its question — the bubble
# above it does. Photon's API takes a title it can never render. Its own message_type so
# the open-poll gate (which reads "poll" rows) and the ack window never mistake it.
POLL_QUESTION_MESSAGE_TYPE = "poll_question"
PICK_MARKER = " on the poll "          # what a vote's inbound text always carries
MAX_TITLE_CHARS = 120
MAX_OPTION_CHARS = 40
MAX_TOPIC_CHARS = 60
MIN_OPTIONS = 2
# The one refusal after which something IS already in the thread (the question bubble): a
# caller that falls back to words must send the options, not the question again.
QUESTION_OUT_PREFIX = "error: the question went out as a text bubble"

_ROW_RE = re.compile(r'^\[poll "(?P<title>.*?)"(?: \((?P<topic>[^()]*)\))?: ')


def normalize_topic(raw) -> str:
    """Loose key for "the same question": lowercase, punctuation out, spaces collapsed —
    'Gym time?' and 'gym time' collide, nothing else is inferred."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", str(raw or "").lower()).split())


def send_poll_enabled_for(user_id: int) -> bool:
    """The flag, narrowed to SEND_POLL_USER_IDS when that's set (staged rollout: the founder
    first). One answer for both the loop (offer the tool) and the handler (honour a call)."""
    if not config.SEND_POLL_TOOL_ENABLED:
        return False
    return not config.SEND_POLL_USER_IDS or int(user_id) in config.SEND_POLL_USER_IDS


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


def vote_body(session, user_id: int, vote: dict, *, fallback: str = "") -> str:
    """The inbound text for a vote: '[picked "option" on the poll "title"]' with the title taken
    from OUR outbound row for that poll (provider_sid == poll_id). iMessage polls carry no title
    at all, so the wire's title is "" or the option's own title — the row is the truth.
    Unknown poll_id → the sidecar's text as sent (or a line built from the wire)."""
    from models import Message
    option = " ".join(str(vote.get("option") or "").split())
    picked = "picked" if vote.get("selected") is not False else "un-picked"
    poll_id = str(vote.get("poll_id") or "")
    title = None
    if poll_id:
        row = (session.query(Message)
               .filter(Message.user_id == user_id, Message.direction == "out",
                       Message.message_type == POLL_MESSAGE_TYPE, Message.provider_sid == poll_id)
               .order_by(Message.id.desc()).first())
        if row:
            title = poll_title(row.body)
    if title is None:
        wire = " ".join(str(vote.get("title") or "").split())
        if not wire and fallback:
            return fallback
        title = wire or "?"
    return f'[{picked} "{option or "?"}" on the poll "{title}"]'


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
        if not _picked(session, user_id, row):
            still_open.append(row)
    return still_open


def _picked(session, user_id: int, row) -> bool:
    """A later inbound names a pick on this poll row."""
    from models import Message
    title = poll_title(row.body)
    return bool(session.query(Message.id)
                .filter(Message.user_id == user_id, Message.direction == "in", Message.id > row.id,
                        Message.body.contains(f'{PICK_MARKER}"{title}"' if title else PICK_MARKER)).first())


def untapped_streak(session, user_id: int) -> bool:
    """True when their last POLL_UNTAPPED_STREAK real polls (iMessage, sent as polls — an SMS
    list is answered in words and never counts) all went without a tap AND the newest is
    younger than POLL_UNTAPPED_RETRY_DAYS. We can't see a recipient's iOS version and a phone
    that can't show a poll gets a question bubble with nothing under it; a run of untapped
    polls is the only signal. After the retry window one more poll may go out — a tap on it
    ends the streak."""
    from models import Message
    n = int(getattr(config, "POLL_UNTAPPED_STREAK", 3))
    if n <= 0:
        return False
    rows = (session.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "out",
                    Message.message_type == POLL_MESSAGE_TYPE, Message.channel == "imessage",
                    Message.provider_sid.isnot(None))
            .order_by(Message.id.desc()).limit(n).all())
    if len(rows) < n:
        return False
    retry = timedelta(days=float(getattr(config, "POLL_UNTAPPED_RETRY_DAYS", 14)))
    newest = rows[0].created_at
    if newest is not None and datetime.now(timezone.utc).replace(tzinfo=None) - newest.replace(tzinfo=None) >= retry:
        return False
    return not any(_picked(session, user_id, r) for r in rows)


def gate(session, user_id: int, topic: str, *, source: str = "coach") -> str | None:
    """The reason this poll can't go out now, or None. Same topic open → the re-ask; too many
    open → they aren't tapping, ask in words; a run of untapped polls → their phone likely
    can't show one (an admin send skips that last check — it's how the founder re-tests)."""
    open_ = open_polls(session, user_id)
    for row in open_:
        if poll_topic(row.body) == topic:
            return (f"error: that question is already out as a poll ({row.body}) — wait for their pick "
                    "or move on; don't re-ask it")
    if len(open_) >= config.POLL_MAX_OUTSTANDING:
        return (f"error: {len(open_)} polls are already unanswered — they aren't tapping right now, "
                "ask in words instead")
    if source != "admin" and untapped_streak(session, user_id):
        return (f"error: their last {int(getattr(config, 'POLL_UNTAPPED_STREAK', 3))} polls got no tap — "
                "their phone likely can't show polls (or they'd rather type); ask in words")
    return None


def proactive_ok(user_id: int) -> bool:
    """May a PROACTIVE path (heartbeat, reminder) ask this person with a poll right now?
    The flag, setup done, on iMessage (no unprompted numbered lists over SMS), no untapped
    streak, and room under the open cap. The topic gate still runs at send time. Never raises."""
    if not send_poll_enabled_for(user_id):
        return False
    try:
        import sms
        from models import get_session, User
        session = get_session()
        try:
            user = session.get(User, user_id)
            if not user or not user.phone or getattr(user, "archived_at", None):
                return False
            if (user.onboarding_step or 0) < 3:
                return False
            if len(open_polls(session, user_id)) >= config.POLL_MAX_OUTSTANDING:
                return False
            if untapped_streak(session, user_id):
                return False
        finally:
            session.close()
        return sms._resolve_channel(user_id) == "imessage"
    except Exception as e:  # noqa: BLE001 — an unknown answer is "ask in words"
        logger.warning("POLL_PROACTIVE_CHECK_FAILED user=%s err=%s", user_id, e)
        return False


# A lone short question is the shape the proactive misses took after polls shipped ("lab get
# in?", 2026-11-25 00:19 PT, eight days after did-you checks became polls in the prompt).
_URL_RE = re.compile(r"https?://|\bwww\.", re.I)


def lone_short_question(text) -> bool:
    """One line, exactly one '?', nothing after it but an emoji or two, at most
    POLL_NUDGE_MAX_WORDS words, no link. Says nothing about whether it's a PICK — that
    judgement stays with the model; this only decides whether the text is worth one look back."""
    raw = str(text or "").strip()
    if not raw or "\n" in raw or _URL_RE.search(raw) or raw.count("?") != 1:
        return False
    tail = raw.split("?", 1)[1]
    if any(ch.isalnum() for ch in tail):
        return False
    return len(raw.split()) <= int(getattr(config, "POLL_NUDGE_MAX_WORDS", 12))


def record_note(user_id: int, source: str, title, note: str) -> None:
    """A poll_attempts row for something that ISN'T a send_poll call — a question sent back
    once to be reconsidered as a poll — so 'offered vs taken' survives the log rotation."""
    _record_attempt(user_id, source, title, None, note)


def _record_attempt(user_id: int, source: str, title, topic, out: str) -> None:
    """poll_attempts row for every call, ok or refused — best-effort, own session."""
    try:
        from models import get_session, PollAttempt
        s = get_session()
        try:
            s.add(PollAttempt(user_id=user_id, source=source,
                              title=" ".join(str(title or "").split())[:200] or None,
                              topic=(normalize_topic(topic) or normalize_topic(title) or None),
                              ok=out.startswith("ok:"), result=out[:2000]))
            s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001 — the ledger never costs the send
        logger.warning("POLL_ATTEMPT_RECORD_FAILED user=%s err=%s", user_id, e)


def send_poll_to_user(user_id: int, title, options, *, topic=None, source: str = "coach") -> str:
    """Validate, gate, send over the user's channel, log the row, and record the attempt
    (poll_attempts) whatever the outcome. Returns the status line for the coach loop:
    'ok: …' or 'error: …'. Never raises."""
    out = _send_poll_to_user(user_id, title, options, topic=topic, source=source)
    _record_attempt(user_id, source, title, topic, out)
    return out


def _send_poll_to_user(user_id: int, title, options, *, topic=None, source: str = "coach") -> str:
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
        if source != "admin" and (user.onboarding_step or 0) < 3:
            return "error: no polls during setup — ask in words until their setup is done"
        blocked = gate(session, user_id, topic_n, source=source)
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
    # The question goes out FIRST as its own text bubble, then the poll under it: iMessage polls have no
    # title field at all (founder 2026-11-14, confirmed person-to-person), so the bubble above
    # is the only place the question shows. If the poll then fails, the question is already asked — the model lists the options.
    try:
        if sms.outage_active():
            raise RuntimeError("imessage pipe is down (outage_active)")
        sms.send_sms(phone, title, user_id=user_id, message_type=POLL_QUESTION_MESSAGE_TYPE)
    except Exception as e:  # noqa: BLE001 — nothing is out yet; the model asks in words
        logger.warning("POLL_QUESTION_SEND_FAILED user=%s source=%s err=%s", user_id, source, e)
        return "error: couldn't send the poll — ask in words instead (a short question with the options)"
    try:
        sid = sms._send_imessage_poll(phone, title, opts)
    except Exception as e:  # noqa: BLE001 — the question is out; the options must follow in words
        logger.warning("POLL_SEND_FAILED user=%s source=%s title=%r err=%s", user_id, source, title, e)
        return (QUESTION_OUT_PREFIX + " but the poll itself didn't — send the "
                "options in words now, one short line, don't repeat the question")
    sms._log_message(user_id, body, POLL_MESSAGE_TYPE, channel="imessage", provider_sid=sid, delivery_status="sent")
    logger.info("POLL_SENT user=%s source=%s title=%r n=%d id=%s", user_id, source, title, len(opts), sid)
    return ("ok: poll sent as its own bubble with the question as a text bubble beside it — reply with "
            "nothing, or one short line of why you're asking; never restate the question or the options")
