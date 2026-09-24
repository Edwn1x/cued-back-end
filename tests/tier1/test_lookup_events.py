"""lookup_events: query the full synced Event table by keyword + window, so a due
date weeks out (past the 7-day UPCOMING context) is findable instead of the coach
saying "not on the feed" (live: founder 2026-09-24, HW4 due 8 days out). Plus the
immediate sync_now hook that pulls events right after a connect."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user


def _mk_event(user_id, title, days_out, *, source="bcourses", all_day=False, ext=None):
    from events import upsert_external_event
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    when = (now + timedelta(days=days_out)).replace(hour=7, minute=0, second=0, microsecond=0)
    upsert_external_event(user_id, source=source, external_id=ext or title,
                          title=title, occurred_at=when, all_day=all_day)


# ─── the fix: find events past the 7-day UPCOMING window ─────────────────────

def test_finds_a_due_date_beyond_the_7day_window(db):
    from agent_tools import handle_lookup_events
    user = make_user(db)
    _mk_event(user.id, "due: Homework 4: RISC-V [CS61C Fa26]", 8)   # 8 days out
    out = handle_lookup_events(user.id, {"query": "homework 4"})
    assert out.startswith("ok:") and "Homework 4" in out


def test_query_matches_course_code(db):
    from agent_tools import handle_lookup_events
    user = make_user(db)
    _mk_event(user.id, "due: Homework 5 [CS61C Fa26]", 20, ext="hw5")
    _mk_event(user.id, "due: Essay [ENGLISH 1A]", 22, ext="essay")
    out = handle_lookup_events(user.id, {"query": "cs61c"})
    assert "Homework 5" in out and "Essay" not in out


def test_no_match_is_a_clean_answer_not_an_error(db):
    from agent_tools import handle_lookup_events
    user = make_user(db)
    _mk_event(user.id, "due: HW1 [CS70]", 10, ext="hw1")
    out = handle_lookup_events(user.id, {"query": "organic chemistry final"})
    assert "no events matching" in out and "error" not in out


def test_days_ahead_bounds_the_window(db):
    from agent_tools import handle_lookup_events
    user = make_user(db)
    _mk_event(user.id, "due: Final Project [DATA C104]", 60, ext="finalproj")   # 60 days out
    assert "no events" in handle_lookup_events(user.id, {"query": "final", "days_ahead": 30})
    assert "Final Project" in handle_lookup_events(user.id, {"query": "final", "days_ahead": 90})


def test_all_day_event_renders_without_a_time(db):
    from agent_tools import handle_lookup_events
    user = make_user(db)
    _mk_event(user.id, "Fall Break", 15, all_day=True, ext="break")
    out = handle_lookup_events(user.id, {"query": "break"})
    assert "Fall Break" in out and "(all day)" in out


def test_flag_off_still_dispatches_handler_directly(db):
    # the handler itself doesn't gate on the flag (the tool-offer does); a direct call works
    from agent_tools import handle_lookup_events
    user = make_user(db)
    _mk_event(user.id, "due: HW9 [CS61C]", 12, ext="hw9")
    assert "HW9" in handle_lookup_events(user.id, {"query": "hw9"})


# ─── immediate sync on connect ───────────────────────────────────────────────

def test_gcal_provider_sync_now_calls_sync_user(monkeypatch):
    import integrations.gcal as gcal
    import integrations.gcal_sync as gcal_sync
    seen = {}
    monkeypatch.setattr(gcal_sync, "sync_user", lambda uid: seen.setdefault("uid", uid) or {"upserted": 3})
    gcal.GCalProvider().sync_now(42)
    assert seen["uid"] == 42


def test_base_provider_sync_now_is_a_noop():
    from integrations.base import Provider
    assert Provider().sync_now(1) is None


def test_lookup_events_is_wired():
    import agent_tools
    assert agent_tools._HANDLERS.get("lookup_events") is agent_tools.handle_lookup_events
    assert agent_tools.LOOKUP_EVENTS_TOOL["name"] == "lookup_events"
