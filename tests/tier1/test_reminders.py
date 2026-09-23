"""
Reminders (PR B) — an explicit "remind me" is a promise code keeps.

Live 2026-09-22 (user 42): "Wanna remind me to after class to run?" → "yeah i can do
that, i'll ping u after" → nothing existed to do it (onboarding has no tools; the regex
Event floor logged EVENT_NEAR_MISS; memory carried no time). Also: the heartbeat had no
onboarding gate, so it could speak mid-intake and its reply would land in the intake.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")


def _utc(local: datetime) -> datetime:
    """naive UTC for a PT-local wall time"""
    return local.replace(tzinfo=PT).astimezone(timezone.utc).replace(tzinfo=None)


TUE_3AM = datetime(2026, 9, 22, 3, 0)     # a Tuesday
TUE_8PM = datetime(2026, 9, 22, 20, 0)


# ─── time math ────────────────────────────────────────────────────────────────

def test_parse_days_and_times():
    from reminders import parse_days, parse_local_time
    assert parse_days(["tue", "thu"]) == "tue,thu"
    assert parse_days("Tuesdays and Thursdays") == "tue,thu"
    assert parse_days("thu, tue") == "tue,thu"
    assert parse_days("weekdays") == "mon,tue,wed,thu,fri"
    assert parse_days("every day") == "mon,tue,wed,thu,fri,sat,sun"
    assert parse_days("whenever") is None and parse_days(None) is None
    assert parse_local_time("19:30") == (19, 30)
    assert parse_local_time("7:30pm") == (19, 30) and parse_local_time("7pm") == (19, 0)
    assert parse_local_time("12am") == (0, 0) and parse_local_time("12:15pm") == (12, 15)
    assert parse_local_time("after class") is None and parse_local_time("25:00") is None


def test_next_fire_recurring_today_if_ahead_else_next_listed_day():
    from reminders import next_fire_at
    # Tuesday 3am, ask for tue/thu 7:30pm → TODAY 7:30pm (the live case: class ends 7:30 tonight)
    assert next_fire_at(PT, "19:30", "tue,thu", after=_utc(TUE_3AM)) == _utc(datetime(2026, 9, 22, 19, 30))
    # Tuesday 8pm → Thursday
    assert next_fire_at(PT, "19:30", "tue,thu", after=_utc(TUE_8PM)) == _utc(datetime(2026, 9, 24, 19, 30))
    # Thursday 8pm → next Tuesday
    assert next_fire_at(PT, "19:30", "tue,thu", after=_utc(datetime(2026, 9, 24, 20, 0))) == _utc(datetime(2026, 9, 29, 19, 30))
    # exact same minute is NOT ahead
    assert next_fire_at(PT, "19:30", "tue", after=_utc(datetime(2026, 9, 22, 19, 30))) == _utc(datetime(2026, 9, 29, 19, 30))


def test_next_fire_one_off_today_tomorrow_and_date():
    from reminders import next_fire_at
    assert next_fire_at(PT, "07:00", None, after=_utc(TUE_3AM)) == _utc(datetime(2026, 9, 22, 7, 0))
    assert next_fire_at(PT, "07:00", None, after=_utc(TUE_8PM)) == _utc(datetime(2026, 9, 23, 7, 0))
    assert next_fire_at(PT, "07:00", None, after=_utc(TUE_3AM), date_str="tomorrow") == _utc(datetime(2026, 9, 23, 7, 0))
    assert next_fire_at(PT, "09:00", None, after=_utc(TUE_3AM), date_str="2026-10-01") == _utc(datetime(2026, 10, 1, 9, 0))
    assert next_fire_at(PT, "bogus", None, after=_utc(TUE_3AM)) is None


# ─── create / cancel ──────────────────────────────────────────────────────────

def test_create_cancel_and_context_block(db):
    from reminders import create_reminder, cancel_reminder, active_reminders, context_block
    u = make_user(db)
    r = create_reminder(u.id, "go run", "7:30pm", days=["tue", "thu"], source="model")
    assert "error" not in r and r["local_time"] == "19:30" and r["recur_days"] == "tue,thu"
    assert create_reminder(u.id, "", "19:30") == {"error": "text required"}
    assert "not understood" in create_reminder(u.id, "x", "after class")["error"]
    rows = active_reminders(u.id)
    assert [x.text for x in rows] == ["go run"]
    block = context_block(db.get(type(u), u.id), db)
    assert "## REMINDERS" in block and "'go run' — tue/thu 7:30pm" in block and f"id={r['id']}" in block
    assert cancel_reminder(u.id, r["id"]) is True and cancel_reminder(u.id, r["id"]) is False
    assert active_reminders(u.id) == []
    other = make_user(db)
    assert cancel_reminder(other.id, r["id"]) is False  # not theirs


def test_create_with_replace_rewrites_the_same_row(db):
    from reminders import create_reminder, active_reminders
    u = make_user(db)
    a = create_reminder(u.id, "go run", "18:00", days="tue,thu", source="onboarding")
    b = create_reminder(u.id, "go run", "19:30", days="tue,thu", source="onboarding", replace_id=a["id"])
    assert a["id"] == b["id"]
    rows = active_reminders(u.id)
    assert len(rows) == 1 and rows[0].local_time == "19:30"


# ─── firing ───────────────────────────────────────────────────────────────────

def _messages(db, user_id):
    from models import Message
    db.expire_all()
    return [(m.message_type, m.body) for m in db.query(Message).filter_by(user_id=user_id, direction="out").order_by(Message.id)]


def test_fire_due_sends_in_voice_rearms_recurring_and_closes_one_offs(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import create_reminder, fire_due
    seen = {}

    def handler(kw):
        seen["instruction"] = kw["messages"][0]["content"]
        seen["system"] = kw["system"]
        return "yo class is out, go get that run in"
    anthropic_stub.reply_with(handler)

    u = make_user(db, name="Alex")
    rec = create_reminder(u.id, "go run", "19:30", days="tue,thu")
    db.expire_all()
    fire_before = db.get(Reminder, rec["id"]).fire_at

    assert fire_due(now=fire_before - timedelta(minutes=1)) == 0          # not yet
    n = fire_due(now=fire_before + timedelta(seconds=30))
    assert n == 1 and sms_capture[-1][1] == "yo class is out, go get that run in"
    assert _messages(db, u.id)[-1] == ("reminder", "yo class is out, go get that run in")
    assert 'remind them: "go run"' in seen["instruction"] and "Under 20 words" in seen["instruction"]
    assert seen["system"][0].get("cache_control") and "Alex" in seen["system"][1]["text"]

    row = db.get(Reminder, rec["id"])
    assert row.active is True and row.sent_count == 1 and row.last_sent_at is not None
    # tue → thu is 2 days, thu → tue is 5: depends on which listed day the first fire hit
    from datetime import timezone as _tzu
    from reminders import _tz
    first_local = fire_before.replace(tzinfo=_tzu.utc).astimezone(_tz(u.user_timezone))
    expected = timedelta(days=2 if first_local.weekday() == 1 else 5)
    assert row.fire_at > fire_before and row.fire_at - fire_before == expected

    # one-off created AFTER the recurring fire: its next 7am can fall before the
    # recurring's first fire (thu this week), where the sweeps above would consume it
    one = create_reminder(u.id, "take creatine", "07:00")
    db.expire_all()
    one_row = db.get(Reminder, one["id"])
    assert fire_due(now=one_row.fire_at + timedelta(seconds=1)) == 1
    db.expire_all()
    assert db.get(Reminder, one["id"]).active is False


def test_fire_due_keeps_the_promise_when_the_model_fails(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import create_reminder, fire_due
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(RuntimeError("api down")))
    u = make_user(db)
    r = create_reminder(u.id, "go run", "19:30")
    db.expire_all()
    assert fire_due(now=db.get(Reminder, r["id"]).fire_at) == 1
    assert sms_capture[-1][1] == "reminder: go run"


def test_fire_due_skips_opted_out_and_stale_rows(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import create_reminder, fire_due
    anthropic_stub.reply_with(lambda kw: "go")
    out = make_user(db, opted_out=True)
    r1 = create_reminder(out.id, "go run", "19:30")
    db.expire_all()
    assert fire_due(now=db.get(Reminder, r1["id"]).fire_at) == 0 and not sms_capture
    db.expire_all()
    assert db.get(Reminder, r1["id"]).active is False

    u = make_user(db)
    r2 = create_reminder(u.id, "go run", "19:30", days="tue,thu")
    db.expire_all()
    old_fire = db.get(Reminder, r2["id"]).fire_at
    assert fire_due(now=old_fire + timedelta(days=3)) == 0 and not sms_capture   # >24h late → re-armed, not sent
    db.expire_all()
    assert db.get(Reminder, r2["id"]).fire_at > old_fire + timedelta(days=3)


def test_flag_off_disables_everything(db, monkeypatch, sms_capture):
    import config
    from models import Reminder
    from reminders import create_reminder, fire_due, context_block
    u = make_user(db)
    r = create_reminder(u.id, "go run", "19:30")
    monkeypatch.setattr(config, "REMINDERS_ENABLED", False)
    db.expire_all()
    assert fire_due(now=db.get(Reminder, r["id"]).fire_at) == 0 and not sms_capture
    assert context_block(db.get(type(u), u.id), db) is None


# ─── tools ────────────────────────────────────────────────────────────────────

def test_set_and_cancel_reminder_tools(db):
    from agent_tools import handle_set_reminder, handle_cancel_reminder, dispatch_tool
    from reminders import active_reminders
    u = make_user(db)
    out = handle_set_reminder(u.id, {"text": "go run", "time": "19:30", "days": ["tue", "thu"]})
    assert out.startswith("ok: reminder set") and "tue/thu 7:30pm" in out
    rid = active_reminders(u.id)[0].id
    assert handle_set_reminder(u.id, {"text": "x", "time": "soon"}).startswith("error")
    assert dispatch_tool("cancel_reminder", {"reminder_id": rid}, u.id) == "ok: cancelled"
    assert dispatch_tool("cancel_reminder", {"reminder_id": rid}, u.id).startswith("error")
    assert handle_cancel_reminder(u.id, {}).startswith("error")


def test_loop_offers_the_tools_and_persists_a_set_reminder(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from reminders import active_reminders
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        names = [t["name"] for t in kw["tools"]]
        assert "set_reminder" in names and "cancel_reminder" in names
        calls.append(1)
        if len(calls) == 1:
            return ToolUse("set_reminder", {"text": "go run", "time": "19:30", "days": ["tue", "thu"]})
        return "bet, i'll ping u at 7:30 tue and thu"
    anthropic_stub.reply_with(handler)
    u = make_user(db)
    replies = driver.send(u, "remind me to run after class on tuesdays and thursdays, class ends 7:30")
    rows = active_reminders(u.id)
    assert len(rows) == 1 and rows[0].recur_days == "tue,thu" and rows[0].local_time == "19:30" and rows[0].source == "model"
    assert any("7:30" in r for r in replies)


def test_voice_routes_reminder_requests_to_the_tool():
    from agent_loop import _voice_prompt
    v = _voice_prompt()
    assert "set_reminder" in v and "Never say \"i'll ping u\"" in v


# ─── onboarding capture ───────────────────────────────────────────────────────

def _is_field_extract(kw):
    return "Extract any fitness coaching profile data" in str(kw["messages"][0]["content"])


def _is_reminder_extract(kw):
    return "asked the coach to REMIND" in str(kw["messages"][0]["content"])


def test_onboarding_captures_the_ask_and_a_later_time_correction(db, anthropic_stub, sms_capture):
    """The live sequence: 'class till 6' → 'remind me after class to run' → 'nah class
    ends 7:30'. One reminder, corrected in place, never re-asked."""
    import onboarding_agent
    from models import Message
    from reminders import active_reminders
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    s = db
    s.add(Message(user_id=user.id, direction="out", body="hey how's your day going?", message_type="onboarding"))
    s.add(Message(user_id=user.id, direction="in", body="In school my Tuesday's and Thursday's I have class till 6 pm", message_type="freeform"))
    s.add(Message(user_id=user.id, direction="out", body="long days those two.", message_type="onboarding"))
    s.commit()
    seen = {"reminder_prompts": []}

    def handler(kw):
        if _is_field_extract(kw):
            return "{}"
        if _is_reminder_extract(kw):
            p = kw["messages"][0]["content"]
            seen["reminder_prompts"].append(p)
            if "ends at 7:30" in p.split("Their latest message")[-1]:
                return json.dumps({"text": "go run", "time": "19:30", "days": ["tue", "thu"], "date": None})
            return json.dumps({"text": "go run", "time": "18:00", "days": ["tue", "thu"], "date": None})
        seen["system"] = kw["system"]
        return "yeah i can do that, i'll ping u after"
    anthropic_stub.reply_with(handler)

    onboarding_agent.handle_onboarding_reply(user, "Wanna remind me to after class to run ?")
    rows = active_reminders(user.id)
    assert len(rows) == 1 and rows[0].local_time == "18:00" and rows[0].source == "onboarding"
    rid = rows[0].id
    assert "class till 6 pm" in seen["reminder_prompts"][0]  # history reaches the extractor
    sys_text = "".join(b["text"] for b in seen["system"]) if isinstance(seen["system"], list) else seen["system"]
    assert "Reminders you've set" in sys_text and "'go run' — tue/thu 6:00pm" in sys_text

    onboarding_agent.handle_onboarding_reply(user, "Nah class starts at 6 pm ends at 7:30 pm")
    rows = active_reminders(user.id)
    assert len(rows) == 1 and rows[0].id == rid and rows[0].local_time == "19:30"

    # a plain message with no number and no ask → no extraction call at all
    before = len(seen["reminder_prompts"])
    onboarding_agent.handle_onboarding_reply(user, "lol yeah pretty tired")
    assert len(seen["reminder_prompts"]) == before


