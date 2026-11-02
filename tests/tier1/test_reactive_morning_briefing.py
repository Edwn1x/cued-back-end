"""
They texted first this morning → the reply IS the day's briefing.

Live 2026-10-29, user 77 (Alexey): the late-wake hold (#301) kept the 07:34 and 08:20 ticks
quiet; his "Good morning" at 08:44 made the activity wake 08:44 and — being a text — also
counted as "talked since they woke", so the heartbeat's morning open was never owed again.
The reply was "morning" + two lines; no weather, no water line, no due-today, and the 09:00
water GIF still fired. With a wake detected from a text, the briefing was dead by construction.

Fix: heartbeat._morning_open_signal(reactive=True) — same window and material, their own
texts not counted as talking — rides the reply turn (agent_loop._morning_briefing_block); the
reply gets the tick's send-time check (weather / water / due-today) and folds the first
water-GIF slot (_finish_reactive_briefing).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
DAY = (2026, 10, 29)   # the Thursday it happened


def _local(h, mi=0, s=0):
    return datetime(*DAY, h, mi, s, tzinfo=PT)


def _naive(aware):
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _msg(db, user_id, direction, when_aware, body="hey", message_type="freeform"):
    from models import Message
    db.add(Message(user_id=user_id, direction=direction, body=body, message_type=message_type,
                   created_at=_naive(when_aware)))
    db.commit()


def _ctx(db, **kw):
    from models import get_session, User
    u = make_user(db, **kw)
    s = get_session()
    return s, s.get(User, u.id)


@pytest.fixture
def briefing_on(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", True)
    monkeypatch.setattr(config, "WAKE_MODEL_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_DAILY_BRIEFING_ENABLED", True)
    monkeypatch.setattr(config, "WEATHER_ENABLED", False)          # no network in tier-1
    monkeypatch.setattr(config, "REACTIVE_MORNING_BRIEFING_ENABLED", True)
    monkeypatch.setattr(config, "REACTIVE_BRIEFING_LATEST_LOCAL_HOUR", 13)


# ─── knobs ────────────────────────────────────────────────────────────────────

def test_knob_defaults():
    assert config.REACTIVE_MORNING_BRIEFING_ENABLED is True
    assert config.REACTIVE_BRIEFING_LATEST_LOCAL_HOUR == 13


def test_the_tick_instruction_is_unchanged_and_the_reactive_one_shares_its_requirements():
    import heartbeat
    tick, rx = heartbeat._BRIEFING_INSTRUCTION, heartbeat._REACTIVE_BRIEFING_INSTRUCTION
    assert heartbeat._BRIEFING_REQUIRED in tick and heartbeat._BRIEFING_REQUIRED in rx
    assert tick.startswith("A daily briefing: ONE short, warm rundown")
    assert "not a reason to speak" in tick and "not a reason to speak" not in rx
    assert "THIS REPLY is the day's briefing" in rx and "here's your briefing" in rx
    for s in (tick, rx):
        assert s.index("weather") < s.index("big glass of water first") < s.index("due TODAY")


# ─── the live case: a text-sourced wake used to kill the briefing ─────────────

def test_alexey_good_morning_at_0844_owes_the_briefing_on_the_reply_turn(db, briefing_on):
    """Stated wake 07:00, nothing from the coach since; 'Good morning' at 08:44. The tick's
    signal is None (his text counts as talked) — the reactive one renders, anchored to the
    08:44 activity wake, with the material and the reply-is-the-briefing instruction."""
    import heartbeat
    s, user = _ctx(db, wake_time="07:00", sleep_time="00:00")
    try:
        _msg(s, user.id, "in", _local(8, 44), "Good morning")
        now = _local(8, 44, 30)
        assert heartbeat._morning_open_signal(user, s, now=now) is None          # the bug
        st: dict = {}
        block = heartbeat._morning_open_signal(user, s, now=now, state=st, reactive=True)
    finally:
        s.close()
    assert block, "reactive morning open should render"
    assert "8:44am wake" in block and "wake source: activity" in block, block
    assert "their text just now is the first word" in block
    assert "Briefing material:" in block and "Water:" in block
    assert "THIS REPLY is the day's briefing" in block
    assert st["briefing"]["water"] is True and "nutrition" in st["briefing"]


def test_a_briefing_the_tick_already_sent_closes_it(db, briefing_on):
    """Early riser: the 07:29 tick sent the briefing; their 08:00 text is not a second one."""
    import heartbeat
    s, user = _ctx(db, wake_time="07:00", sleep_time="23:00")
    try:
        _msg(s, user.id, "out", _local(7, 29), "51° and clear. lab 12-7", message_type="heartbeat")
        _msg(s, user.id, "in", _local(8, 0), "morning")
        assert heartbeat._morning_open_signal(user, s, now=_local(8, 0, 30), reactive=True) is None
    finally:
        s.close()


def test_an_earlier_exchange_this_morning_closes_it(db, briefing_on):
    """07:05 'hey' → 07:06 reply (that reply was the morning text); 08:00 text → nothing owed."""
    import heartbeat
    s, user = _ctx(db, wake_time="07:00", sleep_time="23:00")
    try:
        _msg(s, user.id, "in", _local(7, 5), "hey")
        _msg(s, user.id, "out", _local(7, 6), "morning. 51° and clear, big glass of water first")
        _msg(s, user.id, "in", _local(8, 0), "what's for lunch")
        assert heartbeat._morning_open_signal(user, s, now=_local(8, 0, 30), reactive=True) is None
    finally:
        s.close()


def test_a_water_gif_or_code_step_since_wake_is_not_the_coach_talking(db, briefing_on):
    """Only the COACH talking closes it: a fired water GIF at 07:15 and a code-sent connect
    offer don't — same exclusions as the tick."""
    import heartbeat
    s, user = _ctx(db, wake_time="07:00", sleep_time="23:00")
    try:
        _msg(s, user.id, "out", _local(7, 15), "[water-break gif]", message_type="water_reminder")
        _msg(s, user.id, "out", _local(7, 20), "connect ur calendar?", message_type="connect_offer")
        _msg(s, user.id, "in", _local(8, 0), "morning")
        assert heartbeat._morning_open_signal(user, s, now=_local(8, 0, 30), reactive=True)
    finally:
        s.close()


