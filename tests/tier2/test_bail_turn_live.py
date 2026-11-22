"""
Tier-2 (live) anchors for the bail turn: the three live folds, x3 each. The reply must PUSH on a
first pushback with no real reason (ask what's in the way / make it small / the habit) and must not
fold ("ok don't then", "alr … then", "no worries", "fair enough", "cleared"). A named real reason
(dance, hurts) must NOT be pushed against.
Run: pytest tests/tier2/test_bail_turn_live.py --run-tier2 -s
"""

from __future__ import annotations

import re

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2

FOLD_RE = re.compile(r"\b(ok don'?t then|alr(?:ight)?,? (?:tomorrow|tmrw|monday|next time|back) then|no worries|no stress|fair enough|"
                     r"i cleared|that'?s fine|all good|whenever u want|whenever ur ready)\b", re.I)
PUSH_RE = re.compile(r"\b(what'?s (?:actually )?(?:in the way|going on|stopping u|up)|day one|habit|20 min|15 min|just (?:the )?first|"
                     r"3 machines|one exercise|get there|u'?ll feel better|can'?t let me down|come on|just go|quick one|30 min|not optional|"
                     r"whole ask|still (?:go|happening)|get it in)\b", re.I)

# The live folds happened in the evening; the harness runs whenever it runs, so the test users live in
# a zone where it is evening right now (the loop reads the real clock).
import datetime as _dt
def _evening_tz() -> str:
    h = _dt.datetime.now(_dt.timezone.utc).hour
    offset = (18 - h) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"

PROG = {"split": "ppl", "days": {d: k for d, k in zip(["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                                       ["push", "pull", "legs", "push", "pull", "legs", "push"])}, "times": {}}


def _on(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "BAIL_TURN_BLOCK_ENABLED", "SCOREBOARD_ENABLED", "REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True, raising=False)


def _run(user_id, text):
    from agent_loop import run_agent_loop
    from models import get_session, User
    s = get_session()
    try:
        return run_agent_loop(s.get(User, user_id), text, "freeform") or ""
    finally:
        s.close()


@pytest.mark.parametrize("i", range(3))
def test_jayden_first_pushback_gets_a_push(db, monkeypatch, i):
    _on(monkeypatch)
    u = make_user(db, name="Jayden", age=17, gender="male", height_ft=5, height_in=4, weight_lbs=206, experience="none",
                  equipment="machines", goal="fat_loss", biggest_obstacle="consistency", program=PROG, workout_time="22:30",
                  calorie_target=2500, protein_target=146, preferred_channel="sms", user_timezone=_evening_tz())
    reply = _run(u.id, "Ughhh I don't want toooo")
    print(f"\n[jayden {i}] {reply!r}")
    assert not FOLD_RE.search(reply), reply
    assert PUSH_RE.search(reply), reply


@pytest.mark.parametrize("i", range(3))
def test_founder_swapping_legs_for_back_gets_held_to_legs(db, monkeypatch, i):
    _on(monkeypatch)
    import datetime as dt
    from models import WorkoutSession
    u = make_user(db, name="ed", age=20, gender="male", height_ft=5, height_in=6, weight_lbs=142, experience="intermediate",
                  equipment="full_gym", goal="fat_loss,muscle_building", program=PROG, workout_time="18:00",
                  calorie_target=2500, protein_target=142, preferred_channel="sms", user_timezone=_evening_tz())
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for k, d in (("push", 20), ("pull", 16), ("push", 6), ("pull", 2)):
        when = now - dt.timedelta(days=d)
        db.add(WorkoutSession(user_id=u.id, template_key=k, status="done", date=when, finished_at=when))
    # the legs card for tonight is already on his phone (his live swap was a bail on legs specifically)
    db.add(WorkoutSession(user_id=u.id, template_key="legs", status="planned", date=now))
    db.commit()
    reply = _run(u.id, "I'll prolly hit back instead ngl")
    print(f"\n[founder {i}] {reply!r}")
    assert not FOLD_RE.search(reply), reply
    assert re.search(r"\blegs?\b", reply, re.I), reply


@pytest.mark.parametrize("i", range(3))
def test_a_named_real_reason_is_not_pushed_against(db, monkeypatch, i):
    _on(monkeypatch)
    u = make_user(db, name="Sarah", age=19, gender="female", height_ft=5, height_in=3, weight_lbs=115, experience="none",
                  equipment="full_gym", goal="general_fitness", program=PROG, workout_time="14:00", calorie_target=1950,
                  protein_target=92, preferred_channel="sms", user_timezone=_evening_tz())
    reply = _run(u.id, "wait im just not at the gym hold on LOL like i got some work to do so unforch i will jsut go to dance thats my workout for the day")
    print(f"\n[sarah {i}] {reply!r}")
    assert not re.search(r"\b(can'?t let me down|come on|just go|day one or one day)\b", reply, re.I), reply
    assert re.search(r"\b(dance|tomorrow|tmrw|sunday|monday|set|reminder)\b", reply, re.I), reply
