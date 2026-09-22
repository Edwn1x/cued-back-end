"""
Daily rhythm (rewrite/daily-rhythm/CHANGESPEC.md) — tier-1, red-first.

User 32: "reminders and check-ups are way too far apart". Five pieces, each flag-gated:
  1. MEAL GAP standing condition (code-computed, local time, coexist wording)
  2. water reminders: interval recurrence on the reminders engine (+ ack path)
  3. quiet hours from the user's own wake/sleep (alt-wake day honoured)
  4. MORNING OPEN / EVENING CLOSE standing conditions
  5. per-user check-in level: tool, cap, rendering
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")

# Local wall-clock → aware UTC instant (the `now=` every signal accepts).
def _at(y, m, d, h, mi=0, tz=PT):
    return datetime(y, m, d, h, mi, tzinfo=tz).astimezone(timezone.utc)


def _naive(dt_aware):
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


# 2026-09-22 is a Tuesday; 2026-09-26 a Saturday.
TUE = (2026, 9, 22)
SAT = (2026, 9, 26)

AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", wake_time="06:50",
               sleep_time="00:00", meals_per_day="3", user_timezone="America/Los_Angeles",
               calorie_target=1400, protein_target=173, workout_days="mon,wed,fri")


@pytest.fixture
def rhythm_on(monkeypatch):
    import config
    for f in ("HEARTBEAT_MEAL_GAP_ENABLED", "HEARTBEAT_RHYTHM_ENABLED",
              "QUIET_HOURS_FROM_PROFILE_ENABLED", "WATER_REMINDERS_ENABLED",
              "SET_CHECKIN_LEVEL_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


def _ctx_user(db, **kw):
    from models import get_session, User
    u = make_user(db, **kw)
    s = get_session()
    return s, s.get(User, u.id)


def _meal(s, user_id, when_aware, desc="chicken bowl", cal=500, pro=40):
    from models import Meal
    s.add(Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro, carbs_g=30, fat_g=15,
               source="text", log_type="user_reported", eaten_at=_naive(when_aware), logged_at=_naive(when_aware)))
    s.commit()


def _msg(s, user_id, direction, when_aware, body="hey", message_type="freeform"):
    from models import Message
    s.add(Message(user_id=user_id, direction=direction, body=body, message_type=message_type,
                  created_at=_naive(when_aware)))
    s.commit()


def _tick(s, user_id, when_aware, spoke=True, message="what'd u eat"):
    from models import HeartbeatTick
    s.add(HeartbeatTick(user_id=user_id, spoke=spoke, reason="spoke" if spoke else "nothing new",
                        message=message if spoke else None, decided_at=_naive(when_aware)))
    s.commit()


# ─── flags default off ───────────────────────────────────────────────────────

def test_all_five_flags_default_off():
    import config
    for f in ("HEARTBEAT_MEAL_GAP_ENABLED", "WATER_REMINDERS_ENABLED", "QUIET_HOURS_FROM_PROFILE_ENABLED",
              "HEARTBEAT_RHYTHM_ENABLED", "SET_CHECKIN_LEVEL_TOOL_ENABLED"):
        assert getattr(config, f) is False, f


# ─── 1. MEAL GAP ──────────────────────────────────────────────────────────────

def test_meal_gap_first_meal_by_wake_plus_five_hours(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        # 9am: only 2h10 after a 6:50 wake — not yet
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 9, 0)) is None
        # 2pm: 7h after wake, nothing logged today
        sig = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 0))
    finally:
        s.close()
    assert sig and sig.startswith("## MEAL GAP (standing condition — code-computed)"), sig
    assert "breakfast" in sig.lower() and "lunch" in sig.lower(), sig
    assert "0 of" in sig and "3" in sig, sig          # today's count vs meals_per_day
    assert "7.2h" in sig or "7.1h" in sig, sig        # hours since wake, code-computed
    assert "2:00pm" in sig, sig                        # the local clock, precomputed
    assert "once per gap" in sig.lower(), sig
    assert "screenshot" not in sig.lower(), "coexist wording must not render for a native logger"


def test_meal_gap_six_hours_since_last_meal_during_waking_hours(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        _meal(s, user.id, _at(*TUE, 8, 0), "oatmeal")
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 11, 0)) is None   # 3h: fine
        sig = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 30))           # 6.5h
    finally:
        s.close()
    assert sig and "6.5h" in sig and "1 of" in sig, sig
    assert "oatmeal" in sig, "the last logged meal is named so the coach can be specific"


def test_meal_gap_evening_close_before_sleep(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)   # sleeps 00:00 → evening window opens 22:00
    try:
        _meal(s, user.id, _at(*TUE, 8, 0))
        _meal(s, user.id, _at(*TUE, 13, 0))
        # 5pm, 4h since lunch, not yet the evening window and < 6h → nothing
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 17, 0)) is None
        # 7:30pm: 6.5h since lunch → the long-gap branch
        mid = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 19, 30))
        assert mid and "evening" not in mid.lower(), mid
        _meal(s, user.id, _at(*TUE, 19, 45), "dinner")   # dinner logged → 3 of 3
        # 10:30pm: expected count reached → silent
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 22, 30)) is None
    finally:
        s.close()
    # a 2-of-3 day at 10:30pm, last meal 9.5h ago → evening close wording
    s, user2 = _ctx_user(db, **AISLINN)
    try:
        _meal(s, user2.id, _at(*TUE, 8, 0))
        _meal(s, user2.id, _at(*TUE, 13, 0))
        sig = heartbeat._meal_gap_signal(user2, s, now=_at(*TUE, 22, 30))
    finally:
        s.close()
    assert sig and "evening close" in sig.lower() and "didn't log" in sig.lower(), sig
    assert "2 of" in sig, sig


def test_meal_gap_silent_outside_waking_hours_and_with_flag_off(db, rhythm_on, monkeypatch):
    import config, heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 5, 30)) is None   # before wake
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 0, 30)) is None   # past bedtime (00:00)
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 0))           # sanity: renders
        monkeypatch.setattr(config, "HEARTBEAT_MEAL_GAP_ENABLED", False)
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 0)) is None
    finally:
        s.close()


def test_meal_gap_defaults_when_profile_times_are_free_phrases(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **dict(AISLINN, wake_time="whenever my alarm goes", sleep_time="late", meals_per_day=None))
    try:
        # default wake 08:00 + 5h = 13:00; 12:30 is not yet, 13:30 is
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 12, 30)) is None
        sig = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 13, 30))
    finally:
        s.close()
    assert sig and "0 of ~3" in sig, sig


def test_meal_gap_coexist_wording_asks_for_a_screenshot(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        # the sibling PR's column may not exist yet: set the attribute on the instance
        object.__setattr__(user, "food_logger_status", "coexist")
        sig = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 0))
    finally:
        s.close()
    assert sig and "screenshot" in sig.lower() and "re-type" in sig.lower(), sig
    assert "their app" in sig.lower() or "own app" in sig.lower(), sig


def test_meal_gap_marks_a_text_already_sent_during_this_gap(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        _meal(s, user.id, _at(*TUE, 8, 0))
        fresh = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 30))
        _tick(s, user.id, _at(*TUE, 14, 35), spoke=True)   # the heartbeat spoke during the gap
        again = heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 15, 30))
    finally:
        s.close()
    assert fresh and "no proactive text yet" in fresh.lower(), fresh
    assert again and "already" in again.lower() and "do not ask again" in again.lower(), again


def test_meal_gap_reaches_proactive_context_and_prompt_names_it(db, rhythm_on, monkeypatch):
    import heartbeat
    from datetime import datetime as _dt
    s, user = _ctx_user(db, **AISLINN)
    try:
        # pin the heartbeat's clock at 2pm local so the real _proactive_context renders it
        monkeypatch.setattr(heartbeat, "_now_aware", lambda: _at(*TUE, 14, 0))
        ctx = heartbeat._proactive_context(user, s)
    finally:
        s.close()
    assert "## MEAL GAP" in ctx
    assert "MEAL GAP" in heartbeat.HEARTBEAT_PROMPT and "once per gap" in heartbeat.HEARTBEAT_PROMPT


# ─── 2. water reminders (interval recurrence) ─────────────────────────────────

def test_interval_next_fire_inside_window_and_across_the_edge():
    from reminders import next_interval_fire_at
    # slots anchor at the window start: 07:00, 09:00, ... 23:00
    assert next_interval_fire_at(PT, 2, "07:00", "23:00", after=_naive(_at(*TUE, 13, 30))) == _naive(_at(*TUE, 15, 0))
    assert next_interval_fire_at(PT, 2, "07:00", "23:00", after=_naive(_at(*TUE, 15, 0))) == _naive(_at(*TUE, 17, 0))  # exact slot is not ahead
    # last slot 23:00 then skip past the window to tomorrow's start
    assert next_interval_fire_at(PT, 2, "07:00", "23:00", after=_naive(_at(*TUE, 22, 30))) == _naive(_at(*TUE, 23, 0))
    assert next_interval_fire_at(PT, 2, "07:00", "23:00", after=_naive(_at(*TUE, 23, 30))) == _naive(_at(2026, 9, 23, 7, 0))
    # before the window opens → today's start
    assert next_interval_fire_at(PT, 2, "07:00", "23:00", after=_naive(_at(*TUE, 3, 0))) == _naive(_at(*TUE, 7, 0))
    # a 3h step whose last slot doesn't land on the end: 07,10,13,16,19,22 → after 22:30 is tomorrow 07:00
    assert next_interval_fire_at(PT, 3, "07:00", "23:00", after=_naive(_at(*TUE, 22, 30))) == _naive(_at(2026, 9, 23, 7, 0))


def test_interval_next_fire_window_crossing_midnight():
    from reminders import next_interval_fire_at
    # wake 10:00, sleep 01:00 → window 10:00–01:00 next day; slots 10,12,...,22,00
    assert next_interval_fire_at(PT, 2, "10:00", "01:00", after=_naive(_at(*TUE, 23, 30))) == _naive(_at(2026, 9, 23, 0, 0))
    assert next_interval_fire_at(PT, 2, "10:00", "01:00", after=_naive(_at(2026, 9, 23, 0, 30))) == _naive(_at(2026, 9, 23, 10, 0))
    assert next_interval_fire_at(PT, 2, "10:00", "01:00", after=_naive(_at(2026, 9, 23, 2, 0))) == _naive(_at(2026, 9, 23, 10, 0))
    assert next_interval_fire_at(PT, 2, "10:00", "01:00", after=_naive(_at(2026, 9, 23, 22, 30))) == _naive(_at(2026, 9, 24, 0, 0))
    # bad inputs never raise
    assert next_interval_fire_at(PT, 0, "10:00", "01:00") is None
    assert next_interval_fire_at(PT, 2, "soon", "01:00") is None


def test_create_interval_reminder_defaults_window_to_wake_and_sleep(db):
    from models import Reminder
    from reminders import create_reminder, describe, _tz, active_reminders, context_block
    u = make_user(db, wake_time="06:50", sleep_time="23:00")
    r = create_reminder(u.id, "drink water", None, every_hours=2)
    assert "error" not in r, r
    db.expire_all()
    row = db.get(Reminder, r["id"])
    assert row.every_hours == 2 and row.window_start is None and row.window_end is None  # null = follow the profile
    assert row.local_time == "06:50" and row.recur_days is None and row.active is True
    d = describe(row, _tz(u.user_timezone))
    assert "every 2h" in d and "6:50am" in d and "11:00pm" in d, d
    assert active_reminders(u.id)[0].id == row.id
    ctx = context_block(u, db)
    assert ctx and "every 2h" in ctx
    # explicit window + validation
    r2 = create_reminder(u.id, "water", None, every_hours=3, window_start="09:00", window_end="21:00")
    db.expire_all()
    row2 = db.get(Reminder, r2["id"])
    assert (row2.window_start, row2.window_end, row2.local_time) == ("09:00", "21:00", "09:00")
    assert "error" in create_reminder(u.id, "water", None, every_hours=0)
    assert "error" in create_reminder(u.id, "water", None, every_hours=13)
    assert "error" in create_reminder(u.id, "water", None)  # neither time nor interval


def test_interval_window_defaults_when_profile_times_are_free_phrases(db):
    from models import Reminder
    from reminders import create_reminder
    u = make_user(db, wake_time="early ish", sleep_time="around midnight i guess")
    r = create_reminder(u.id, "drink water", None, every_hours=2)
    db.expire_all()
    row = db.get(Reminder, r["id"])
    assert row.local_time == "08:00"   # fallback window 08:00–22:00


def test_fire_due_rearms_an_interval_row_and_composes_one_short_line(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import create_reminder, fire_due
    seen = {}

    def handler(kw):
        seen["instruction"] = kw["messages"][0]["content"]
        return "water check 💧"
    anthropic_stub.reply_with(handler)

    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    r = create_reminder(u.id, "drink water", None, every_hours=2)
    db.expire_all()
    row = db.get(Reminder, r["id"])
    first = row.fire_at
    assert fire_due(now=first - timedelta(minutes=1)) == 0
    assert fire_due(now=first + timedelta(seconds=30)) == 1
    assert sms_capture[-1][1] == "water check 💧"
    assert "every 2 hours" in seen["instruction"] and "few words" in seen["instruction"], seen["instruction"]
    db.expire_all()
    row = db.get(Reminder, r["id"])
    assert row.active is True and row.sent_count == 1
    assert row.fire_at - first == timedelta(hours=2)
    # stale (container down for a day): re-armed, not sent
    row.fire_at = first - timedelta(days=2)
    db.commit()
    assert fire_due(now=first + timedelta(seconds=30)) == 0
    db.expire_all()
    row = db.get(Reminder, r["id"])
    assert row.active is True and row.fire_at > first


def test_set_reminder_tool_affordance_is_flag_gated(monkeypatch):
    import config
    from agent_tools import set_reminder_tool, SET_REMINDER_TOOL
    monkeypatch.setattr(config, "WATER_REMINDERS_ENABLED", False)
    off = set_reminder_tool()
    assert "every_hours" not in off["input_schema"]["properties"]
    assert "water" not in off["description"].lower()
    assert off["input_schema"]["required"] == SET_REMINDER_TOOL["input_schema"]["required"]
    monkeypatch.setattr(config, "WATER_REMINDERS_ENABLED", True)
    on = set_reminder_tool()
    assert "every_hours" in on["input_schema"]["properties"]
    assert "water" in on["description"].lower() and "hydration" in on["description"].lower()
    assert "time" not in on["input_schema"]["required"], "time is optional for an interval reminder"
    assert "every_hours" not in SET_REMINDER_TOOL["input_schema"]["properties"], "the constant stays the flag-off shape"


def test_handle_set_reminder_with_every_hours(db):
    from agent_tools import handle_set_reminder
    from reminders import active_reminders
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    out = handle_set_reminder(u.id, {"text": "drink water", "every_hours": 2})
    assert out.startswith("ok: reminder set") and "every 2h" in out, out
    rows = active_reminders(u.id)
    assert len(rows) == 1 and rows[0].every_hours == 2
    assert handle_set_reminder(u.id, {"text": "drink water", "every_hours": "lots"}).startswith("error")


def test_loop_persists_a_water_reminder_via_the_tool(db, driver, monkeypatch, anthropic_stub, rhythm_on):
    import config
    from tests._fake_anthropic import ToolUse
    from reminders import active_reminders
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        tool = next(t for t in kw["tools"] if t["name"] == "set_reminder")
        assert "every_hours" in tool["input_schema"]["properties"]
        calls.append(1)
        if len(calls) == 1:
            return ToolUse("set_reminder", {"text": "drink water", "every_hours": 2})
        return "bet, water every 2h while ur up"
    anthropic_stub.reply_with(handler)
    u = make_user(db)
    replies = driver.send(u, "can u remind me to drink water")
    rows = active_reminders(u.id)
    assert len(rows) == 1 and rows[0].every_hours == 2 and rows[0].source == "model"
    assert any("water" in r for r in replies)


def test_water_ack_rides_the_closing_ack_branch(db, driver, anthropic_stub, sms_capture):
    """'drank' / 'done' right after a water ping: no model call, nothing sent (SMS user)."""
    from reminders import create_reminder, is_water_ack
    from models import Message
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    create_reminder(u.id, "drink water", None, every_hours=2)
    now = datetime.now(timezone.utc)
    _msg(db, u.id, "out", now - timedelta(minutes=3), "water check 💧", message_type="reminder")
    assert is_water_ack(u.id, "drank") and is_water_ack(u.id, "Done!") and is_water_ack(u.id, "👍")
    assert not is_water_ack(u.id, "drank but also what should i eat for dinner")
    replies = driver.send(u, "drank")
    assert replies == [] and anthropic_stub.calls == [], "a water ack must not reach the model"
    # not a water ack when the last outbound wasn't a reminder
    _msg(db, u.id, "out", now - timedelta(minutes=1), "how was practice?", message_type="freeform")
    assert not is_water_ack(u.id, "done")
    # nor when the user has no interval reminder at all
    v = make_user(db)
    _msg(db, v.id, "out", now - timedelta(minutes=1), "go run", message_type="reminder")
    assert not is_water_ack(v.id, "done")


def test_capabilities_claim_water_and_checkin_level(monkeypatch):
    import config
    from capabilities import CAPABILITIES
    ids = {c.id: c for c in CAPABILITIES}
    assert "water_reminders" in ids and "checkin_level" in ids
    assert "set_reminder" in ids["water_reminders"].tools
    assert "set_checkin_level" in ids["checkin_level"].tools
    monkeypatch.setattr(config, "WATER_REMINDERS_ENABLED", False)
    monkeypatch.setattr(config, "SET_CHECKIN_LEVEL_TOOL_ENABLED", False)
    assert ids["water_reminders"].enabled(None) is False and ids["checkin_level"].enabled(None) is False


# ─── 3. quiet hours from the profile ─────────────────────────────────────────

@pytest.fixture
def quiet_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", True)


def test_quiet_hours_follow_wake_and_sleep(quiet_on):
    from heartbeat import _in_standing_quiet_hours

    class U:  # user 32: up 6:50, bed 00:00
        user_timezone = "America/Los_Angeles"
        wake_time, sleep_time, wake_time_alt, wake_days_alt = "06:50", "00:00", None, None
    q = lambda h, m=0: _in_standing_quiet_hours(U(), now=_at(*TUE, h, m))
    assert q(7, 0) is True       # wake+15 = 7:05
    assert q(7, 10) is False     # her morning is open (global window would mute until 8)
    assert q(22, 0) is False     # still up (global would mute from 21)
    assert q(23, 20) is False
    assert q(23, 35) is True     # sleep−30 = 23:30
    assert q(2, 0) is True


def test_quiet_hours_honour_the_alt_wake_day(quiet_on):
    from heartbeat import _in_standing_quiet_hours

    class U:
        user_timezone = "America/Los_Angeles"
        wake_time, sleep_time = "07:00", "23:00"
        wake_time_alt, wake_days_alt = "10:00", "sat,sun"
    assert _in_standing_quiet_hours(U(), now=_at(*TUE, 9, 0)) is False   # weekday: up at 7
    assert _in_standing_quiet_hours(U(), now=_at(*SAT, 9, 0)) is True    # saturday: sleeps till 10
    assert _in_standing_quiet_hours(U(), now=_at(*SAT, 10, 20)) is False


def test_quiet_hours_fall_back_to_global_when_unparseable_or_flag_off(quiet_on, monkeypatch):
    import config
    from heartbeat import _in_standing_quiet_hours

    class Phrase:
        user_timezone = "America/Los_Angeles"
        wake_time, sleep_time, wake_time_alt, wake_days_alt = "around 7", "late", None, None
    assert _in_standing_quiet_hours(Phrase(), now=_at(*TUE, 7, 30)) is True    # global: until 8
    assert _in_standing_quiet_hours(Phrase(), now=_at(*TUE, 21, 30)) is True   # global: from 21

    class Half:  # only one side parses → global window (both are required)
        user_timezone = "America/Los_Angeles"
        wake_time, sleep_time, wake_time_alt, wake_days_alt = "06:50", "late", None, None
    assert _in_standing_quiet_hours(Half(), now=_at(*TUE, 7, 30)) is True

    class U:
        user_timezone = "America/Los_Angeles"
        wake_time, sleep_time, wake_time_alt, wake_days_alt = "06:50", "00:00", None, None
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", False)
    assert _in_standing_quiet_hours(U(), now=_at(*TUE, 7, 30)) is True   # back to the floor


def test_quiet_hours_profile_gates_the_tick(db, quiet_on, monkeypatch, anthropic_stub):
    from models import get_session, User
    from heartbeat import guardrail_reason
    import config
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, **AISLINN)
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert guardrail_reason(u, s, now=_at(*TUE, 23, 45)) == "quiet_hours_standing"
        assert guardrail_reason(u, s, now=_at(*TUE, 7, 30)) is None
    finally:
        s.close()


# ─── 4. MORNING OPEN / EVENING CLOSE ─────────────────────────────────────────

def test_morning_open_renders_only_in_the_window_with_no_conversation(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)   # Tue is not a workout day (mon,wed,fri)
    try:
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 6, 30)) is None    # before wake
        sig = heartbeat._morning_open_signal(user, s, now=_at(*TUE, 7, 20))             # wake+30
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 8, 30)) is None    # wake+100 > 90
        _msg(s, user.id, "in", _at(*TUE, 7, 0), "morning")                              # they texted since wake
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 7, 20)) is None
    finally:
        s.close()
    assert sig and sig.startswith("## MORNING OPEN"), sig
    assert "rest day" in sig.lower() and "tuesday" in sig.lower(), sig
    assert "one short line" in sig.lower() and "not a briefing" in sig.lower(), sig
    assert sig.count("\n") <= 3, "one line of material, not a briefing: " + sig


def test_morning_open_names_a_workout_day_and_todays_events(db, rhythm_on):
    import heartbeat
    from models import Event
    WED = (2026, 9, 23)
    s, user = _ctx_user(db, **AISLINN)
    try:
        s.add(Event(user_id=user.id, event_type="class", occurred_at=_naive(_at(*WED, 10, 0)),
                    ends_at=_naive(_at(*WED, 11, 0)), source="model", raw_text="chem lab"))
        s.commit()
        _msg(s, user.id, "out", _at(*TUE, 20, 0), "night", message_type="freeform")      # yesterday: irrelevant
        _msg(s, user.id, "out", _at(*WED, 7, 5), ".", message_type="reaction")            # a tapback doesn't count
        sig = heartbeat._morning_open_signal(user, s, now=_at(*WED, 7, 30))
    finally:
        s.close()
    assert sig and "workout day" in sig.lower() and "chem lab" in sig, sig


def test_evening_close_renders_before_sleep_with_no_outbound_since_five(db, rhythm_on):
    import heartbeat
    from models import Workout
    s, user = _ctx_user(db, **AISLINN)   # sleeps 00:00 → window 22:00–00:00
    try:
        _meal(s, user.id, _at(*TUE, 13, 0), cal=600, pro=45)
        s.add(Workout(user_id=user.id, workout_type="full_body", completed=True, date=_naive(_at(*TUE, 18, 0))))
        s.commit()
        assert heartbeat._evening_close_signal(user, s, now=_at(*TUE, 21, 0)) is None    # too early
        sig = heartbeat._evening_close_signal(user, s, now=_at(*TUE, 22, 30))
        _msg(s, user.id, "out", _at(*TUE, 18, 30), "nice session")                        # an outbound after 5pm
        assert heartbeat._evening_close_signal(user, s, now=_at(*TUE, 22, 30)) is None
    finally:
        s.close()
    assert sig and sig.startswith("## EVENING CLOSE"), sig
    assert "600" in sig and "1400" in sig and "45" in sig, sig          # totals vs target, code-computed
    assert "1 workout" in sig.lower(), sig
    assert "evening check" in sig.lower() and "not a briefing" in sig.lower(), sig


def test_evening_close_five_pm_reference_for_an_after_midnight_sleeper(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **dict(AISLINN, sleep_time="01:00"))   # window 23:00–01:00
    try:
        WED = (2026, 9, 23)
        _msg(s, user.id, "out", _at(*TUE, 16, 30), "afternoon")            # before 5pm Tue: doesn't count
        assert heartbeat._evening_close_signal(user, s, now=_at(*WED, 0, 15))   # 12:15am Wed = Tue night
        _msg(s, user.id, "out", _at(*TUE, 19, 0), "evening text")
        assert heartbeat._evening_close_signal(user, s, now=_at(*WED, 0, 15)) is None
    finally:
        s.close()


def test_rhythm_flag_off_and_unparseable_times_render_nothing(db, rhythm_on, monkeypatch):
    import config, heartbeat
    s, user = _ctx_user(db, **dict(AISLINN, wake_time="early", sleep_time="late"))
    try:
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 8, 30)) is None
        assert heartbeat._evening_close_signal(user, s, now=_at(*TUE, 22, 30)) is None
    finally:
        s.close()
    s, user = _ctx_user(db, **AISLINN)
    try:
        monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", False)
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 7, 20)) is None
        assert heartbeat._evening_close_signal(user, s, now=_at(*TUE, 22, 30)) is None
    finally:
        s.close()
    assert "MORNING OPEN" in heartbeat.HEARTBEAT_PROMPT and "EVENING CLOSE" in heartbeat.HEARTBEAT_PROMPT


def test_rhythm_blocks_reach_proactive_context(db, rhythm_on, monkeypatch):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        monkeypatch.setattr(heartbeat, "_now_aware", lambda: _at(*TUE, 7, 20))
        morning = heartbeat._proactive_context(user, s)
        monkeypatch.setattr(heartbeat, "_now_aware", lambda: _at(*TUE, 22, 30))
        evening = heartbeat._proactive_context(user, s)
    finally:
        s.close()
    assert "## MORNING OPEN" in morning and "## EVENING CLOSE" not in morning
    assert "## EVENING CLOSE" in evening and "## MORNING OPEN" not in evening


# ─── 5. check-in level ───────────────────────────────────────────────────────

def test_set_checkin_level_tool_and_handler(db, rhythm_on):
    from agent_tools import handle_set_checkin_level, dispatch_tool, SET_CHECKIN_LEVEL_TOOL
    from models import User
    assert SET_CHECKIN_LEVEL_TOOL["name"] == "set_checkin_level"
    assert SET_CHECKIN_LEVEL_TOOL["input_schema"]["properties"]["level"]["enum"] == ["more", "normal", "less"]
    u = make_user(db)
    out = handle_set_checkin_level(u.id, {"level": "less"})
    assert out.startswith("ok:") and "less" in out and "2" in out, out
    db.expire_all()
    assert db.get(User, u.id).checkin_level == "less"
    out = dispatch_tool("set_checkin_level", {"level": "more"}, u.id)
    assert out.startswith("ok:") and "8" in out, out
    db.expire_all()
    assert db.get(User, u.id).checkin_level == "more"
    assert handle_set_checkin_level(u.id, {"level": "whatever"}).startswith("error")
    assert handle_set_checkin_level(u.id, {}).startswith("error")


def test_loop_offers_set_checkin_level_only_with_the_flag(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from models import User
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "SET_CHECKIN_LEVEL_TOOL_ENABLED", False)
    seen = []

    def handler(kw):
        if kw.get("tools"):
            seen.append([t["name"] for t in kw["tools"]])
            if "set_checkin_level" in seen[-1] and len(seen) == 2:
                return ToolUse("set_checkin_level", {"level": "less"})
        return "ok"
    anthropic_stub.reply_with(handler)
    u = make_user(db)
    driver.send(u, "chill with the texts")
    assert seen and "set_checkin_level" not in seen[-1]
    monkeypatch.setattr(config, "SET_CHECKIN_LEVEL_TOOL_ENABLED", True)
    driver.send(u, "chill with the texts")
    assert "set_checkin_level" in seen[-1]
    db.expire_all()
    assert db.get(User, u.id).checkin_level == "less"


def test_checkin_level_sets_the_daily_cap(db, rhythm_on, monkeypatch):
    import config, heartbeat
    from models import get_session, User
    monkeypatch.setattr(config, "HEARTBEAT_MAX_PER_DAY", 5)
    assert heartbeat._max_per_day(make_user(db)) == 5
    assert heartbeat._max_per_day(make_user(db, checkin_level="normal")) == 5
    assert heartbeat._max_per_day(make_user(db, checkin_level="more")) == 8
    assert heartbeat._max_per_day(make_user(db, checkin_level="less")) == 2
    assert heartbeat._max_per_day(make_user(db, checkin_level="bogus")) == 5
    # guardrail: 'less' user hits daily_budget at 2 spoken ticks; 'more' user does not at 5
    less = make_user(db, checkin_level="less")
    more = make_user(db, checkin_level="more")
    s = get_session()
    try:
        for uid, n in ((less.id, 2), (more.id, 5)):
            for _ in range(n):
                _tick(s, uid, datetime.now(timezone.utc) - timedelta(minutes=5), spoke=True)
        assert heartbeat.guardrail_reason(s.get(User, less.id), s) == "daily_budget"
        assert heartbeat.guardrail_reason(s.get(User, more.id), s) != "daily_budget"
    finally:
        s.close()


def test_checkin_level_less_drops_the_rhythm_conditions(db, rhythm_on):
    import heartbeat
    s, user = _ctx_user(db, **dict(AISLINN, checkin_level="less"))
    try:
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 0)) is None
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 7, 20)) is None
        assert heartbeat._evening_close_signal(user, s, now=_at(*TUE, 22, 30)) is None
    finally:
        s.close()
    s, user = _ctx_user(db, **dict(AISLINN, checkin_level="more"))
    try:
        assert heartbeat._meal_gap_signal(user, s, now=_at(*TUE, 14, 0))
        assert heartbeat._morning_open_signal(user, s, now=_at(*TUE, 7, 20))
    finally:
        s.close()


def test_context_names_the_checkin_level(db, rhythm_on):
    import heartbeat
    for level, cap in ((None, 5), ("more", 8), ("less", 2)):
        s, user = _ctx_user(db, **dict(AISLINN, checkin_level=level))
        try:
            ctx = heartbeat._proactive_context(user, s)
        finally:
            s.close()
        assert "## CHECK-IN LEVEL" in ctx, level
        assert f"{level or 'normal'}" in ctx.split("## CHECK-IN LEVEL", 1)[1][:120], (level, ctx)
        assert f"{cap}/day" in ctx, (level, ctx)


def test_voice_routes_checkin_level_and_water_asks():
    from agent_loop import _voice_prompt
    v = _voice_prompt()
    assert "set_checkin_level" in v and "chill with the texts" in v
    assert "every_hours" in v and "drink water" in v


# ─── migrations ──────────────────────────────────────────────────────────────

def test_daily_rhythm_columns_are_migrated_idempotently(db):
    from sqlalchemy import inspect
    import models
    from migrate import run_migrations
    run_migrations()
    run_migrations()
    insp = inspect(models.engine)
    assert "checkin_level" in {c["name"] for c in insp.get_columns("users")}
    rem = {c["name"] for c in insp.get_columns("reminders")}
    assert {"every_hours", "window_start", "window_end"} <= rem
    src = open(__import__("migrate").__file__).read()
    for col in ("checkin_level", "every_hours", "window_start", "window_end"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in src, col