def test_an_afternoon_first_text_is_not_a_morning(db, briefing_on, monkeypatch):
    """First text at 13:10 → None under the 13:00 cap; the knob raised to 15 lets it through."""
    import heartbeat
    s, user = _ctx(db, wake_time="07:00", sleep_time="00:00")
    try:
        _msg(s, user.id, "in", _local(13, 10), "yo")
        assert heartbeat._morning_open_signal(user, s, now=_local(13, 10, 30), reactive=True) is None
        monkeypatch.setattr(config, "REACTIVE_BRIEFING_LATEST_LOCAL_HOUR", 15)
        assert heartbeat._morning_open_signal(user, s, now=_local(13, 10, 30), reactive=True)
    finally:
        s.close()


def test_reactive_needs_the_daily_briefing_on(db, briefing_on, monkeypatch):
    import heartbeat
    monkeypatch.setattr(config, "CALENDAR_DAILY_BRIEFING_ENABLED", False)
    s, user = _ctx(db, wake_time="07:00", sleep_time="00:00")
    try:
        _msg(s, user.id, "in", _local(8, 44), "Good morning")
        assert heartbeat._morning_open_signal(user, s, now=_local(8, 44, 30), reactive=True) is None
    finally:
        s.close()


def test_the_tick_path_is_byte_for_byte_unchanged(db, briefing_on):
    """reactive=False renders exactly what it did: 'nobody has texted since they woke' + the
    tick instruction (a 07:20 tick with no messages since a 07:00 wake)."""
    import heartbeat
    s, user = _ctx(db, wake_time="07:00", sleep_time="23:00")
    try:
        block = heartbeat._morning_open_signal(user, s, now=_local(7, 20))
    finally:
        s.close()
    assert block and "nobody has texted since they woke" in block
    assert block.endswith(heartbeat._BRIEFING_INSTRUCTION)
    assert "THIS REPLY" not in block


# ─── the agent-loop block ─────────────────────────────────────────────────────

def test_block_gates_flag_and_setup(db, briefing_on, monkeypatch):
    import agent_loop
    s, user = _ctx(db, wake_time="07:00", sleep_time="00:00")
    try:
        _msg(s, user.id, "in", _local(8, 44), "Good morning")
        now = _local(8, 44, 30)
        block, parts = agent_loop._morning_briefing_block(user, "Good morning", s, now=now)
        assert block and parts and parts["water"] is True
        monkeypatch.setattr(config, "REACTIVE_MORNING_BRIEFING_ENABLED", False)
        assert agent_loop._morning_briefing_block(user, "Good morning", s, now=now) == (None, None)
    finally:
        s.close()
    s2, mid_setup = _ctx(db, wake_time="07:00", sleep_time="00:00", onboarding_step=2)
    try:
        monkeypatch.setattr(config, "REACTIVE_MORNING_BRIEFING_ENABLED", True)
        _msg(s2, mid_setup.id, "in", _local(8, 44), "Good morning")
        assert agent_loop._morning_briefing_block(mid_setup, "Good morning", s2, now=_local(8, 44, 30)) == (None, None)
    finally:
        s2.close()


