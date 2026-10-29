"""
Live 2026-10-28, user 77. At 14:14 the coach swapped "lean back over chair" (his Steelcase reclines) for
a wall thoracic extension and wall angels — and both standing posture reminders (2pm + 8pm daily) kept
"lean back over chair x10". The coach had set_reminder and cancel_reminder but no way to change what a
reminder SAYS and no rule to do it, so the 8pm break would have prescribed the move he'd just been told
to drop. The rows were hand-fixed at 15:2x.

Now: update_reminder rewrites a reminder's text in place — time, days, next fire, sent count untouched —
and voice.md says a change to something a reminder carries updates that row in the same turn. A new
library move in the new text rides its clip on the next fire (the existing unseen-clip path).
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse

OLD = "posture break: chin tucks x10, doorway chest stretch 30s each side, lean back over chair x10"
NEW = "posture break: chin tucks x10, doorway chest stretch 30s each side, wall thoracic extension 30s x2"


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "REMINDERS_ENABLED", True)
    monkeypatch.setattr(config, "UPDATE_REMINDER_TOOL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)
    monkeypatch.setattr(config, "REMINDER_DEMO_LINKS_ENABLED", True)


def _row(db, rid):
    from models import Reminder
    db.expire_all()
    return db.get(Reminder, rid)


def _posture(db, user, at="20:00"):
    from reminders import create_reminder
    return create_reminder(user.id, OLD, at, days="mon,tue,wed,thu,fri,sat,sun")["id"]


def test_text_changes_and_the_schedule_does_not(db, caplog):
    from reminders import update_reminder_text
    u = make_user(db, name="Alexey")
    rid = _posture(db, u)
    row = _row(db, rid)
    row.sent_count = 3
    db.commit()
    before = (row.local_time, row.recur_days, row.fire_at, row.sent_count, row.active, row.source)
    with caplog.at_level(logging.INFO):
        r = update_reminder_text(u.id, rid, NEW)
    assert r == {"id": rid, "text": NEW}
    row = _row(db, rid)
    assert row.text == NEW
    assert (row.local_time, row.recur_days, row.fire_at, row.sent_count, row.active, row.source) == before
    assert any(f"REMINDER_UPDATED user={u.id} id={rid}" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("case", ["other_user", "cancelled", "empty", "missing"])
def test_refuses_what_it_shouldnt_touch(db, case):
    from reminders import update_reminder_text, cancel_reminder
    u = make_user(db, name="Alexey")
    rid = _posture(db, u)
    if case == "other_user":
        other = make_user(db, phone="+15550007777")
        r = update_reminder_text(other.id, rid, NEW)
    elif case == "cancelled":
        cancel_reminder(u.id, rid)
        r = update_reminder_text(u.id, rid, NEW)
    elif case == "empty":
        r = update_reminder_text(u.id, rid, "   ")
    else:
        r = update_reminder_text(u.id, 999999, NEW)
    assert "error" in r
    assert _row(db, rid).text == OLD


def test_the_tool_rewrites_and_reports_the_schedule(db):
    from agent_tools import dispatch_tool, UPDATE_REMINDER_TOOL
    assert UPDATE_REMINDER_TOOL["name"] == "update_reminder"
    assert set(UPDATE_REMINDER_TOOL["input_schema"]["required"]) == {"reminder_id", "text"}
    u = make_user(db, name="Alexey")
    rid = _posture(db, u)
    out = dispatch_tool("update_reminder", {"reminder_id": rid, "text": NEW}, u.id)
    assert out.startswith("ok: reminder updated") and "wall thoracic extension" in out and "8:00pm" in out, out
    assert dispatch_tool("update_reminder", {"reminder_id": "x", "text": NEW}, u.id).startswith("error")
    assert dispatch_tool("update_reminder", {"reminder_id": 999999, "text": NEW}, u.id).startswith("error")


def test_the_loop_offers_it_with_the_reminder_tools(db, anthropic_stub, monkeypatch):
    import agent_loop
    anthropic_stub.reply_with(lambda kw: "ok")
    u = make_user(db, name="Alexey")
    agent_loop.run_agent_loop(u, "hey", "freeform")
    names = {t.get("name") for t in anthropic_stub.calls[-1].get("tools") or []}
    assert {"set_reminder", "cancel_reminder", "update_reminder"} <= names
    monkeypatch.setattr(config, "UPDATE_REMINDER_TOOL_ENABLED", False)
    agent_loop.run_agent_loop(u, "hey", "freeform")
    names = {t.get("name") for t in anthropic_stub.calls[-1].get("tools") or []}
    assert "update_reminder" not in names and "set_reminder" in names


def test_the_live_swap_turn_updates_the_row(db, anthropic_stub, sms_capture):
    """The 14:14 turn, replayed with the model doing what the rule asks: update_reminder, then the reply."""
    import agent_loop
    u = make_user(db, name="Alexey")
    rid = _posture(db, u)
    anthropic_stub.push(ToolUse("update_reminder", {"reminder_id": rid, "text": NEW}, id="toolu_u"),
                        "wall thoracic extension instead: hands on the wall, walk ur feet back, hinge till ur "
                        "chest sinks. updated ur posture breaks too")
    reply = agent_loop.run_agent_loop(u, "is there an alternative for leav bacj over chair? Im sitting on a "
                                         "steelcase", "freeform")
    assert "updated ur posture breaks" in reply
    assert _row(db, rid).text == NEW
    # the model saw the row id it needed: the REMINDERS block in its context
    system = " ".join((b.get("text", "") if isinstance(b, dict) else str(b))
                      for b in (anthropic_stub.calls[0].get("system") or []))
    assert f"id={rid}" in system and "lean back over chair" in system


def test_next_fire_says_the_new_text_and_carries_the_new_clip_once(db, sms_capture, anthropic_stub, monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None:
                        type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())
    from models import Message
    from reminders import update_reminder_text, fire_due
    composed = []
    anthropic_stub.reply_with(lambda kw: composed.append(str(kw.get("messages"))) or
                              "posture break: chin tucks x10, doorway stretch 30s, wall thoracic extension 30s x2")
    u = make_user(db, name="Alexey", seen_exercise_demos={"chin_tuck": True, "doorway_pec_stretch": True,
                                                         "seated_thoracic_extension": True})
    rid = _posture(db, u)
    update_reminder_text(u.id, rid, NEW)
    at = _row(db, rid).fire_at
    assert fire_due(now=at + timedelta(seconds=30)) == 1
    assert composed and "wall thoracic extension" in composed[-1] and "lean back over chair" not in composed[-1]
    db.expire_all()
    out = [m.body for m in db.query(Message).filter_by(user_id=u.id, direction="out").order_by(Message.id)]
    assert out[-1].startswith("how-to clips") and "https://youtu.be/-TLJEN3Ri3c" in out[-1]
    assert "oO9Zernkbxg" not in out[-1], "already-seen clips don't repeat"


def test_voice_says_update_the_reminder_in_the_same_turn():
    import re
    from agent_loop import _voice_prompt
    v = re.sub(r"\s+", " ", _voice_prompt())
    assert "**A change to something a reminder carries**" in v
    assert "update_reminder" in v and "in the SAME turn" in v
    assert "keeps firing the old text" in v
