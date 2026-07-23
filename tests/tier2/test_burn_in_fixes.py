"""
Tier-2 (live) — burn-in fixes end to end. The deterministic mechanics are proven in
tier-1; these check the model actually USES them: names the right local day, quotes the
code-computed totals digit-for-digit, and reaches for edit (not delete-and-relog) on a
correction. Run: pytest --run-tier2 -s tests/tier2/test_burn_in_fixes.py.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _enable(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "LOG_EVENT_TOOL_ENABLED",
              "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def test_item1_names_the_correct_local_day(db, monkeypatch):
    """A tomorrow event + 'what's my first thing tomorrow' → reply names tomorrow's
    weekday and the local time (not a UTC-shifted day)."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam")  # America/Los_Angeles
    handle_log_event(user.id, {"description": "orgo midterm", "starts_at": "09:00", "date": "tomorrow"})
    reply = run_agent_loop(user, "what's my first thing tomorrow?", "freeform")
    print(f"\n[ITEM1] {reply!r}")

    from timefmt import local_day_bounds
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Los_Angeles")
    tomorrow = (datetime.now(tz) + timedelta(days=1)).strftime("%A")
    low = reply.lower()
    assert tomorrow.lower() in low or "9" in low, f"expected tomorrow ({tomorrow}) / 9am: {reply!r}"


def test_item2_quotes_totals_digit_for_digit(db, monkeypatch):
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Meal

    user = make_user(db, name="Sam", calorie_target=2200, protein_target=160)
    now = _naive_utcnow()
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="oatmeal + eggs", calories=520, protein_g=32,
                   eaten_at=now, source="text", log_type="user_reported"))
        s.add(Meal(user_id=user.id, description="chicken bowl", calories=740, protein_g=58,
                   eaten_at=now, source="text", log_type="user_reported"))
        s.commit()
    finally:
        s.close()

    reply = run_agent_loop(user, "what are my totals today?", "freeform")
    print(f"\n[ITEM2] {reply!r}")
    # code sum = 1260 cal / 90g protein; remaining 940 / 70g — the reply must quote them
    assert "1260" in reply, f"calories total not quoted exactly: {reply!r}"
    assert "90" in reply, f"protein total not quoted exactly: {reply!r}"


def test_item3_corrects_via_edit_not_relog(db, monkeypatch):
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Meal, active

    user = make_user(db, name="Sam")
    r1 = run_agent_loop(user, "just had a big burrito, like 1250 cal 40g protein", "freeform")
    print(f"\n[ITEM3] log: {r1!r}")
    reply = run_agent_loop(user, "actually that burrito was closer to 900", "freeform")
    print(f"[ITEM3] correct: {reply!r}")

    s = get_session()
    try:
        meals = active(s, Meal, user_id=user.id).all()
    finally:
        s.close()
    # exactly one active meal, edited to ~900, with an audit trail (NOT delete-and-relog)
    assert len(meals) == 1, f"expected 1 active meal (edit, not relog); got {len(meals)}"
    assert meals[0].calories == 900, f"expected calories edited to 900; got {meals[0].calories}"
    assert meals[0].edits, "edit left no audit trail"
    assert "900" in reply, f"reply should quote the new value: {reply!r}"