def test_finish_runs_the_send_time_check_and_folds_the_first_water_slot(db, briefing_on, caplog):
    """The model answered 'morning. lab 12-7' — no weather, no water line. The reply gets
    the weather clause + the water fallback, and the pending water-GIF slot moves on."""
    import agent_loop
    from models import Reminder
    user = make_user(db, wake_time="07:00", sleep_time="00:00")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = Reminder(user_id=user.id, text="drink water", local_time="07:00", every_hours=2,
                   window_start="07:00", window_end="22:00", fire_at=now + timedelta(minutes=20),
                   active=True, source="offer")
    db.add(row); db.commit()
    before = row.fire_at
    parts = {"weather": "56° & overcast in Berkeley", "due_today": [], "due_today_times": {},
             "due_soon": [], "gym_window": None, "nutrition": None, "water": True}
    with caplog.at_level(logging.INFO):
        out = agent_loop._finish_reactive_briefing(user, "morning. lab 12-7", parts)
    assert out.startswith("56° & overcast in Berkeley"), out
    assert "big glass of water first" in out and "lab 12-7" in out
    assert "AGENT_LOOP_MORNING_BRIEFING_SENT" in caplog.text
    db.refresh(row)
    assert row.fire_at > before, (before, row.fire_at)


def test_finish_leaves_a_complete_briefing_alone_and_is_a_noop_without_parts(db, briefing_on):
    import agent_loop
    user = make_user(db)
    parts = {"weather": "56° & overcast in Berkeley", "due_today": [], "due_today_times": {},
             "due_soon": [], "gym_window": None, "nutrition": None, "water": True}
    text = "morning. 56° and overcast, big glass of water first. lab 12-7 today"
    assert agent_loop._finish_reactive_briefing(user, text, parts) == text
    assert agent_loop._finish_reactive_briefing(user, "morning", None) == "morning"


def test_due_today_the_model_dropped_comes_back_as_its_own_bubble(db, briefing_on):
    import agent_loop
    user = make_user(db)
    parts = {"weather": None, "due_today": ["CS 70 HW 9"], "due_today_times": {"CS 70 HW 9": "4:00pm"},
             "due_soon": [], "gym_window": None, "nutrition": None, "water": False}
    out = agent_loop._finish_reactive_briefing(user, "morning, long day ahead", parts)
    assert "cs 70 hw 9 due today by 4:00pm" in out, out


# ─── end to end through the loop ──────────────────────────────────────────────

def test_run_agent_loop_carries_the_briefing_and_finishes_the_reply(db, briefing_on, monkeypatch, anthropic_stub):
    """'Good morning' at 08:44 → the model sees the MORNING OPEN block with the reactive
    instruction, and its bare 'morning' comes back carrying the water line."""
    import agent_loop
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "BRIEFING_WATER_FOLDS_FIRST_PING_ENABLED", False)   # no reminder rows here
    real = agent_loop._morning_briefing_block
    monkeypatch.setattr(agent_loop, "_morning_briefing_block",
                        lambda u, b, s, **kw: real(u, b, s, now=_local(8, 44, 30)))
    seen: list = []

    def _reply(kw):
        seen.append(json.dumps(kw, default=str))
        return "morning"
    anthropic_stub.reply_with(_reply)
    user = make_user(db, wake_time="07:00", sleep_time="00:00")
    _msg(db, user.id, "in", _local(8, 44), "Good morning")

    reply = run_agent_loop(user, "Good morning", "freeform")
    assert seen and "THIS REPLY is the day's briefing" in seen[0]
    assert "their text just now is the first word" in seen[0]
    assert "big glass of water first" in reply, reply


def test_run_agent_loop_skips_the_block_on_a_continuation(db, briefing_on, monkeypatch, anthropic_stub):
    import agent_loop
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    calls: list = []
    monkeypatch.setattr(agent_loop, "_morning_briefing_block",
                        lambda u, b, s, **kw: (calls.append(1), (None, None))[1])
    anthropic_stub.reply_with(lambda kw: "👍")
    user = make_user(db)
    run_agent_loop(user, "ok", "freeform", continuation=True)
    assert calls == []
