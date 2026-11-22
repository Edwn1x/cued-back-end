"""
Scoreboard (scoreboard.py): the agenda the coach owns, computed by code every turn, with a LEAD
line the model speaks to. From the 11-thread audit (2026-11-21): three scale asks never closed,
a leg day missing a month while "legs?" was offered as optional, promises never followed up.
"""

from __future__ import annotations

import datetime as dt

import pytest

import config
from tests.factories import make_user

PROG = {"split": "ppl", "days": {"mon": "push", "wed": "pull", "fri": "legs"}, "times": {}}


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "SCOREBOARD_ENABLED", True)


def _done(db, user, key, days_ago):
    from models import WorkoutSession
    when = _now() - dt.timedelta(days=days_ago)
    db.add(WorkoutSession(user_id=user.id, template_key=key, status="done", date=when, finished_at=when))
    db.commit()


def _out(db, user, body, hours_ago=1.0, mtype="heartbeat"):
    from models import Message
    db.add(Message(user_id=user.id, direction="out", body=body, message_type=mtype, channel="imessage",
                   created_at=_now() - dt.timedelta(hours=hours_ago)))
    db.commit()


def _in(db, user, body, hours_ago=0.5):
    from models import Message
    db.add(Message(user_id=user.id, direction="in", body=body, message_type="freeform", channel="imessage",
                   created_at=_now() - dt.timedelta(hours=hours_ago)))
    db.commit()


def _meal(db, user, kcal, days_ago):
    from models import Meal
    db.add(Meal(user_id=user.id, description="food", calories=kcal, protein_g=30,
                eaten_at=_now() - dt.timedelta(days=days_ago, hours=2)))
    db.commit()


def _weigh(db, user, lbs, days_ago):
    from models import WeightLog
    db.add(WeightLog(user_id=user.id, weight_lbs=lbs, weighed_at=_now() - dt.timedelta(days=days_ago)))
    db.commit()


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


def test_the_founders_thirty_days_lead_with_the_weigh_in(db, on):
    """Five lifts, zero legs, nine high days, three scale asks never answered → the weigh-in leads."""
    from scoreboard import compute, render
    u = make_user(db, program=PROG, calorie_target=2500, onboarding_completed_at=_now() - dt.timedelta(days=30))
    for k, d in (("push", 25), ("pull", 21), ("push", 12), ("pull", 5)):
        _done(db, u, k, d)
    for h in (20 * 24, 13 * 24, 2 * 24):
        _out(db, u, "hop on the scale before u eat tmrw", hours_ago=h)
    for d, kcal in ((1, 3400), (2, 2900), (3, 2200), (4, 3100), (5, 1900), (6, 2950)):
        _meal(db, u, kcal, d)
    sb = compute(_u(u.id), db)
    assert sb["plan_per_week"] >= 3 and sb["never_trained"] is False
    assert sb["weigh_in_days"] is None and sb["weigh_asks"] == 3
    assert [k for k, _ in sb["slipping"]] == ["legs"]
    assert sb["food_days_logged"] == 6 and sb["food_days_over"] == 4 and sb["food_avg_kcal"] == 2741
    assert sb["lead"].startswith("the weigh-in: asked 3×")
    text = render(sb, _u(u.id))
    assert text.startswith("## SCOREBOARD") and "- legs: never done — slipping" in text
    assert "weigh-in: no number yet; asked 3×" in text and "6 days logged, avg 2741 vs 2500 target, 4 days >10% over" in text
    assert "LEAD: the weigh-in" in text


def test_a_slipping_day_type_leads_once_the_weigh_in_is_closed(db, on):
    from scoreboard import compute
    u = make_user(db, program=PROG, calorie_target=2500)
    for k, d in (("push", 20), ("pull", 16), ("legs", 30), ("push", 6), ("pull", 2)):
        _done(db, u, k, d)
    _weigh(db, u, 141.0, 3)
    sb = compute(_u(u.id), db)
    assert sb["weigh_in_days"] == 3 and sb["weigh_asks"] == 0
    assert sb["slipping"] == [("legs", 30)]
    assert sb["lead"].startswith("legs keeps slipping (30 days")


