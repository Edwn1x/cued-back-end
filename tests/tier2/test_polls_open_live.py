"""
Tier-2 (live) anchors for the two prompt-shaped pieces that shipped before polls opened to
everyone (2026-11-26). Binary anchors, x3 each:

  (a) REMINDER, a question: "ask if i have time for internship or research apps" (user 42's
      reminder, three times in words) → the composed line splits into a question + 2–3 options.
  (b) REMINDER, an instruction: "go run" / "take creatine" → one line, no POLL line.
  (c) HEARTBEAT look-back, a did-you check: the tick's first answer is forced to
      send_text("lab get in?") (the live miss, user 51, 2026-11-25); the REAL model answers the
      look-back → send_poll with 2–4 options, nothing sent in words.
  (d) HEARTBEAT look-back, warmth: first answer forced to send_text("how'd the midterm go?") →
      the real model sends the same text again; no poll.
Run: python3 -m pytest tests/tier2/test_polls_open_live.py --run-tier2 -q -p no:cacheprovider -s
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.tier2

ED = dict(name="ed", onboarding_step=3, preferred_channel="imessage", equipment="gym", current_split="ppl",
          workout_days="mon,tue,thu,fri", workout_time="17:00", wake_time="09:00", sleep_time="01:00",
          height_ft=5, height_in=10, weight_lbs=170, age=20, gender="male", goal="muscle_gain",
          calorie_target=2800, protein_target=170)

SUMMARY = ("## Coaching Decisions\n- 2800 cal, 170g protein/day.\n- PPL at RSF, evenings.\n"
           "## Recent Themes\n- Takes CS 61B and CS 70 this term.\n")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _evening_tz():
    """A fixed-offset zone where it's ~8pm for the user now — awake, past the day's classes."""
    offset = (20 - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


@pytest.fixture
def polls_on(monkeypatch):
    import config, sms
    for flag in ("SEND_POLL_TOOL_ENABLED", "IMESSAGE_CHANNEL_ENABLED", "REMINDER_POLL_ENABLED",
                 "HEARTBEAT_POLL_NUDGE_ENABLED"):
        monkeypatch.setattr(config, flag, True)
    monkeypatch.setattr(config, "SEND_POLL_USER_IDS", frozenset())
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    sent = {"text": [], "polls": []}
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body: (sent["text"].append(body), f"photon-{len(sent['text'])}")[1])
    monkeypatch.setattr(sms, "_send_imessage_poll",
                        lambda phone, title, options: (sent["polls"].append((title, list(options))), f"POLL-{len(sent['polls'])}")[1])
    monkeypatch.setattr(sms, "outage_active", lambda: False)
    yield sent
    sms.clear_outage()


def _user(db):
    from tests.factories import make_user
    return make_user(db, **dict(ED, user_timezone=_evening_tz(), coaching_summary=SUMMARY,
                                created_at=_now() - timedelta(days=30)))


@pytest.mark.parametrize("run", [1, 2, 3])
def test_a_question_reminder_composes_as_question_plus_options(db, polls_on, run):
    import reminders
    from models import Reminder
    u = _user(db)
    row = Reminder(user_id=u.id, text="ask if i have time for internship or research apps")
    raw = reminders._compose(u, row)
    question, options = reminders._split_poll(raw)
    print(f"\n[REMINDER-Q run {run}] raw={raw!r}")
    assert options and 2 <= len(options) <= 3, f"no POLL line on a yes/no reminder: {raw!r}"
    # the coach's voice often drops the "?" ("push day today, what time u going") — not asserted
    assert question and "\n" not in question and len(question) <= 120 and "POLL" not in question
    assert all(len(o) <= 40 for o in options)


@pytest.mark.parametrize("run", [1, 2, 3])
@pytest.mark.parametrize("text", ["go run", "take creatine"])
def test_an_instruction_reminder_stays_one_line(db, polls_on, run, text):
    import reminders
    from models import Reminder
    u = _user(db)
    raw = reminders._compose(u, Reminder(user_id=u.id, text=text))
    line, options = reminders._split_poll(raw)
    print(f"\n[REMINDER-DO {text!r} run {run}] raw={raw!r}")
    assert options is None and "POLL" not in raw, f"an instruction went out as a poll: {raw!r}"
    assert line and "\n" not in line.strip()


def _force_first_answer(monkeypatch, message):
    """The tick's first model answer is send_text(message), built from real SDK types so the
    follow-up request serializes; every later call goes to the live model."""
    import anthropic
    from anthropic.types import Message, ToolUseBlock, Usage
    target = anthropic.resources.messages.Messages
    real = target.create
    state = {"n": 0}

    def _create(self, *args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return Message.model_construct(
                id="msg_forced", type="message", role="assistant", model=kwargs.get("model"),
                stop_reason="tool_use", stop_sequence=None,
                content=[ToolUseBlock(id="toolu_forced01", name="send_text", input={"message": message}, type="tool_use")],
                usage=Usage(input_tokens=1, output_tokens=1))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(target, "create", _create)
    return state


def _seed_thread(db, user, *rows):
    """rows: (direction, hours_ago, body), oldest first."""
    from models import Message
    for direction, hours_ago, body in rows:
        db.add(Message(user_id=user.id, direction=direction, body=body, message_type="freeform",
                       created_at=_now() - timedelta(hours=hours_ago)))
    db.commit()


LAB_THREAD = (("in", 6.0, "gotta submit the 61b lab by midnight, then im free"),
              ("out", 5.98, "bet, lab first"))
MIDTERM_THREAD = (("in", 9.0, "cs70 midterm at 2 today, wish me luck"),
                  ("out", 8.98, "u got this, go eat first"))


@pytest.mark.parametrize("run", [1, 2, 3])
def test_the_look_back_turns_a_did_you_check_into_a_poll(db, polls_on, monkeypatch, run):
    import heartbeat
    u = _user(db)
    _seed_thread(db, u, *LAB_THREAD)
    state = _force_first_answer(monkeypatch, "lab get in?")
    spoke, payload, search = heartbeat.decide(u.id)
    print(f"\n[LOOK-BACK did-you run {run}] spoke={spoke} calls={state['n']} :: {payload!r} polls={polls_on['polls']}")
    assert search.get("poll_nudged") is True and state["n"] >= 2
    assert polls_on["polls"], f"the did-you check stayed in words: {payload!r}"
    title, options = polls_on["polls"][0]
    assert 2 <= len(options) <= 4 and spoke is True and payload.startswith('[poll "')
    assert polls_on["text"] == [title], "the question bubble and nothing else"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_the_look_back_leaves_an_open_question_in_words(db, polls_on, monkeypatch, run):
    import heartbeat
    u = _user(db)
    _seed_thread(db, u, *MIDTERM_THREAD)
    state = _force_first_answer(monkeypatch, "how'd the midterm go?")
    spoke, payload, search = heartbeat.decide(u.id)
    print(f"\n[LOOK-BACK open run {run}] spoke={spoke} calls={state['n']} :: {payload!r} polls={polls_on['polls']}")
    assert search.get("poll_nudged") is True
    assert not polls_on["polls"], f"an open-ended question became a poll: {polls_on['polls']}"
    assert spoke is True and "midterm" in payload.lower()
