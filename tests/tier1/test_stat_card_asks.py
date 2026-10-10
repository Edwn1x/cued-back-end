"""An outright ask for a stat card queues it in CODE before the model turn.

Live 2026-10-08 (user 48): "Ok send me my macros" → text, no card (the model ended the turn
with no tool call); "What's my week look like" → a text re-list, no card. The send_stat_card
tool existed and nothing refused it; the model didn't reach for it. Now the ask itself
decides (same guards as the tool), the card rides the turn's queue, and the model is told
it's going out — one line around it. When the week card goes, the #195 schedule rundown
becomes the model's reference instead of the thing to relay."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")


def _seen_text(calls) -> str:
    out = []
    for c in calls:
        sysm = c.get("system")
        out.append(sysm if isinstance(sysm, str) else "".join(b.get("text", "") for b in (sysm or [])))
        for m in c.get("messages") or []:
            out.append(str(m.get("content")))
    return "\n".join(out)


def _queued(uid) -> list:
    from agent_tools import pop_turn_state
    return pop_turn_state(uid).get("stat_cards") or []


@pytest.mark.parametrize("text, kind", [
    ("Ok send me my macros", "macros"),
    ("how am i doing today", "macros"),
    ("where am i at on protein", "macros"),
    ("what's left today", "macros"),
    ("how's my day looking", "macros"),
    ("What's my week look like", "week"),
    ("Send me my week", "week"),
    ("rest of the week?", "week"),
    ("so cooked this week", "week"),
    ("how packed is rsf", "rsf"),
    ("is the gym dead rn", "rsf"),
    ("Send me the rsf capacity", "rsf"),
    ("rsf right now?", "rsf"),
    # not card asks: schedule questions the card doesn't answer, meals, generic
    ("what do i have friday", None),
    ("whats due", None),
    ("ate a burger, 600 cal", None),
    ("my week was rough", None),
    ("going to the gym", None),
    ("what's a good push day", None),
    ("", None),
])
def test_stat_card_ask_regex(text, kind):
    from agent_loop import _stat_card_asked
    assert _stat_card_asked(text) == kind


def test_macros_ask_queues_the_card_and_tells_the_model(db, anthropic_stub):
    import agent_loop
    u = make_user(db, calorie_target=2450, protein_target=139)
    anthropic_stub.reply_with(lambda kw: "ur at 0, long way to go")
    agent_loop.run_agent_loop(u, "Ok send me my macros", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "## STAT CARD (going out right after your reply — the today card)" in seen
    assert "don't call send_stat_card for this kind" in seen
    assert _queued(u.id) == ["macros"]


def test_tool_call_for_the_code_queued_kind_is_a_no_op(db, anthropic_stub):
    """The model calling send_stat_card anyway must not double-send."""
    import agent_loop
    from agent_tools import handle_send_stat_card, peek_turn_state
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "k")
    agent_loop.run_agent_loop(u, "send me my macros", "freeform")
    assert peek_turn_state(u.id).get("stat_cards") == ["macros"]
    r = handle_send_stat_card(u.id, {"kind": "macros"})
    assert r.startswith("ok: the macros card is already going out")
    assert _queued(u.id) == ["macros"]


def _connected_gcal(uid):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=uid, provider="gcal", status="connected", meta={}))
        s.commit()
    finally:
        s.close()


def _event(uid, title, start_local, hours=1, *, source="gcal", all_day=False):
    from events import upsert_external_event
    st = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    en = (start_local + timedelta(hours=hours)).astimezone(timezone.utc).replace(tzinfo=None)
    upsert_external_event(uid, source=source, external_id=title, title=title, occurred_at=st,
                          ends_at=None if all_day else en, all_day=all_day)


def test_week_ask_queues_the_card_and_the_rundown_becomes_reference(db, anthropic_stub, monkeypatch):
    import config, agent_loop
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    u = make_user(db)
    _connected_gcal(u.id)
    tomorrow = (datetime.now(PT) + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    _event(u.id, "CS 70 Lecture", tomorrow)
    _event(u.id, "due: Homework 4 [CS70]", tomorrow.replace(hour=16), source="bcourses")
    anthropic_stub.reply_with(lambda kw: "hw4 tmrw is the one")
    agent_loop.run_agent_loop(u, "What's my week look like", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "## STAT CARD (going out right after your reply" in seen
    assert "the WEEK CARD is going out after your reply" in seen
    assert "do NOT list the days" in seen
    assert "relay it, don't rebuild it" not in seen          # the full-relay header is gone
    assert "Homework 4" in seen                               # the rundown still rides as reference
    assert _queued(u.id) == ["week"]


def test_week_ask_with_an_empty_grid_sends_no_card_and_keeps_the_full_rundown(db, anthropic_stub):
    import agent_loop
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "nothing on it yet")
    agent_loop.run_agent_loop(u, "send me my week", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "## STAT CARD" not in seen
    assert "## SCHEDULE RUNDOWN (built in code for THIS question" in seen
    assert _queued(u.id) == []


def test_rsf_ask_when_closed_sends_no_card_and_says_so(db, anthropic_stub, monkeypatch):
    import agent_loop, integrations.rsf
    monkeypatch.setattr(integrations.rsf, "is_open", lambda now=None: False)
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "rsf's closed")
    agent_loop.run_agent_loop(u, "Send me the rsf capacity", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "## STAT CARD\nThey asked for the rsf card but there's none right now (closed right now)" in seen
    assert "never say 'here's the card'" in seen
    assert _queued(u.id) == []


def test_rsf_ask_when_open_queues_the_card(db, anthropic_stub, monkeypatch):
    import agent_loop, integrations.rsf
    from models import GymOccupancy
    from integrations.rsf import FACILITY
    monkeypatch.setattr(integrations.rsf, "is_open", lambda now=None: True)
    db.add(GymOccupancy(facility=FACILITY, ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2), pct=18))
    db.commit()
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "dead. go")
    agent_loop.run_agent_loop(u, "how packed is rsf", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "the rsf right now card" in seen and "18% full" in seen
    assert _queued(u.id) == ["rsf"]


def test_repeat_within_the_window_is_skipped_with_a_pointer(db, anthropic_stub):
    import agent_loop
    from models import Message
    u = make_user(db)
    db.add(Message(user_id=u.id, direction="out", body="today", message_type="stat_card_macros",
                   created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)))
    db.commit()
    anthropic_stub.reply_with(lambda kw: "same as the card above")
    agent_loop.run_agent_loop(u, "send me my macros", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "you sent it 5 min ago" in seen and "Don't resend" in seen
    assert _queued(u.id) == []


def test_flag_off_or_tool_off_adds_nothing(db, anthropic_stub, monkeypatch):
    import config, agent_loop
    u = make_user(db)
    monkeypatch.setattr(config, "STAT_CARD_ASK_IN_CODE_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "k")
    agent_loop.run_agent_loop(u, "send me my macros", "freeform")
    assert "## STAT CARD" not in _seen_text(anthropic_stub.calls)
    assert _queued(u.id) == []
    monkeypatch.setattr(config, "STAT_CARD_ASK_IN_CODE_ENABLED", True)
    monkeypatch.setattr(config, "STAT_CARD_TOOL_ENABLED", False)
    anthropic_stub.calls.clear()
    anthropic_stub.reply_with(lambda kw: "k")
    agent_loop.run_agent_loop(u, "send me my macros", "freeform")
    assert "## STAT CARD" not in _seen_text(anthropic_stub.calls)
    assert _queued(u.id) == []


def test_identity_names_the_card_as_the_answer():
    text = open("prompts/identity.md", encoding="utf-8").read()
    assert "The card is the answer, the text is the caption." in text
    assert "send_stat_card" in text
