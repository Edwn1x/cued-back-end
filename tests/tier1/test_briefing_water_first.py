"""
The morning briefing tells them to drink a big glass of water first — and that line IS the
day's first water ping: the first interval (water GIF) slot after it is folded into the
next one, so a briefing at 7:29 and a GIF at 7:15/9:00 don't nag twice in one morning.

Founder 2026-10-29: "recommend drinking a big glass of water right after the user wakes up
as part of their morning briefing … make the briefing count as the day's first water ping
and push the first GIF to the next slot."

Same shape as the weather guarantee: the line is briefing MATERIAL (the model writes it in
its own words, varied day to day, right after the weather), the send-time check logs a drop,
and code appends a short fallback when the model left it out.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
DAY = (2026, 10, 7)   # a Wednesday


def _local(h, mi=0):
    return datetime(*DAY, h, mi, tzinfo=PT)


def _naive(aware):
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _parts(**over):
    base = {"weather": "51° and clear", "due_today": [], "due_today_times": {}, "due_soon": [],
            "gym_window": None, "nutrition": None, "water": True}
    base.update(over)
    return base


def _water_row(db, user, fire_local, *, every=2, text="drink water", source="offer"):
    from models import Reminder
    row = Reminder(user_id=user.id, text=text, local_time="07:00", every_hours=every,
                   window_start="07:00", window_end="22:00", fire_at=_naive(fire_local),
                   active=True, source=source)
    db.add(row)
    db.commit()
    return row


# ─── knobs ────────────────────────────────────────────────────────────────────

def test_knob_defaults():
    assert config.BRIEFING_WATER_ENABLED is True
    assert config.BRIEFING_WATER_FOLDS_FIRST_PING_ENABLED is True


# ─── the material + the instruction ───────────────────────────────────────────

def test_parts_carry_water_and_the_render_names_it(db, monkeypatch):
    import heartbeat
    monkeypatch.setattr(config, "WEATHER_ENABLED", False)
    user = make_user(db)
    from models import get_session, User
    s = get_session()
    try:
        parts = heartbeat._daily_briefing_parts(s.get(User, user.id), s, now=_local(7, 29))
    finally:
        s.close()
    assert parts["water"] is True
    rendered = heartbeat._render_briefing_extras(parts)
    assert "Water:" in rendered and "big glass" in rendered and "right after waking" in rendered, rendered


def test_flag_off_leaves_the_material_and_render_alone(db, monkeypatch):
    import heartbeat
    monkeypatch.setattr(config, "BRIEFING_WATER_ENABLED", False)
    monkeypatch.setattr(config, "WEATHER_ENABLED", False)
    user = make_user(db)
    from models import get_session, User
    s = get_session()
    try:
        parts = heartbeat._daily_briefing_parts(s.get(User, user.id), s, now=_local(7, 29))
    finally:
        s.close()
    assert not parts.get("water")
    assert "Water:" not in heartbeat._render_briefing_extras(parts)


def test_the_instruction_puts_water_right_after_the_weather_and_varies_it():
    import heartbeat
    ins = heartbeat._BRIEFING_INSTRUCTION
    assert "big glass of water first" in ins
    assert ins.index("weather") < ins.index("big glass of water first") < ins.index("due TODAY")
    assert "varied day to day" in ins


# ─── the send-time check + fallback ───────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "51° and clear. big glass of water first. lab 12–7",
    "51° and clear. chug a glass before anything else",
    "51° and clear. water before coffee today",
    "51° and clear. hydrate first, then lab",
])
def test_a_water_line_in_the_text_is_not_a_drop(text):
    import heartbeat
    assert not [d for d in heartbeat._briefing_drops(text, _parts()) if d[0] == "water"], text


def test_no_water_line_is_a_drop_and_gets_the_fallback_on_the_first_bubble(caplog):
    import heartbeat
    msg = "51° and clear. legs at 8, then lab 12–7 straight with no real break\n---\nget the burrito in before noon"
    assert ("water", "big glass right after waking") in heartbeat._briefing_drops(msg, _parts())
    with caplog.at_level(logging.INFO):
        out = heartbeat._enforce_briefing(msg, _parts(), 77)
    assert out == ("51° and clear. legs at 8, then lab 12–7 straight with no real break. big glass of water first"
                   "\n---\nget the burrito in before noon"), out
    assert "BRIEFING_WATER_APPENDED" in caplog.text and "BRIEFING_DROPPED_ITEM" in caplog.text


def test_a_present_line_is_left_alone():
    import heartbeat
    msg = "51° and clear. big glass of water first. lab 12–7"
    assert heartbeat._enforce_briefing(msg, _parts(), 77) == msg


def test_fallback_composes_with_the_due_today_append():
    import heartbeat
    parts = _parts(due_today=["CS 70 HW Due"], due_today_times={"CS 70 HW Due": "4:00pm"})
    out = heartbeat._enforce_briefing("51° and clear out", parts, 77)
    assert out == "51° and clear out. big glass of water first\n---\nalso: cs 70 hw due today by 4:00pm", out


def test_no_water_material_means_no_fallback():
    import heartbeat
    msg = "51° and clear out"
    assert heartbeat._enforce_briefing(msg, _parts(water=False), 77) == msg
    assert not [d for d in heartbeat._briefing_drops(msg, _parts(water=False)) if d[0] == "water"]


# ─── the fold: the briefing is the first water ping ───────────────────────────

def test_fold_moves_the_pending_first_slot_to_the_next_grid_slot(db, caplog):
    """GIF pending at 07:15 (wake + 15), briefing sent 07:29 → the 07:15 slot is spent
    (it was gated/caught up or is simply next); the row now fires at 09:00."""
    from reminders import fold_first_water_slot
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(7, 15))
    with caplog.at_level(logging.INFO):
        assert fold_first_water_slot(user.id, now=_naive(_local(7, 29))) == 1
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 0)) and row.sent_count == 0
    assert "WATER_FIRST_SLOT_FOLDED" in caplog.text


def test_fold_moves_a_slot_still_ahead_within_one_interval(db):
    """Briefing at 08:40 with the GIF due at 09:00 (within the 2h interval) → 11:00."""
    from reminders import fold_first_water_slot
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(9, 0))
    assert fold_first_water_slot(user.id, now=_naive(_local(8, 40))) == 1
    db.refresh(row)
    assert row.fire_at == _naive(_local(11, 0))


def test_fold_leaves_a_slot_further_out_alone(db):
    """Briefing at 07:29, next GIF 11:00 (a catch-up already past the next slot): untouched."""
    from reminders import fold_first_water_slot
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(11, 0))
    assert fold_first_water_slot(user.id, now=_naive(_local(7, 29))) == 0
    db.refresh(row)
    assert row.fire_at == _naive(_local(11, 0))


def test_fold_ignores_non_water_rows_and_no_rows(db):
    from reminders import fold_first_water_slot
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    assert fold_first_water_slot(user.id, now=_naive(_local(7, 29))) == 0
    row = _water_row(db, user, _local(7, 15), text="stretch", source="model")   # an interval row, not water
    assert fold_first_water_slot(user.id, now=_naive(_local(7, 29))) == 0
    db.refresh(row)
    assert row.fire_at == _naive(_local(7, 15))


def _tick(monkeypatch, user, text, search):
    import heartbeat
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s, now=None: None)
    monkeypatch.setattr(heartbeat, "decide", lambda uid, allow_skip=True: (True, text, dict(search)))
    sent = []
    monkeypatch.setattr(heartbeat, "send_sms", lambda phone, body, **kw: sent.append(body) or True)
    monkeypatch.setattr(heartbeat, "_naive_utcnow", lambda: _naive(_local(7, 29)))
    heartbeat.heartbeat_tick(user.id)
    return sent


def test_a_sent_briefing_with_the_water_line_folds_the_first_gif(db, monkeypatch):
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(7, 15))
    sent = _tick(monkeypatch, user, "51° and clear. big glass of water first", {"briefing_water": True})
    assert sent
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 0))


def test_an_ordinary_heartbeat_does_not_touch_the_gif(db, monkeypatch):
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(7, 15))
    _tick(monkeypatch, user, "how'd the lab go", {})
    db.refresh(row)
    assert row.fire_at == _naive(_local(7, 15))


def test_fold_flag_off_sends_the_briefing_and_keeps_the_gif(db, monkeypatch):
    monkeypatch.setattr(config, "BRIEFING_WATER_FOLDS_FIRST_PING_ENABLED", False)
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(7, 15))
    sent = _tick(monkeypatch, user, "51° and clear. big glass of water first", {"briefing_water": True})
    assert sent
    db.refresh(row)
    assert row.fire_at == _naive(_local(7, 15))


def test_decide_marks_a_briefing_tick_that_carried_water(db, monkeypatch, anthropic_stub):
    """End to end through decide(): a morning-open briefing tick sets search['briefing_water']
    so heartbeat_tick knows to fold; an off-flag briefing doesn't."""
    import heartbeat
    for flag in ("HEARTBEAT_ALLOWLIST",):
        monkeypatch.setattr(config, flag, [])
    for flag in ("HEARTBEAT_RHYTHM_ENABLED", "HEARTBEAT_STANDING_QUIET_ENABLED", "QUIET_HOURS_FROM_PROFILE_ENABLED",
                 "CALENDAR_ASSISTANT_ENABLED", "CALENDAR_DAILY_BRIEFING_ENABLED"):
        monkeypatch.setattr(config, flag, True)
    for flag in ("WEATHER_ENABLED", "HEARTBEAT_WEB_SEARCH", "HEARTBEAT_STALE_SKIP_ENABLED", "HEARTBEAT_BACKOFF_ENABLED"):
        monkeypatch.setattr(config, flag, False, raising=False)
    monkeypatch.setattr(heartbeat, "_now_aware", lambda: _local(7, 29).astimezone(timezone.utc))
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    anthropic_stub.push("51° and clear. big glass of water first. lab 12–7 today")
    spoke, payload, search = heartbeat.decide(user.id, allow_skip=False)
    assert spoke and "water" in payload
    assert search.get("briefing_water") is True
    monkeypatch.setattr(config, "BRIEFING_WATER_ENABLED", False)
    anthropic_stub.push("51° and clear. lab 12–7 today")
    spoke, payload, search = heartbeat.decide(user.id, allow_skip=False)
    assert spoke and not search.get("briefing_water")


