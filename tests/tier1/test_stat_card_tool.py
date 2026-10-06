"""send_stat_card: the coach drops an rsf / macros / week picture card into the thread.
The tool validates and QUEUES; app.py sends the card right after the reply text, so
the thread reads coach line → card (the founder's mockups)."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_user
from tests._fake_anthropic import ToolUse


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def turn():
    from agent_tools import begin_turn, pop_turn_state
    started = []

    def _start(uid):
        begin_turn(uid)
        started.append(uid)
    yield _start
    for uid in started:
        pop_turn_state(uid)


def test_tool_queues_the_card_and_tells_the_coach_what_it_shows(db, turn):
    from agent_tools import dispatch_tool, peek_turn_state
    u = make_user(db, calorie_target=2400, protein_target=160)
    turn(u.id)
    out = dispatch_tool("send_stat_card", {"kind": "macros"}, u.id)
    assert out.startswith("ok: the today card goes out right after your reply") and "Don't repeat" in out
    assert peek_turn_state(u.id)["stat_cards"] == ["macros"]
    assert dispatch_tool("send_stat_card", {"kind": "macros"}, u.id).startswith("ok: the macros card is already going")
    assert peek_turn_state(u.id)["stat_cards"] == ["macros"]


def test_tool_caps_cards_per_turn_and_rejects_bad_kinds(db, turn):
    from agent_tools import dispatch_tool
    u = make_user(db)
    turn(u.id)
    assert dispatch_tool("send_stat_card", {"kind": "steps"}, u.id).startswith("error: kind must be one of")
    assert dispatch_tool("send_stat_card", {"kind": "macros"}, u.id).startswith("ok")
    assert dispatch_tool("send_stat_card", {"kind": "week"}, u.id).startswith("ok")
    assert dispatch_tool("send_stat_card", {"kind": "rsf"}, u.id).startswith("error: already sending 2 cards")


def test_tool_refuses_a_repeat_within_20_minutes(db, turn):
    from models import Message
    from agent_tools import dispatch_tool
    u = make_user(db)
    db.add(Message(user_id=u.id, direction="out", body="[today card]", message_type="stat_card_macros",
                   channel="imessage", created_at=_utcnow() - timedelta(minutes=5)))
    db.commit()
    turn(u.id)
    out = dispatch_tool("send_stat_card", {"kind": "macros"}, u.id)
    assert out.startswith("error: you sent the macros card 5 min ago")
    db.query(Message).filter(Message.user_id == u.id).update({"created_at": _utcnow() - timedelta(minutes=25)})
    db.commit()
    assert dispatch_tool("send_stat_card", {"kind": "macros"}, u.id).startswith("ok")


def test_tool_errors_when_the_meter_has_nothing_true(db, turn, monkeypatch):
    """No reading → error, nothing queued, so the coach can't promise a card."""
    import integrations.rsf
    from agent_tools import dispatch_tool, peek_turn_state
    monkeypatch.setattr(integrations.rsf, "is_open", lambda now=None: True)
    u = make_user(db)
    turn(u.id)
    out = dispatch_tool("send_stat_card", {"kind": "rsf"}, u.id)
    assert out.startswith("error: no rsf card right now") and "no numbers invented" in out
    assert not peek_turn_state(u.id).get("stat_cards")


def test_the_card_goes_out_after_the_reply_text(driver, db, anthropic_stub, monkeypatch):
    """Full inbound turn: the model calls the tool, then replies; the reply lands first,
    the card second (an SMS user here, so the card is the link text)."""
    import config
    monkeypatch.setattr(config, "STAT_CARD_BASE_URL", "https://web.test")
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    u = make_user(db, calorie_target=2400, protein_target=160)
    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        assert any(t.get("name") == "send_stat_card" for t in kw["tools"])
        loop_calls.append(1)
        return ToolUse("send_stat_card", {"kind": "macros"}) if len(loop_calls) == 1 else "logged it."
    anthropic_stub.reply_with(handler)
    out = driver.send(u, "how am i doing today")
    assert out[0] == "logged it."
    assert out[1].startswith("today: https://web.test/card/stat/macros?t=")
    from models import Message
    db.expire_all()
    assert db.query(Message).filter(Message.user_id == u.id, Message.message_type == "stat_card_macros").count() == 1


def test_tool_is_offered_only_behind_its_flag(monkeypatch):
    import config
    import agent_loop
    src = open(agent_loop.__file__).read()
    assert "if config.STAT_CARD_TOOL_ENABLED:" in src and "SEND_STAT_CARD_TOOL" in src
    assert config.STAT_CARD_TOOL_ENABLED is True