def test_onboarding_ignores_schedule_facts_without_an_ask(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from reminders import active_reminders
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    calls = []
    anthropic_stub.reply_with(lambda kw: (calls.append(1) or "null") if _is_reminder_extract(kw) else ("{}" if _is_field_extract(kw) else "ok"))
    onboarding_agent.handle_onboarding_reply(user, "class till 6 on tuesdays")
    assert calls == [] and active_reminders(user.id) == []


# ─── heartbeat ────────────────────────────────────────────────────────────────

def test_heartbeat_guardrail_and_sweep_skip_onboarding_users(db, monkeypatch):
    import config
    from heartbeat import guardrail_reason, heartbeat_all
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    mid = make_user(db, onboarding_step=2)
    done = make_user(db, onboarding_step=3)
    assert guardrail_reason(mid, db) == "onboarding"
    assert guardrail_reason(done, db) != "onboarding"
    ticked = []
    monkeypatch.setattr("heartbeat.heartbeat_tick", lambda uid: ticked.append(uid))
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    heartbeat_all()
    assert ticked == [done.id]


def test_heartbeat_context_shows_pending_and_sent_reminders(db):
    from heartbeat import _proactive_context
    from models import Reminder, User
    from reminders import create_reminder
    u = make_user(db)
    r = create_reminder(u.id, "go run", "19:30", days="tue,thu")
    db.expire_all()
    row = db.get(Reminder, r["id"]); row.last_sent_at = datetime.now(timezone.utc).replace(tzinfo=None); db.commit()
    ctx = _proactive_context(db.get(User, u.id), db)
    assert "## REMINDERS" in ctx and "pending: 'go run' — tue/thu 7:30pm" in ctx and "sent " in ctx


def test_reminder_counts_as_proactive_for_anti_stack():
    from engagement_tracker import PROACTIVE_MESSAGE_TYPES
    assert "reminder" in PROACTIVE_MESSAGE_TYPES


def test_reminders_table_is_migrated(db):
    from sqlalchemy import inspect
    from models import engine
    cols = {c["name"] for c in inspect(engine).get_columns("reminders")}
    assert {"user_id", "text", "local_time", "recur_days", "fire_at", "source", "active", "last_sent_at", "sent_count"} <= cols


def test_agent_loop_context_carries_the_reminders_block(db):
    """Live 2026-09-23: only the heartbeat saw REMINDERS; the reactive loop didn't know
    Alex's tue/thu run ping existed and planned to set it again."""
    from agent_loop import build_loop_context
    from reminders import create_reminder
    u = make_user(db, onboarding_step=3)
    r = create_reminder(u.id, "go run", "19:30", days=["tue", "thu"], source="onboarding")
    db.expire_all()
    ctx = build_loop_context(db.get(type(u), u.id), db)
    assert "## REMINDERS" in ctx and f"id={r['id']}" in ctx and "'go run' — tue/thu 7:30pm" in ctx