# ─── the reverse order: the GIF is due before the briefing tick ───────────────

@pytest.fixture
def morning_on(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    for flag in ("HEARTBEAT_RHYTHM_ENABLED", "HEARTBEAT_STANDING_QUIET_ENABLED", "QUIET_HOURS_FROM_PROFILE_ENABLED",
                 "CALENDAR_ASSISTANT_ENABLED", "CALENDAR_DAILY_BRIEFING_ENABLED", "WATER_REMINDER_GATES_ENABLED"):
        monkeypatch.setattr(config, flag, True, raising=False)
    for flag in ("WEATHER_ENABLED", "WATER_GIF_ENABLED"):
        monkeypatch.setattr(config, flag, False, raising=False)
    monkeypatch.setattr("events.current_blocks", lambda uid, now=None: [], raising=False)
    yield


def _fire(monkeypatch, row, when_local):
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    ok = reminders._fire_one(row.id, _naive(when_local))
    return ok, sent


def test_the_first_gif_of_the_day_waits_for_a_briefing_still_owed(db, monkeypatch, morning_on, caplog):
    """Wake 07:00, GIF due 07:15, nobody has talked since they woke → the briefing is still
    owed and will carry the water line: the slot re-arms to 09:00, nothing is sent."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(7, 15))
    with caplog.at_level(logging.INFO):
        ok, sent = _fire(monkeypatch, row, _local(7, 15))
    assert ok is False and not sent
    assert "REMINDER_GATED" in caplog.text and "reason=briefing_owed" in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 0)) and row.sent_count == 0


def test_once_they_have_talked_the_gif_goes_out(db, monkeypatch, morning_on):
    """A text at 07:05 means no briefing is owed (they're in a reactive thread) → the GIF
    fires at 07:15 as before."""
    from models import Message
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    db.add(Message(user_id=user.id, direction="in", body="morning", created_at=_naive(_local(7, 5))))
    db.commit()
    row = _water_row(db, user, _local(7, 15))
    ok, sent = _fire(monkeypatch, row, _local(7, 15))
    assert ok is True and sent
    db.refresh(row)
    assert row.sent_count == 1


def test_only_the_first_slot_of_the_day_waits(db, monkeypatch, morning_on):
    """A row that already sent today (09:00 slot, last_sent_at 07:15) never waits."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(9, 0))
    row.last_sent_at = _naive(_local(7, 15))
    row.sent_count = 1
    db.commit()
    monkeypatch.setattr("heartbeat._morning_open_signal", lambda u, s, now=None, state=None: "owed")
    ok, sent = _fire(monkeypatch, row, _local(9, 0))
    assert ok is True and sent


def test_outside_the_morning_window_the_gif_goes_out(db, monkeypatch, morning_on):
    """11:00 slot, nobody talked all morning, but the 90-min morning window closed at 08:30:
    no briefing is coming → fire."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _water_row(db, user, _local(11, 0))
    ok, sent = _fire(monkeypatch, row, _local(11, 0))
    assert ok is True and sent


def test_wait_is_off_with_either_flag_or_without_the_daily_briefing(db, monkeypatch, morning_on):
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    for flag in ("BRIEFING_WATER_ENABLED", "BRIEFING_WATER_FOLDS_FIRST_PING_ENABLED", "CALENDAR_DAILY_BRIEFING_ENABLED"):
        monkeypatch.setattr(config, flag, False)
        row = _water_row(db, user, _local(7, 15))
        ok, sent = _fire(monkeypatch, row, _local(7, 15))
        assert ok is True and sent, flag
        monkeypatch.setattr(config, flag, True)
