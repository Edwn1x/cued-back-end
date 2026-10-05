"""
Tier-2 (live) — deferred tasks end to end with the real model.

1. "find out where the 61c midterm is and text me tonight" → the coach calls schedule_task
   (a row exists, with a tonight-ish run time) and does NOT claim it already has the answer.
2. A due lookup task runs live (fetch_page against the real cs61c.org) → one text with a
   real date/room, status done.
3. A question answerable now ("what's the weather") does NOT get scheduled.

Run: pytest --run-tier2 -s tests/tier2/test_agent_tasks_live.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2


def _flags(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "TASKS_ENABLED", "FETCH_PAGE_TOOL_ENABLED",
              "WEB_SEARCH_TOOL_ENABLED", "LOOKUP_EVENTS_TOOL_ENABLED", "WEATHER_ENABLED", "REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


def test_promise_becomes_a_task_row(db, monkeypatch, sms_capture):
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from agent_tasks import active_tasks
    _flags(monkeypatch)
    user = make_user(db, name="Sam", occupation="student", user_timezone="America/Los_Angeles")

    reply = run_agent_loop(user, "can you find out where the 61c midterm is and text me tonight around 8", "freeform")
    rows = active_tasks(user.id)
    print(f"\n[TASK] reply: {reply}")
    print(f"[TASK] rows: {[(r.goal, r.kind, r.run_at) for r in rows]}")

    assert rows, "the coach promised (or should have) but no task row exists — the promise has nothing behind it"
    low = reply.lower()
    assert not any(k in low for k in ("wheeler", "dwinelle", "pimentel", "vlsb", "li ka shing")), \
        "claimed a room now instead of scheduling the lookup"
    assert any(k in low for k in ("tonight", "8", "later", "text you", "let you know", "ping", "get back")), low


def test_due_task_runs_live_and_texts_the_answer(db, monkeypatch, sms_capture):
    from datetime import datetime, timezone, timedelta
    from tests.factories import make_user
    from agent_tasks import create_task, fire_due
    from models import AgentTask
    import webfetch
    webfetch._CACHE.clear()
    _flags(monkeypatch)
    user = make_user(db, name="Sam", occupation="student", user_timezone="America/Los_Angeles")
    r = create_task(user.id, "find when the CS61C midterm is from the course site cs61c.org and text me the dates", delay_minutes=5)
    row = db.get(AgentTask, r["id"])
    row.run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.commit()

    sent = fire_due()
    db.expire_all()
    row = db.get(AgentTask, r["id"])
    print(f"\n[TASK-RUN] sent={sent} status={row.status} runs={row.runs} result={row.result!r}")
    print(f"[TASK-RUN] sms: {sms_capture}")

    assert sent == 1 and row.status == "done"
    text = sms_capture[-1][1].lower()
    import re
    assert re.search(r"\b(10|11)/\d{1,2}\b|\b(oct|nov)\b", text), f"no date from the page in the text: {text}"
    assert "http" not in text


def test_answerable_now_is_not_scheduled(db, monkeypatch, sms_capture):
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from agent_tasks import active_tasks
    _flags(monkeypatch)
    user = make_user(db, name="Sam", occupation="student")
    reply = run_agent_loop(user, "do i need a jacket today", "freeform")
    print(f"\n[TASK-NOW] reply: {reply}")
    assert not active_tasks(user.id), "scheduled a task for a question it could answer now"
