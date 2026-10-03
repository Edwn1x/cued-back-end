"""
Briefing completeness — weather + due-today never drop — tier-1.

Two verified live bugs (founder, 2026-10-02 12:38 PT):

B1  DUE-TODAY DEADLINES VANISH. bcourses/canvas "due:" assignments are all-day rows
    stored at 00:00 LOCAL of the due day. deadline_items filtered occurred_at >= now and
    set when = occurred_at, so every such deadline dropped out of the radar, the briefing,
    deadlines_within and high_load_soon the moment its due day STARTED. The material said
    "Due soon: CS 70 HW Due (5d)" while "due: Homework 4: RISC-V [CS61C Fa26]" was due that
    day. Fix: all-day rows are due at the END of their local day; a "Due TODAY:" line.

B2  WEATHER NEVER SHIPS. "Weather: 69° & overcast in Berkeley" was in the material every
    morning and the instruction never named it — 0 of 8 briefings carried it. Fix: the
    instruction makes weather + due-today REQUIRED, and the briefing tick verifies the
    SENT text: a missing weather clause is prepended (BRIEFING_WEATHER_GUARANTEE_ENABLED),
    every dropped material item is logged as BRIEFING_DROPPED_ITEM.

Clock-independent: every call pins `now=`; the model is stubbed; HTTP is mocked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import weather
from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
TOKYO = ZoneInfo("Asia/Tokyo")
DAY = (2026, 10, 2)   # the live Friday


def _naive(dt_aware) -> datetime:
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _local(h, mi=0, *, day=DAY, tz=PT):
    return datetime(*day, h, mi, tzinfo=tz)


def _utc(h, mi=0, *, day=DAY, tz=PT):
    return _local(h, mi, day=day, tz=tz).astimezone(timezone.utc)


def _mk_timed(user_id, title, start_aware, end_aware, *, source="gcal", ext=None):
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=ext or title, title=title,
                          occurred_at=_naive(start_aware), ends_at=_naive(end_aware), all_day=False)


def _mk_allday_due(user_id, title, local_midnight_aware, *, source="bcourses", ext=None):
    """A bcourses/canvas-style all-day assignment: stored at 00:00 LOCAL of its due day."""
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=ext or title, title=title,
                          occurred_at=_naive(local_midnight_aware), all_day=True)


def _mk_timed_due(user_id, title, when_aware, *, source="gcal", ext=None):
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=ext or title, title=title,
                          occurred_at=_naive(when_aware), all_day=False)


def _fresh(db, user):
    from models import get_session, User
    s = get_session()
    return s, s.get(User, user.id)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _patch_weather(monkeypatch, *, temp=69, code=3):
    """open-meteo mocked: 69° & overcast (WMO code 3), no hint → '69° & overcast in Berkeley'."""
    payload = {"current": {"temperature_2m": temp, "weather_code": code, "precipitation": 0.0},
               "daily": {"temperature_2m_max": [74], "temperature_2m_min": [55]}}
    monkeypatch.setattr(weather.requests, "get", lambda url, params=None, timeout=None: _FakeResp(payload))


@pytest.fixture(autouse=True)
def _clear_weather_cache():
    weather._CACHE.clear()
    yield
    weather._CACHE.clear()


@pytest.fixture
def brief_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_DAILY_BRIEFING_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_BRIEFING_GUARANTEE_ENABLED", True)
    monkeypatch.setattr(config, "BRIEFING_WEATHER_GUARANTEE_ENABLED", True)
    monkeypatch.setattr(config, "WEATHER_ENABLED", True)
    yield


# ─── knob default ──────────────────────────────────────────────────────────────

def test_weather_guarantee_knob_default_on():
    import config
    assert config.BRIEFING_WEATHER_GUARANTEE_ENABLED is True


# ─── B1: deadline_items — all-day rows are due at the END of their local day ──────

def test_allday_due_today_is_included_until_end_of_local_day(db):
    from schedule import deadline_items
    user = make_user(db)
    _mk_allday_due(user.id, "due: Homework 4: RISC-V [CS61C Fa26]", _local(0), ext="hw4")
    now = _utc(12, 0)
    items = deadline_items(user.id, db, days=7, now=now)
    assert [d.title for d in items] == ["due: Homework 4: RISC-V [CS61C Fa26]"]
    d = items[0]
    assert d.all_day is True
    assert d.when == _naive(_local(23, 59).replace(second=59))
    assert 11.9 < d.hours_until(_naive(now)) < 12.1


def test_allday_due_yesterday_is_excluded(db):
    from schedule import deadline_items
    user = make_user(db)
    _mk_allday_due(user.id, "due: Lab 3 [CS61C]", _local(0, day=(2026, 10, 1)), ext="lab3")
    assert deadline_items(user.id, db, days=7, now=_utc(12, 0)) == []


def test_timed_deadline_later_today_unchanged(db):
    from schedule import deadline_items
    user = make_user(db)
    _mk_timed_due(user.id, "CS 70 HW Due", _local(23, 0), ext="cs70")
    items = deadline_items(user.id, db, days=7, now=_utc(12, 0))
    assert len(items) == 1 and items[0].all_day is False
    assert items[0].when == _naive(_local(23, 0))


def test_timed_deadline_earlier_today_is_dropped(db):
    """Widening the query to day-start must NOT resurrect a timed deadline that already passed."""
    from schedule import deadline_items
    user = make_user(db)
    _mk_timed_due(user.id, "Essay 2 Due", _local(9, 0), ext="essay")
    assert deadline_items(user.id, db, days=7, now=_utc(12, 0)) == []


def test_allday_sorts_by_effective_due_instant(db):
    """An all-day item due today (end of day) sorts BEFORE a timed item due tomorrow morning,
    and AFTER a timed one due this afternoon — ordering keys off `when`, consistently."""
    from schedule import deadline_items
    user = make_user(db)
    _mk_allday_due(user.id, "due: HW4", _local(0), ext="hw4")
    _mk_timed_due(user.id, "Pset Due", _local(15, 0), ext="pset")
    _mk_timed_due(user.id, "Essay Due", _local(9, 0, day=(2026, 10, 3)), ext="essay")
    titles = [d.title for d in deadline_items(user.id, db, days=7, now=_utc(12, 0))]
    assert titles == ["Pset Due", "due: HW4", "Essay Due"]


def test_deadlines_within_and_high_load_see_due_today_item(db, monkeypatch):
    import config
    from schedule import deadline_items, deadlines_within, high_load_soon
    monkeypatch.setattr(config, "CALENDAR_HIGH_LOAD_HOURS", 48)
    monkeypatch.setattr(config, "CALENDAR_HIGH_LOAD_CLUSTER", 3)
    user = make_user(db)
    _mk_allday_due(user.id, "due: Homework 4: RISC-V [CS61C Fa26]", _local(0), ext="hw4")
    _mk_allday_due(user.id, "due: Reading response [R1B]", _local(0), ext="rr")
    _mk_timed_due(user.id, "CS 70 HW Due", _local(23, 0, day=(2026, 10, 3)), ext="cs70")
    now = _utc(12, 0)
    items = deadline_items(user.id, db, days=7, now=now)
    within = deadlines_within(items, 24, now=now)
    assert {d.title for d in within} == {"due: Homework 4: RISC-V [CS61C Fa26]", "due: Reading response [R1B]"}
    assert high_load_soon(user.id, db, now=now, items=items) is True   # 3 within 48h


def test_allday_exam_today_is_high_load(db):
    from schedule import deadline_items, high_load_soon
    user = make_user(db)
    _mk_allday_due(user.id, "due: CS61C Midterm 1", _local(0), ext="mt")
    now = _utc(8, 0)
    items = deadline_items(user.id, db, days=7, now=now)
    assert items and items[0].is_exam
    assert high_load_soon(user.id, db, now=now, items=items) is True


def test_due_today_helper_splits_today_from_rest(db):
    from schedule import deadline_items, due_today
    user = make_user(db)
    _mk_allday_due(user.id, "due: HW4", _local(0), ext="hw4")
    _mk_timed_due(user.id, "CS 70 HW Due", _local(23, 0), ext="cs70")
    _mk_timed_due(user.id, "Essay Due", _local(9, 0, day=(2026, 10, 5)), ext="essay")
    now = _utc(12, 0)
    items = deadline_items(user.id, db, days=7, now=now)
    assert {d.title for d in due_today(items, user, now=now)} == {"due: HW4", "CS 70 HW Due"}


def test_display_title_strips_due_prefix():
    from schedule import display_title
    assert display_title("due: Homework 4: RISC-V [CS61C Fa26]") == "Homework 4: RISC-V [CS61C Fa26]"
    assert display_title("CS 70 HW Due") == "CS 70 HW Due"


def test_tokyo_user_windows_on_local_day(db):
    """Tokyo local day ≠ UTC day: an all-day item due Tokyo-Friday is stored at Thu 15:00 UTC.
    At Tokyo Friday noon (03:00 UTC Fri) it must be INCLUDED with ~12h left; at Tokyo
    Saturday 01:00 (16:00 UTC Fri — still Friday in UTC!) it must be GONE."""
    from schedule import deadline_items
    user = make_user(db, user_timezone="Asia/Tokyo")
    _mk_allday_due(user.id, "due: Kanji quiz prep", _local(0, tz=TOKYO), ext="kq")
    noon_tokyo = _utc(12, 0, tz=TOKYO)
    items = deadline_items(user.id, db, days=7, now=noon_tokyo)
    assert len(items) == 1
    assert items[0].when == _naive(_local(23, 59, tz=TOKYO).replace(second=59))
    assert 11.9 < items[0].hours_until(_naive(noon_tokyo)) < 12.1
    sat_1am_tokyo = _utc(1, 0, day=(2026, 10, 3), tz=TOKYO)
    assert sat_1am_tokyo.date() == datetime(2026, 10, 2).date()   # still Friday in UTC
    assert deadline_items(user.id, db, days=7, now=sat_1am_tokyo) == []


def test_tokyo_allday_tomorrow_not_counted_today(db):
    from schedule import deadline_items, due_today
    user = make_user(db, user_timezone="Asia/Tokyo")
    _mk_allday_due(user.id, "due: Essay", _local(0, day=(2026, 10, 3), tz=TOKYO), ext="e")
    now = _utc(12, 0, tz=TOKYO)
    items = deadline_items(user.id, db, days=7, now=now)
    assert len(items) == 1
    assert due_today(items, user, now=now) == []


# ─── B1: the briefing material renders a "Due TODAY:" line ────────────────────────

def test_briefing_material_has_due_today_line_before_due_soon(db, brief_on, monkeypatch):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _mk_allday_due(user.id, "due: Homework 4: RISC-V [CS61C Fa26]", _local(0), ext="hw4")
    _mk_timed_due(user.id, "CS 70 HW Due", _local(23, 0, day=(2026, 10, 7)), ext="cs70")
    material = heartbeat._daily_briefing_extras(user, db, now=_utc(12, 38))
    assert "Due TODAY: Homework 4: RISC-V [CS61C Fa26]." in material
    assert "Due soon: CS 70 HW Due (5d)." in material
    assert material.index("Due TODAY:") < material.index("Due soon:")
    assert "due: Homework" not in material            # routing prefix never shown


def test_briefing_material_timed_due_today_carries_its_time(db, brief_on, monkeypatch):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db)
    _mk_timed_due(user.id, "CS 70 HW Due", _local(23, 0), ext="cs70")
    material = heartbeat._daily_briefing_extras(user, db, now=_utc(12, 0))
    assert "Due TODAY: CS 70 HW Due (by 11:00pm)." in material
    assert "Due soon:" not in material


def test_briefing_instruction_requires_weather_and_due_today(db, brief_on, monkeypatch):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    s, u = _fresh(db, user)
    try:
        sig = heartbeat._morning_open_signal(u, s, now=_utc(12, 0))
    finally:
        s.close()
    assert sig and "MORNING OPEN" in sig
    assert "lead with the weather" in sig
    assert "ALWAYS name anything due TODAY" in sig
    assert "Once — if TICK HISTORY" in sig          # the once-per-day rule is intact


def test_morning_open_signal_stashes_briefing_state(db, brief_on, monkeypatch):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _mk_allday_due(user.id, "due: HW4 [CS61C]", _local(0), ext="hw4")
    s, u = _fresh(db, user)
    try:
        state: dict = {}
        assert heartbeat._morning_open_signal(u, s, now=_utc(12, 0), state=state)
        assert state["briefing"]["weather"] == "69° & overcast in Berkeley"
        assert state["briefing"]["due_today"] == ["HW4 [CS61C]"]
        # outside the morning window nothing is stashed
        state2: dict = {}
        assert heartbeat._morning_open_signal(u, s, now=_utc(15, 0), state=state2) is None
        assert "briefing" not in state2
    finally:
        s.close()


# ─── B2: the send-time guarantee (unit) ────────────────────────────────────────

WL = "69° & overcast in Berkeley"


def _parts(**kw):
    base = {"weather": WL, "due_today": [], "due_today_times": {}, "due_soon": [],
            "gym_window": None, "nutrition": None}
    base.update(kw)
    return base


def test_enforce_prepends_weather_when_neither_temp_nor_condition_present(brief_on):
    import heartbeat
    out = heartbeat._enforce_briefing("morning! discussion at 4, then free till dinner", _parts(), 1)
    assert out.startswith("69° & overcast in Berkeley — morning!")


def test_enforce_does_not_duplicate_when_temp_present(brief_on):
    import heartbeat
    msg = "morning — 69° out, discussion at 4"
    assert heartbeat._enforce_briefing(msg, _parts(), 1) == msg


def test_enforce_does_not_duplicate_when_condition_present(brief_on):
    import heartbeat
    msg = "Overcast one today. discussion at 4"
    assert heartbeat._enforce_briefing(msg, _parts(), 1) == msg


def test_enforce_two_word_condition_matches_either_word(brief_on):
    import heartbeat
    msg = "a bit cloudy out there, discussion at 4"
    assert heartbeat._enforce_briefing(msg, _parts(weather="62° & partly cloudy in Berkeley"), 1) == msg


def test_enforce_weather_line_with_hint_uses_period_separator(brief_on):
    import heartbeat
    wl = "46° & rain in Berkeley — grab a jacket, good indoor-cardio day"
    out = heartbeat._enforce_briefing("discussion at 4", _parts(weather=wl), 1)
    assert out == f"{wl}. discussion at 4"


# ─── B2: the degree-sign guarantee (live 2026-10-03 "88 and clear out" → "Wym 88") ───

WL88 = "88° & clear in Berkeley — hydrate & train early"


def test_degree_inserted_after_bare_temperature(brief_on, caplog):
    import heartbeat
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        out = heartbeat._enforce_briefing("morning. 88 and clear out, hydrate", _parts(weather=WL88), 3)
    assert out == "morning. 88° and clear out, hydrate"
    assert any("BRIEFING_DEGREE_INSERTED user=3 temp=88" in r.getMessage() for r in caplog.records)
    assert not any("BRIEFING_WEATHER_PREPENDED" in r.getMessage() for r in caplog.records)


def test_degree_already_present_unchanged(brief_on, caplog):
    import heartbeat
    msg = "morning. 88° and clear out"
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        assert heartbeat._enforce_briefing(msg, _parts(weather=WL88), 3) == msg
    assert not any("BRIEFING_DEGREE_INSERTED" in r.getMessage() for r in caplog.records)


def test_degree_unit_word_already_present_unchanged(brief_on):
    import heartbeat
    for msg in ("88 degrees and clear", "88 deg and clear", "88F and clear", "88 f and clear",
                "88º and clear"):
        assert heartbeat._enforce_briefing(msg, _parts(weather=WL88), 3) == msg, msg


def test_degree_not_inserted_inside_another_number(brief_on):
    """'1880 cal' contains '88' — the digit-boundary guard must leave it alone; with no bare
    temp AND the condition word present, the text is untouched (no prepend either)."""
    import heartbeat
    msg = "clear skies. you're at 1880 cal, 288 to go"
    assert heartbeat._enforce_briefing(msg, _parts(weather=WL88), 3) == msg


def test_degree_inserted_first_occurrence_only(brief_on):
    import heartbeat
    out = heartbeat._enforce_briefing("88 now, maybe 88 again at 3", _parts(weather=WL88), 3)
    assert out == "88° now, maybe 88 again at 3"


def test_degree_case_no_temp_at_all_falls_to_prepend(brief_on, caplog):
    import heartbeat
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        out = heartbeat._enforce_briefing("morning! discussion at 4", _parts(weather=WL88), 3)
    assert out == "88° & clear in Berkeley — hydrate & train early. morning! discussion at 4"
    assert any("BRIEFING_WEATHER_PREPENDED" in r.getMessage() for r in caplog.records)
    assert not any("BRIEFING_DEGREE_INSERTED" in r.getMessage() for r in caplog.records)


def test_degree_flag_off_bare_temp_unchanged(brief_on, monkeypatch):
    import config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_WEATHER_GUARANTEE_ENABLED", False)
    msg = "morning. 88 and clear out, hydrate"
    assert heartbeat._enforce_briefing(msg, _parts(weather=WL88), 3) == msg


def test_degree_sign_survives_to_the_sent_sms(db, brief_on, monkeypatch, sms_capture, anthropic_stub):
    """Through the real tick: the inserted ° is NOT stripped or converted downstream."""
    import heartbeat
    _patch_weather(monkeypatch, temp=88, code=0)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _pin_clock(monkeypatch, _utc(12, 38))
    _tick_speaking(monkeypatch, anthropic_stub, "morning. 88 and clear out, hydrate")
    heartbeat.heartbeat_tick(user.id)
    assert sms_capture and sms_capture[0][1] == "morning. 88° and clear out, hydrate"


def test_instruction_mandates_the_degree_sign(db, brief_on, monkeypatch):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    s, u = _fresh(db, user)
    try:
        sig = heartbeat._morning_open_signal(u, s, now=_utc(12, 0))
    finally:
        s.close()
    assert "WITH the degree sign (write 88°, never a bare 88" in sig


def test_enforce_flag_off_leaves_text_unchanged(brief_on, monkeypatch, caplog):
    import config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_WEATHER_GUARANTEE_ENABLED", False)
    msg = "discussion at 4"
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        assert heartbeat._enforce_briefing(msg, _parts(), 1) == msg
    # observability still fires with the flag off — the text just isn't touched
    assert any("BRIEFING_DROPPED_ITEM user=1 item=weather" in r.getMessage() for r in caplog.records)


def test_enforce_noop_without_briefing_parts(brief_on):
    import heartbeat
    msg = "how'd the midterm go?"
    assert heartbeat._enforce_briefing(msg, None, 1) == msg
    assert heartbeat._enforce_briefing(msg, {}, 1) == msg


def test_enforce_fails_open_on_error(brief_on, monkeypatch):
    import heartbeat
    monkeypatch.setattr(heartbeat, "_briefing_drops", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    msg = "discussion at 4"
    assert heartbeat._enforce_briefing(msg, _parts(), 1) == msg


def test_drop_log_due_today_absent_vs_present(brief_on, caplog):
    import heartbeat
    parts = _parts(due_today=["Homework 4: RISC-V [CS61C Fa26]"])
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat._enforce_briefing("69° and overcast. discussion at 4, then free", parts, 7)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("BRIEFING_DROPPED_ITEM user=7 item=due_today") for m in msgs)
    assert not any("item=weather" in m for m in msgs)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat._enforce_briefing("69° and overcast. HW4 (RISC-V) due tonight, discussion at 4", parts, 7)
    assert not any("BRIEFING_DROPPED_ITEM" in r.getMessage() for r in caplog.records)


def test_drop_log_covers_every_material_item(brief_on, caplog):
    import heartbeat
    parts = _parts(due_today=["Homework 4: RISC-V [CS61C Fa26]"],
                   due_soon=[("CS 70 HW Due", 5.4)],
                   gym_window=("Fri 1:00pm", "Fri 3:00pm"),
                   nutrition={"cal": 540, "pro": 32, "meals": 1, "tgt": ""})
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat._enforce_briefing("morning! hope you slept well", parts, 9)
    items = sorted(m.split("item=")[1].split()[0] for m in
                   (r.getMessage() for r in caplog.records) if "BRIEFING_DROPPED_ITEM" in m)
    assert items == ["due_soon", "due_today", "gym_window", "nutrition", "weather"]
    caplog.clear()
    full = ("69° and overcast. HW4 RISC-V is due TODAY, CS70 hw Tuesday. gym window 1pm–3pm, "
            "you're at 540 cal so far")
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat._enforce_briefing(full, parts, 9)
    assert not any("BRIEFING_DROPPED_ITEM" in r.getMessage() for r in caplog.records)


def test_drop_log_nutrition_zero_accepts_nothing_logged_phrasing(brief_on, caplog):
    import heartbeat
    parts = _parts(weather=None, nutrition={"cal": 0, "pro": 0, "meals": 0, "tgt": ""})
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat._enforce_briefing("nothing logged yet — grab breakfast before discussion", parts, 2)
    assert not any("item=nutrition" in r.getMessage() for r in caplog.records)


# ─── B2: through the real tick (stubbed model) ─────────────────────────────────

def _pin_clock(monkeypatch, now_aware):
    """The tick reads the wall clock through heartbeat._now_aware (signals) — pin it."""
    import heartbeat
    monkeypatch.setattr(heartbeat, "_now_aware", lambda: now_aware)


def _tick_speaking(monkeypatch, anthropic_stub, text):
    from tests._fake_anthropic import ToolUse
    anthropic_stub.reply_with(lambda kw: ToolUse("send_text", {"message": text}))


def _ticks(user_id):
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        return s.query(HeartbeatTick).filter(HeartbeatTick.user_id == user_id).order_by(HeartbeatTick.id).all()
    finally:
        s.close()


def test_tick_briefing_without_weather_gets_weather_prefixed(db, brief_on, monkeypatch, sms_capture,
                                                             anthropic_stub, caplog):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _pin_clock(monkeypatch, _utc(12, 0))
    _tick_speaking(monkeypatch, anthropic_stub, "morning! discussion at 4 then you're free")
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)
    assert sms_capture, "the briefing tick must speak"
    first = sms_capture[0][1]
    assert first.startswith("69") and "overcast in Berkeley" in first and "morning! discussion" in first
    tick = _ticks(user.id)[-1]
    assert tick.spoke and tick.message.startswith("69")       # the tick record holds what was sent
    assert any("BRIEFING_WEATHER_PREPENDED user=%d" % user.id in r.getMessage() for r in caplog.records)


def test_tick_briefing_with_weather_not_duplicated(db, brief_on, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _pin_clock(monkeypatch, _utc(12, 0))
    _tick_speaking(monkeypatch, anthropic_stub, "morning — 69° and grey out. discussion at 4")
    heartbeat.heartbeat_tick(user.id)
    assert sms_capture
    body = sms_capture[0][1]
    assert body.startswith("morning") and body.count("69") == 1


def test_tick_non_briefing_heartbeat_never_prefixed(db, brief_on, monkeypatch, sms_capture, anthropic_stub, caplog):
    """15:00 — the morning window (11:30–13:00) has closed, so there is no MORNING OPEN in
    context; the weather guarantee must not touch any other heartbeat."""
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _pin_clock(monkeypatch, _utc(15, 0))
    _tick_speaking(monkeypatch, anthropic_stub, "how'd discussion go?")
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)
    assert sms_capture and sms_capture[0][1] == "how'd discussion go?"
    assert not any("BRIEFING_" in r.getMessage() for r in caplog.records)


def test_tick_flag_off_sends_model_text_unchanged(db, brief_on, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_WEATHER_GUARANTEE_ENABLED", False)
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _pin_clock(monkeypatch, _utc(12, 0))
    _tick_speaking(monkeypatch, anthropic_stub, "morning! discussion at 4")
    heartbeat.heartbeat_tick(user.id)
    assert sms_capture and sms_capture[0][1] == "morning! discussion at 4"


def test_tick_bare_text_fallback_also_enforced(db, brief_on, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _pin_clock(monkeypatch, _utc(12, 0))
    anthropic_stub.reply_with(lambda kw: "morning! discussion at 4")
    heartbeat.heartbeat_tick(user.id)
    # send_sms normalises to GSM-7 (em dash → '-'); the prefix survives
    assert sms_capture and sms_capture[0][1].startswith("69° & overcast in Berkeley - morning!")


# ─── the live 2026-10-02 12:38 PT tick, reproduced ────────────────────────────

def _founder_1002(user_id):
    _mk_timed(user_id, "DATA C104", _local(15, 0), _local(16, 0), ext="c104")
    _mk_timed(user_id, "cs70 Discussion (friday = quiz)", _local(16, 0), _local(17, 0), ext="disc")
    _mk_allday_due(user_id, "due: Homework 4: RISC-V [CS61C Fa26]", _local(0), ext="hw4")
    _mk_timed_due(user_id, "CS 70 HW Due", _local(23, 0, day=(2026, 10, 7)), ext="cs70")


def test_live_1238_material_names_weather_and_hw4(db, brief_on, monkeypatch):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _founder_1002(user.id)
    s, u = _fresh(db, user)
    try:
        sig = heartbeat._morning_open_signal(u, s, now=_utc(12, 38))
    finally:
        s.close()
    assert sig and "MORNING OPEN" in sig
    assert "Weather: 69° & overcast in Berkeley." in sig
    assert "Due TODAY: Homework 4: RISC-V [CS61C Fa26]." in sig
    assert "Due soon: CS 70 HW Due (5d)." in sig
    assert "Nutrition so far: 0 cal" in sig
    assert "Discussion" not in sig.split("Briefing material:")[1].split("\n")[0]   # the quiz note is not a deadline


def test_live_1238_sent_text_carries_weather_and_logs_hw4_drop(db, brief_on, monkeypatch, sms_capture,
                                                               anthropic_stub, caplog):
    """The model text the founder actually got omitted both. Now: weather is prepended to the
    sent text and the HW4 drop is logged by name."""
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _founder_1002(user.id)
    _pin_clock(monkeypatch, _utc(12, 38))
    _tick_speaking(monkeypatch, anthropic_stub,
                   "morning! DATA C104 at 3 then discussion at 4. CS70 hw due in 5 days. "
                   "nothing logged yet — grab something before class")
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)
    assert sms_capture, "the briefing tick must speak"
    sent = sms_capture[0][1]
    assert sent.startswith("69") and "overcast in Berkeley" in sent
    assert "DATA C104 at 3" in sent
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"BRIEFING_DROPPED_ITEM user={user.id} item=due_today") and "Homework 4" in m
               for m in msgs)
    assert any(f"BRIEFING_DROPPED_ITEM user={user.id} item=weather" in m for m in msgs)
    assert not any("item=due_soon" in m for m in msgs)      # CS70 was named
    assert not any("item=nutrition" in m for m in msgs)     # "nothing logged yet" counts


def test_live_1238_complete_text_logs_no_drops(db, brief_on, monkeypatch, sms_capture, anthropic_stub, caplog):
    import heartbeat
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _founder_1002(user.id)
    _pin_clock(monkeypatch, _utc(12, 38))
    text = ("69° and overcast. HW4 RISC-V is due TODAY, CS70 hw Wednesday. DATA C104 at 3, "
            "discussion at 4. nothing logged yet - eat before class")
    _tick_speaking(monkeypatch, anthropic_stub, text)
    with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)
    assert sms_capture and sms_capture[0][1] == text
    assert not any("BRIEFING_DROPPED_ITEM" in r.getMessage() for r in caplog.records)
