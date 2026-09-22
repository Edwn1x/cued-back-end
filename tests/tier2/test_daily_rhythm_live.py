"""
Tier-2 (live) anchors for the daily rhythm (rewrite/daily-rhythm/CHANGESPEC.md).
Binary anchors per the founder rule: parametrized x3, pass every time or keep tuning.

  (a) MEAL GAP yes-anchor: 2pm local, wake 6:50, nothing logged today → the heartbeat
      speaks, and the text asks about food in at most two short lines.
  (b) 'less' anchor: the same user with checkin_level='less' → the MEAL GAP block does not
      render; the tick stays silent or speaks about something other than food.
  (c) WATER anchor: "can u remind me to drink water" through the agent loop → set_reminder
      is called with every_hours set (an interval row exists).
Run: python3 -m pytest tests/tier2/test_daily_rhythm_live.py --run-tier2 -q -p no:cacheprovider
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2

AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", current_split="none",
               workout_days="mon,wed,fri", workout_time="18:00", wake_time="06:50", sleep_time="00:00",
               meals_per_day="3", height_ft=5, height_in=2, weight_lbs=180, age=16, gender="female",
               goal="fat_loss", activity_level="lightly_active", calorie_target=1400, protein_target=173)

SUMMARY = ("## Coaching Decisions\n- 1400 cal, 173g protein/day, confirmed.\n"
           "- Training: mon/wed/fri bodyweight in her room, nights after homework.\n"
           "## Recent Themes\n- Logs most meals by text; said the check-ins felt too far apart.\n"
           "## Workouts Completed\n- Wed full body (this week).")

FOOD_WORDS = re.compile(r"\beat|eaten|ate\b|food|lunch|breakfast|meal|log|fuel|hungry|snack|protein", re.I)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_msg(s, user_id, direction, when, body, message_type="freeform"):
    from models import Message
    s.add(Message(user_id=user_id, direction=direction, body=body, message_type=message_type, created_at=when))


def _daytime_tz():
    """A fixed-offset zone where the user's local time is ~2pm now (MEAL GAP: wake+5h has
    passed, nothing logged), and the model's own night gate can't be the reason for silence."""
    offset = (14 - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


def _seed_gap_user(db, monkeypatch, **extra):
    import config
    from models import get_session, Workout
    from tests.factories import make_user
    for f in ("HEARTBEAT_MEAL_GAP_ENABLED", "HEARTBEAT_RHYTHM_ENABLED", "SET_CHECKIN_LEVEL_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, **dict(AISLINN, user_timezone=_daytime_tz(), coaching_summary=SUMMARY,
                                created_at=_now() - timedelta(days=9)), **extra)
    s = get_session()
    try:
        # trained yesterday → no TRAINING GAP; a closed, non-question exchange ~20h ago →
        # no open thread, no live conversation. The only standing material is the meal gap.
        s.add(Workout(user_id=user.id, workout_type="full_body", completed=True, date=_now() - timedelta(days=1)))
        _seed_msg(s, user.id, "in", _now() - timedelta(hours=20), "done w the workout, 25 min")
        _seed_msg(s, user.id, "out", _now() - timedelta(hours=20, minutes=-1), "logged, nice work")
        s.commit()
    finally:
        s.close()
    return user


@pytest.mark.parametrize("run", [1, 2, 3])
def test_heartbeat_speaks_about_food_on_a_meal_gap(db, monkeypatch, run):
    import heartbeat
    from models import get_session, User
    user = _seed_gap_user(db, monkeypatch)
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## MEAL GAP" in ctx, "fixture broke: the MEAL GAP block must render at 2pm with nothing logged"

    spoke, payload, _ = heartbeat.decide(user.id)
    print(f"\n[MEAL-GAP run {run}] spoke={spoke} :: {payload!r}")
    assert spoke is True, f"YES-ANCHOR FAILED (run {run}): 2pm, nothing logged since 6:50 — silence: {payload!r}"
    assert FOOD_WORDS.search(payload), f"spoke, but not about food: {payload!r}"
    lines = [ln for ln in payload.strip().splitlines() if ln.strip()]
    assert len(lines) <= 2 and len(payload) <= 220, f"more than two short lines: {payload!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_less_level_drops_the_meal_gap(db, monkeypatch, run):
    import heartbeat
    from models import get_session, User
    user = _seed_gap_user(db, monkeypatch, checkin_level="less")
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## MEAL GAP" not in ctx and "## CHECK-IN LEVEL" in ctx and "less" in ctx

    spoke, payload, _ = heartbeat.decide(user.id)
    print(f"\n[LESS run {run}] spoke={spoke} :: {payload!r}")
    if spoke:
        assert not re.search(r"\beat|eaten|ate\b|lunch|breakfast|meal|log", payload, re.I), \
            f"'less' user got a food nudge anyway: {payload!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_water_ask_sets_an_interval_reminder(db, monkeypatch, run):
    import config
    from agent_loop import run_agent_loop
    from models import get_session, User, Reminder
    from tests.factories import make_user
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "REMINDERS_ENABLED", "WATER_REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True)
    user = make_user(db, **AISLINN, user_timezone="America/Los_Angeles", coaching_summary=SUMMARY)
    s = get_session()
    try:
        u = s.get(User, user.id)
        reply = run_agent_loop(u, "can u remind me to drink water", "freeform")
    finally:
        s.close()
    print(f"\n[WATER run {run}] {reply!r}")
    s = get_session()
    try:
        rows = s.query(Reminder).filter(Reminder.user_id == user.id, Reminder.active.is_(True)).all()
    finally:
        s.close()
    assert rows, f"no reminder set for a water ask: {reply!r}"
    assert any(r.every_hours for r in rows), \
        f"set_reminder called without every_hours: {[(r.text, r.local_time, r.recur_days, r.every_hours) for r in rows]}"
    assert all(1 <= r.every_hours <= 4 for r in rows if r.every_hours), [r.every_hours for r in rows]
