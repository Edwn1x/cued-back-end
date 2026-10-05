"""
Deferred tasks — the coach can work BETWEEN messages (agent_tasks.py).

What's there now: every turn is one shot; reminders fire a model-composed line at a
named time; the heartbeat decides whether to speak. "I'll look into that and text you"
had no mechanism — the only path was hoping a heartbeat tick noticed a memory line.

What it becomes: a `schedule_task` tool creates an AgentTask row (goal, run_at computed
in CODE from a delay or a local time, kind lookup|watch). A 60s scheduler sweep runs due
tasks through a BOUNDED tool loop of READ-ONLY tools plus two terminal tools (report /
nothing_to_report). Code sends the report (message_type "task"), applies the same
opt-out / onboarding / standing-quiet-hours gates the heartbeat uses (defer, never
drop), re-arms watchers, retries once on a model failure and then keeps the promise
with a plain honest line. A TASKS block in context keeps the coach from re-promising.

Pinned here (tier 1, model mocked): creation math + validation + caps, the context
block, the sweep, every gate, the loop's tool allowlist (no writes), report / silent /
re-arm / retry / fallback outcomes, proactive-type registration, prompts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "TASKS_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


def _task(db, task_id):
    from models import AgentTask
    db.expire_all()
    return db.get(AgentTask, task_id)


def _report(msg, **extra):
    return ToolUse("report", {"message": msg, **extra}, id="toolu_report")


# ─── creation (code owns the clock) ─────────────────────────────────────────

def test_create_from_delay_computes_run_at_in_code(db):
    from agent_tasks import create_task
    user = make_user(db, name="Sam")
    before = _utcnow()
    r = create_task(user.id, "find the CS61C midterm room and text me", delay_minutes=45)
    assert "id" in r and r["kind"] == "lookup"
    row = _task(db, r["id"])
    assert row.status == "pending"
    assert timedelta(minutes=44) <= (row.run_at - before) <= timedelta(minutes=46)


def test_create_from_local_time_uses_the_users_timezone(db):
    from agent_tasks import create_task
    from zoneinfo import ZoneInfo
    user = make_user(db, name="Sam", user_timezone="America/Los_Angeles")
    local = (datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(hours=3)).replace(second=0, microsecond=0)
    r = create_task(user.id, "check if a seat opened in CS170 and text me", run_at_local=local.strftime("%Y-%m-%d %H:%M"))
    row = _task(db, r["id"])
    expected = local.astimezone(timezone.utc).replace(tzinfo=None)
    assert abs((row.run_at - expected).total_seconds()) < 60


@pytest.mark.parametrize("kwargs,needle", [
    (dict(goal="", delay_minutes=10), "goal"),
    (dict(goal="hi", delay_minutes=10), "goal"),
    (dict(goal="look up the rsf hours and text me"), "when"),
    (dict(goal="look up the rsf hours and text me", delay_minutes=0), "delay"),
    (dict(goal="look up the rsf hours and text me", delay_minutes=60 * 24 * 30), "delay"),
    (dict(goal="look up the rsf hours and text me", run_at_local="not a time"), "time"),
    (dict(goal="watch for a seat", kind="watch", every_minutes=1, for_hours=2), "every"),
    (dict(goal="watch for a seat", kind="watch", every_minutes=60), "for_hours"),
    (dict(goal="look up the rsf hours and text me", delay_minutes=10, kind="dance"), "kind"),
])
def test_create_validates(db, kwargs, needle):
    from agent_tasks import create_task
    user = make_user(db, name="Sam")
    r = create_task(user.id, **kwargs)
    assert "error" in r and needle in r["error"]


def test_create_caps_active_tasks_per_user(db, monkeypatch):
    from agent_tasks import create_task
    monkeypatch.setattr(config, "TASKS_MAX_ACTIVE_PER_USER", 2)
    user = make_user(db, name="Sam")
    assert "id" in create_task(user.id, "look up the rsf hours and text me", delay_minutes=10)
    assert "id" in create_task(user.id, "find the 61c midterm room and text me", delay_minutes=10)
    r = create_task(user.id, "a third thing to look up and text me", delay_minutes=10)
    assert "error" in r and "already" in r["error"]


def test_watch_creates_a_recurring_task_with_an_end(db):
    from agent_tasks import create_task
    user = make_user(db, name="Sam")
    r = create_task(user.id, "watch CS170 enrollment for an open seat", kind="watch", every_minutes=30, for_hours=6)
    row = _task(db, r["id"])
    assert row.kind == "watch" and row.every_minutes == 30
    assert timedelta(hours=5, minutes=50) <= (row.until - _utcnow()) <= timedelta(hours=6, minutes=1)
    assert (row.run_at - _utcnow()) <= timedelta(minutes=31)      # first check soon, not after 30 min


def test_cancel(db):
    from agent_tasks import create_task, cancel_task, active_tasks
    user = make_user(db, name="Sam")
    r = create_task(user.id, "look up the rsf hours and text me", delay_minutes=10)
    assert len(active_tasks(user.id)) == 1
    assert cancel_task(user.id, r["id"])
    assert active_tasks(user.id) == []
    assert _task(db, r["id"]).status == "cancelled"
    assert not cancel_task(user.id, 999999)


# ─── context ────────────────────────────────────────────────────────────────

def test_context_block_lists_active_tasks_and_reaches_the_loop_and_heartbeat(db):
    from agent_tasks import create_task, context_block
    from agent_loop import build_loop_context
    from heartbeat import _proactive_context
    user = make_user(db, name="Sam")
    assert context_block(user, db) is None
    create_task(user.id, "find the CS61C midterm room and text me", delay_minutes=45)
    block = context_block(user, db)
    assert block and "TASKS YOU'RE WORKING ON" in block and "midterm room" in block
    assert "TASKS YOU'RE WORKING ON" in build_loop_context(user, db)
    assert "TASKS YOU'RE WORKING ON" in _proactive_context(user, db)


def test_context_block_absent_when_flag_off(db, monkeypatch):
    from agent_tasks import create_task, context_block
    user = make_user(db, name="Sam")
    create_task(user.id, "find the CS61C midterm room and text me", delay_minutes=45)
    monkeypatch.setattr(config, "TASKS_ENABLED", False)
    assert context_block(user, db) is None


# ─── the sweep + the run ────────────────────────────────────────────────────

def _due(db, user, goal="find the CS61C midterm room and text me", **kw):
    from agent_tasks import create_task
    from models import AgentTask
    r = create_task(user.id, goal, delay_minutes=5, **kw)
    row = db.get(AgentTask, r["id"])
    row.run_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    return r["id"]


def test_not_due_does_not_run(db, anthropic_stub, sms_capture):
    from agent_tasks import create_task, fire_due
    user = make_user(db, name="Sam")
    create_task(user.id, "find the CS61C midterm room and text me", delay_minutes=45)
    assert fire_due() == 0
    assert not anthropic_stub.calls and not sms_capture


def test_due_lookup_reports_and_completes(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    anthropic_stub.push(_report("midterm's in wheeler 150, 7-9pm thursday"))
    assert fire_due() == 1
    assert sms_capture and "wheeler 150" in sms_capture[-1][1]
    row = _task(db, tid)
    assert row.status == "done" and row.runs == 1 and "wheeler" in (row.result or "")
    from models import Message
    out = db.query(Message).filter(Message.user_id == user.id, Message.direction == "out").order_by(Message.id.desc()).first()
    assert out.message_type == "task"


def test_run_sees_the_goal_and_the_user_context_and_only_read_tools(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    for f in ("LOOKUP_EVENTS_TOOL_ENABLED", "GET_DINING_MENU_TOOL_ENABLED", "FETCH_PAGE_TOOL_ENABLED",
              "WEB_SEARCH_TOOL_ENABLED", "LOG_MEAL_TOOL_ENABLED", "REMEMBER_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    user = make_user(db, name="Sam")
    _due(db, user, goal="find the CS61C midterm room and text me")
    anthropic_stub.push(_report("found it"))
    fire_due()
    kw = anthropic_stub.calls[-1]
    names = {t.get("name") for t in kw["tools"]}
    assert {"report", "nothing_to_report"} <= names
    assert {"fetch_page", "web_search", "lookup_events", "get_dining_menu"} <= names
    assert not ({"log_meal", "remember", "manage_log", "set_reminder", "schedule_task", "send_text"} & names), names
    sent = str(kw["system"]) + str(kw["messages"])
    assert "CS61C midterm room" in sent and "SAM" in sent.upper()


def test_run_executes_read_tools_and_feeds_results_back(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    import agent_tools
    monkeypatch.setattr(config, "LOOKUP_EVENTS_TOOL_ENABLED", True)
    seen = {}
    monkeypatch.setitem(agent_tools._HANDLERS, "lookup_events",
                        lambda uid, inp, **kw: seen.setdefault("called", inp) and "ok: 1 event — CS61C Midterm Thu 7pm Wheeler 150")
    user = make_user(db, name="Sam")
    _due(db, user)
    anthropic_stub.push(ToolUse("lookup_events", {"query": "61c midterm"}, id="toolu_le"), _report("thu 7pm wheeler 150"))
    assert fire_due() == 1
    assert seen["called"] == {"query": "61c midterm"}
    # the tool result went back to the model before it reported
    second = anthropic_stub.calls[-1]["messages"]
    assert any(isinstance(m.get("content"), list)
               and any(isinstance(c, dict) and c.get("type") == "tool_result" and "Wheeler 150" in str(c.get("content"))
                       for c in m["content"])
               for m in second)
    assert "wheeler 150" in sms_capture[-1][1]


def test_lookup_that_finds_nothing_still_keeps_the_promise_honestly(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "course page has no room yet"}, id="toolu_n"))
    assert fire_due() == 1
    assert sms_capture, "a lookup the user asked for must close the loop even when empty-handed"
    low = sms_capture[-1][1].lower()
    assert "couldn't" in low or "nothing" in low or "no " in low
    assert _task(db, tid).status == "done"


# ─── gates (defer, never drop; cancel only when the user is gone) ───────────

def test_standing_quiet_hours_defer_the_run(db, anthropic_stub, sms_capture, monkeypatch):
    import agent_tasks
    from agent_tasks import fire_due
    monkeypatch.setattr(agent_tasks, "_in_quiet_hours", lambda user, now: True)
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    assert fire_due() == 0
    assert not anthropic_stub.calls and not sms_capture
    row = _task(db, tid)
    assert row.status == "pending" and row.run_at > _utcnow() + timedelta(minutes=10)


def test_opted_out_or_inactive_user_cancels(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    from models import User
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    u = db.get(User, user.id)
    u.opted_out = True
    db.commit()
    assert fire_due() == 0
    assert not sms_capture and _task(db, tid).status == "cancelled"


def test_still_onboarding_cancels(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    from models import User
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    u = db.get(User, user.id)
    u.onboarding_step = 1
    db.commit()
    assert fire_due() == 0
    assert not sms_capture and _task(db, tid).status == "cancelled"


# ─── watchers ───────────────────────────────────────────────────────────────

def test_watch_silent_check_rearms_without_sending(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    user = make_user(db, name="Sam")
    tid = _due(db, user, goal="watch CS170 enrollment for an open seat", kind="watch", every_minutes=30, for_hours=6)
    anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "still full"}, id="toolu_n"))
    assert fire_due() == 0
    assert not sms_capture
    row = _task(db, tid)
    assert row.status == "pending" and row.runs == 1
    assert timedelta(minutes=29) <= (row.run_at - _utcnow()) <= timedelta(minutes=31)


def test_watch_report_sends_and_completes(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    user = make_user(db, name="Sam")
    tid = _due(db, user, goal="watch CS170 enrollment for an open seat", kind="watch", every_minutes=30, for_hours=6)
    anthropic_stub.push(_report("seat just opened in 170 — go grab it"))
    assert fire_due() == 1
    assert "seat just opened" in sms_capture[-1][1]
    assert _task(db, tid).status == "done"


def test_watch_past_its_end_closes_with_one_honest_line(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    from models import AgentTask
    user = make_user(db, name="Sam")
    tid = _due(db, user, goal="watch CS170 enrollment for an open seat", kind="watch", every_minutes=30, for_hours=6)
    row = db.get(AgentTask, tid)
    row.until = _utcnow() - timedelta(minutes=1)
    db.commit()
    assert fire_due() == 1
    assert not anthropic_stub.calls, "an expired watch closes in code — no model run"
    low = sms_capture[-1][1].lower()
    assert "cs170" in low and ("no " in low or "didn't" in low or "never" in low)
    assert _task(db, tid).status == "done"


# ─── failure envelope ───────────────────────────────────────────────────────

def test_model_failure_retries_once_then_keeps_the_promise(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    from models import AgentTask
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(RuntimeError("api down")))
    assert fire_due() == 0
    row = _task(db, tid)
    assert row.status == "pending" and row.runs == 1 and row.run_at > _utcnow()
    assert not sms_capture
    row = db.get(AgentTask, tid)
    row.run_at = _utcnow() - timedelta(minutes=1)
    db.commit()
    assert fire_due() == 1
    row = _task(db, tid)
    assert row.status == "failed"
    low = sms_capture[-1][1].lower()
    assert "couldn't" in low and "midterm room" in low


def test_tool_loop_is_bounded(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    import agent_tools
    monkeypatch.setattr(config, "LOOKUP_EVENTS_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "TASKS_MAX_TOOL_ITERS", 3)
    monkeypatch.setitem(agent_tools._HANDLERS, "lookup_events", lambda uid, inp, **kw: "ok: nothing")
    user = make_user(db, name="Sam")
    tid = _due(db, user)
    anthropic_stub.reply_with(lambda kw: ToolUse("lookup_events", {"query": "again"}, id="toolu_le"))
    fire_due()
    assert len(anthropic_stub.calls) == 3
    row = _task(db, tid)
    assert row.status in ("pending", "failed")           # exhausted = a failed attempt, retried once


# ─── the tools + registry + prompts ─────────────────────────────────────────

def test_schedule_task_tool_creates_and_cancel_tool_cancels(db):
    from agent_tools import dispatch_tool
    from agent_tasks import active_tasks
    user = make_user(db, name="Sam")
    out = dispatch_tool("schedule_task", {"goal": "find the CS61C midterm room and text me", "delay_minutes": 30}, user.id)
    assert out.startswith("ok:") and "id=" in out
    tid = int(out.split("id=")[1].split()[0].strip(")"))
    assert [t.id for t in active_tasks(user.id)] == [tid]
    assert dispatch_tool("cancel_task", {"task_id": tid}, user.id).startswith("ok:")
    assert active_tasks(user.id) == []


def test_schedule_task_tool_refuses_when_off(db, monkeypatch):
    from agent_tools import dispatch_tool
    monkeypatch.setattr(config, "TASKS_ENABLED", False)
    user = make_user(db, name="Sam")
    assert dispatch_tool("schedule_task", {"goal": "find the CS61C midterm room and text me", "delay_minutes": 30}, user.id).startswith("error:")


def test_task_counts_as_proactive_for_anti_stack():
    from engagement_tracker import PROACTIVE_MESSAGE_TYPES
    assert "task" in PROACTIVE_MESSAGE_TYPES


def test_loop_offers_the_tools_behind_the_flag():
    import agent_loop
    src = open(agent_loop.__file__).read()
    assert "if config.TASKS_ENABLED:" in src and "SCHEDULE_TASK_TOOL" in src and "CANCEL_TASK_TOOL" in src


def test_prompts_carry_the_promise_rule():
    voice = open("prompts/voice.md").read()
    assert "schedule_task" in voice
    assert "TASKS YOU'RE WORKING ON" in voice