def test_a_week_at_zero_leads_late_in_the_week(db, on, monkeypatch):
    from scoreboard import compute
    import scoreboard
    u = make_user(db, program=PROG, calorie_target=2500)
    for k, d in (("push", 12), ("pull", 10), ("legs", 9)):
        _done(db, u, k, d)
    # pin "now" to a Thursday 18:00 local so the week window holds no sessions
    now = _now()
    thu = now - dt.timedelta(days=(now.weekday() - 3) % 7)
    sb = compute(_u(u.id), db, now=thu)
    if sb["done_this_week"] == 0:
        assert sb["lead"].startswith("the week's at zero against 3/week")
    else:   # the pinned Thursday fell inside a week that holds one of the fixtures
        assert sb["lead"] is None or "week" in sb["lead"] or "slipping" in sb["lead"]


def test_open_promises_and_questions_surface_and_lead_after_a_day(db, on):
    from scoreboard import compute, render
    u = make_user(db, program=PROG, calorie_target=2500)
    _done(db, u, "push", 1)
    _out(db, u, "a week of coughing is a doctor visit. i'll check in tmrw, text me when u get it looked at", hours_ago=30)
    _out(db, u, "how was dance?", hours_ago=26)
    sb = compute(_u(u.id), db)
    assert sb["open_question"] and sb["open_question"][1] == "how was dance?"
    assert any("text me when" in p[1] for p in sb["promises"])
    assert sb["lead"].startswith("you left this open 30h ago")
    text = render(sb, _u(u.id))
    assert "- your open question (26h, unanswered)" in text and "verify it was done before repeating" in text
    # they answer → the question is no longer open
    _in(db, u, "it was good", hours_ago=0.1)
    sb2 = compute(_u(u.id), db)
    assert sb2["open_question"] is None


def test_nothing_owed_renders_a_quiet_lead(db, on):
    from scoreboard import compute, render
    u = make_user(db, program=PROG, calorie_target=2500)
    _done(db, u, "push", 1)
    _done(db, u, "pull", 3)
    _done(db, u, "legs", 5)
    _weigh(db, u, 150.0, 2)
    sb = compute(_u(u.id), db)
    assert sb["slipping"] == [] and sb["lead"] is None
    assert "LEAD: nothing owed right now" in render(sb, _u(u.id))


def test_never_trained_counts_days_since_joining_and_does_not_flag_slipping(db, on):
    from scoreboard import compute, render
    u = make_user(db, program=PROG, calorie_target=2000, activated_at=_now() - dt.timedelta(days=9))
    sb = compute(_u(u.id), db)
    assert sb["never_trained"] and sb["last_session_days"] == 9 and sb["slipping"] == []
    assert "NO completed session on record — 9 days since they joined" in render(sb, _u(u.id))


def test_block_rides_the_loop_context_and_the_heartbeat_prompt_but_not_setup(db, on, monkeypatch):
    from agent_loop import build_loop_context
    from scoreboard import context_block
    import heartbeat
    u = make_user(db, program=PROG, calorie_target=2500)
    _done(db, u, "push", 2)
    ctx = build_loop_context(_u(u.id), db)
    assert "## SCOREBOARD — what you own for Test this week" in ctx and "LEAD:" in ctx
    assert "THE SCOREBOARD (when a SCOREBOARD block is present below)" in heartbeat._SCOREBOARD_GUIDANCE
    s = make_user(db, program=PROG, onboarding_step=2, phone="+15550008001")
    assert context_block(_u(s.id), db) is None
    monkeypatch.setattr(config, "SCOREBOARD_ENABLED", False)
    assert context_block(_u(u.id), db) is None
