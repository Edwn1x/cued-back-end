"""
The reactive turn hands its tools the inbound message id. Found 2026-10-28 (user 51's calendar
loop): every live pending_calendar_event carried turn=null — app.py never passed message_id to
run_agent_loop, so dispatch_tool got None and everything keyed on it was inert in prod while
green in tests (they pass message_id by hand): the confirm gate's same-turn check, the card
early-send gate (#260 — `if ... and message_id:`), the past-report regex (#278), and the
remember tool's safety-entry trigger.
"""
from __future__ import annotations


def _capture(monkeypatch):
    import app
    import config
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    seen = []
    monkeypatch.setattr(app, "run_agent_loop", lambda user, body, mtype, **kw: seen.append(kw) or "got it")
    return seen


def _last_inbound_id(user_id):
    from models import get_session, Message
    s = get_session()
    try:
        return (s.query(Message.id).filter(Message.user_id == user_id, Message.direction == "in")
                .order_by(Message.id.desc()).first())[0]
    finally:
        s.close()


def test_the_reactive_turn_passes_the_inbound_message_id(db, driver, monkeypatch):
    from tests.factories import make_user
    seen = _capture(monkeypatch)
    user = make_user(db)
    driver.send(user, "yeah")
    assert seen and seen[-1].get("message_id") == _last_inbound_id(user.id)


def test_each_turn_gets_its_own_id(db, driver, monkeypatch):
    from tests.factories import make_user
    seen = _capture(monkeypatch)
    user = make_user(db)
    driver.send(user, "add the midterm thursday 7-9")
    first = _last_inbound_id(user.id)
    driver.send(user, "yeah")
    second = _last_inbound_id(user.id)
    assert first != second
    assert [kw.get("message_id") for kw in seen[-2:]] == [first, second]
