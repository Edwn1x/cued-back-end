"""
TICK HISTORY carries each decision's LOCAL day, and tells the model only [today] counts.

Live 2026-10-24 (Saturday), user 51: Friday 17:37 PT the coach asked "how'd the quiz go".
Saturday's ticks at 16:52, 17:37, 18:22 PT reasoned "already asked about today's quiz …
nothing new to add" and stayed silent through a second zero-food day — the undated history
listed Friday's SPOKE line and Friday's silent reasons as if they were today, and each new
stale reason re-entered the history as fact for the next tick.
"""
from datetime import datetime, timedelta, timezone

import pytest


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pt_instant(days_ago: int, hour: int, minute: int = 0):
    """Naive-UTC instant for `hour:minute` PT, `days_ago` local days back."""
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    local = datetime.now(pt).replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _ctx_with_ticks(db, monkeypatch, ticks):
    import heartbeat
    from models import get_session, User, HeartbeatTick
    from tests.factories import make_user
    monkeypatch.setattr(heartbeat.config, "HEARTBEAT_TICK_HISTORY_DATED_ENABLED", True, raising=False)
    user = make_user(db, user_timezone="America/Los_Angeles")
    s = get_session()
    try:
        for spoke, text, when in ticks:
            s.add(HeartbeatTick(user_id=user.id, spoke=spoke, reason="spoke" if spoke else text,
                                message=text if spoke else None, decided_at=when))
        s.commit()
        return heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()


def test_live_friday_question_is_labelled_yesterday_and_todays_nudge_today(db, monkeypatch):
    # guard against a run that straddles local midnight: "today" instants sit at the hour already passed
    ctx = _ctx_with_ticks(db, monkeypatch, [
        (True, "how'd the quiz go", _pt_instant(1, 17, 37)),
        (False, "open quiz question + meal nudge already sent today — don't double-text", _pt_instant(1, 21, 21)),
        (True, "67° and clear out, good day to get outside", _pt_instant(0, 0, 1)),
    ])
    assert "## TICK HISTORY" in ctx
    assert "[yesterday 5:37pm] SPOKE: how'd the quiz go" in ctx
    assert "[yesterday 9:21pm] silent: open quiz question" in ctx
    assert "[today 12:01am] SPOKE: 67° and clear out" in ctx
    assert "Only entries marked [today] count toward today's budget" in ctx
    assert "An earlier day's question they never answered is CLOSED" in ctx
    assert "you don't re-ask it either" in ctx


def test_older_days_carry_the_weekday():
    import heartbeat
    from types import SimpleNamespace
    u = SimpleNamespace(user_timezone="America/Los_Angeles")
    now = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)            # Sat 1pm PT
    assert heartbeat._tick_day_label(datetime(2026, 10, 24, 0, 37), u, now=now) == "yesterday 5:37pm"   # Fri 17:37 PT
    assert heartbeat._tick_day_label(datetime(2026, 10, 24, 19, 0), u, now=now) == "today 12:00pm"
    assert heartbeat._tick_day_label(datetime(2026, 10, 22, 22, 0), u, now=now) == "Thu 3:00pm"
    assert heartbeat._tick_day_label(datetime(2026, 10, 24, 6, 59), u, now=now) == "yesterday 11:59pm"  # 23:59 PT Fri


def test_flag_off_restores_the_undated_render(db, monkeypatch):
    import heartbeat
    from models import get_session, User, HeartbeatTick
    from tests.factories import make_user
    monkeypatch.setattr(heartbeat.config, "HEARTBEAT_TICK_HISTORY_DATED_ENABLED", False, raising=False)
    user = make_user(db)
    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=user.id, spoke=True, reason="spoke", message="skipped legs twice",
                            decided_at=_utcnow_naive() - timedelta(days=1)))
        s.commit()
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "- SPOKE: skipped legs twice" in ctx and "[yesterday" not in ctx and "Only entries marked" not in ctx


def test_flag_defaults_on():
    import config
    assert config.HEARTBEAT_TICK_HISTORY_DATED_ENABLED is True
