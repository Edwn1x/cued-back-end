"""
A sign-up who is not a student (live 2026-11-05, user 93: 28, signed up off the hero page,
no occupation on the form).

1. "not a student" is not a student. An occupation of "works full time, not a student" read
   as a college student (the substring): the calendar step went out about "ur classes and
   due dates" and the bCourses step followed.
2. The Berkeley assumption is for the campus. With no occupation the first reply was about to
   be "im assuming ur a berkeley student? what major" — to a 28-year-old (and, 2026-10-28, to
   user 80: 26, desk job). Past ONBOARDING_ASSUME_STUDENT_MAX_AGE on the sign-up, or with an
   occupation on file that isn't a student's, the intake asks about work and drops the campus.
3. The stalled-setup nudge names THEIR goals: three goals rendered "get stronger, leaner and
   fitter" for someone who never picked fat loss.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE


def test_knob_default():
    assert config.ONBOARDING_ASSUME_STUDENT_MAX_AGE == 24


# ─── 1. a negation is not a student ───────────────────────────────────────────

@pytest.mark.parametrize("occupation,expected", [
    ("works full time, not a student", False), ("not a student", False), ("Not a Berkeley student, works in tech", False),
    ("non-student", False), ("nonstudent, desk job", False), ("no longer a student", False),
    ("former student, now a nurse", False), ("graduated, software engineer", False), ("isn't a student", False),
    ("student", True), ("cs student at berkeley", True), ("grad student", True), ("student, not working", True),
    ("works full time", False),
])
def test_not_a_student_is_not_a_student(occupation, expected):
    from connect_offers import is_student
    from capabilities import _is_student
    u = NS(occupation=occupation, year=None)
    assert is_student(u) is expected and _is_student(u) is expected


def test_the_live_value_gets_the_week_copy_and_no_bcourses(db):
    from connect_offers import _directive_text, first_offer_candidates
    u = make_user(db, occupation="works full time, not a student", age=28)
    out = _directive_text("offer_link", "gcal", None, "next:", user=u)
    assert "classes" not in out and "due dates" not in out and "ur week" in out, out
    assert all(c[1] != "bcourses" for c in first_offer_candidates(db, u))


# ─── 2. the Berkeley assumption stops at the campus ───────────────────────────

def _signup(db, **over):
    kw = dict(INTAKE, name="karina", age=28, gender="female", goal="muscle_building,endurance,general_fitness",
              experience="beginner", equipment="full_gym", onboarding_step=1)
    kw.update(over)
    return make_user(db, **kw)


@pytest.mark.parametrize("over,expected", [
    (dict(age=28), False), (dict(age=25), False), (dict(age=24), True), (dict(age=19), True), (dict(age=None), True),
    (dict(age=28, year="senior"), True), (dict(age=28, which_gym="rsf"), True),
    (dict(age=28, occupation="grad student at berkeley"), True), (dict(age=20, occupation="barista"), False),
    (dict(age=20, occupation="works full time, not a student"), False),
])
def test_who_is_assumed_a_student(db, over, expected):
    import onboarding_agent as oa
    assert oa._assume_student(_signup(db, **over)) is expected


def test_age_gate_off_assumes_everyone(db, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_ASSUME_STUDENT_MAX_AGE", 0)
    assert oa._assume_student(_signup(db, age=41)) is True


def test_a_working_adult_is_asked_about_work_not_a_major(db):
    import onboarding_agent as oa
    u = _signup(db)
    hints = dict(oa._get_missing_fields(u))
    assert "berkeley" not in hints["occupation"].lower().replace("never mention berkeley", "")
    assert "what do u do for work" in hints["occupation"] and "ASSUME a UC Berkeley" not in hints["occupation"]
    assert "campus" not in hints["activity_level"] and "dining hall" not in hints["cooking_situation"]
    plain = {f[0]: oa._plain(f, u) for f in oa._get_missing_fields(u)}
    assert plain["occupation"] == "what u do for work"
    assert plain["cooking_situation"] == "if u cook most nights or mostly eat out"
    prompt = oa._build_system_prompt(u)
    assert "A number texting cued is a\n  Berkeley student" not in prompt
    assert "NOT that they're a student" in prompt


def test_a_student_age_sign_up_keeps_the_assumption(db):
    import onboarding_agent as oa
    u = _signup(db, age=20)
    hints = dict(oa._get_missing_fields(u))
    assert "ASSUME a UC Berkeley student" in hints["occupation"]
    assert "walking around campus" in hints["activity_level"] and "dining hall" in hints["cooking_situation"]
    assert oa._plain(("occupation", ""), u).startswith("what u do (im assuming berkeley student")
    assert oa._plain(("occupation", "")) == oa.PLAIN_ASKS["occupation"]
    prompt = oa._build_system_prompt(u)
    assert "im assuming ur a berkeley student? what major" in prompt
    assert "NOT that they're a student" not in prompt


def test_the_nudge_ask_for_a_working_adult_has_no_major(db):
    import onboarding_reengage as r
    u = _signup(db, height_ft=5, height_in=5, weight_lbs=125)
    assert r._next_ask(u) == "what u do for work"


# ─── 3. the nudge names their goals ───────────────────────────────────────────

@pytest.mark.parametrize("goal,expected", [
    ("muscle_building,endurance,general_fitness", "build muscle, build endurance and get in shape"),
    ("muscle_building", "build muscle"), ("fat_loss,strength", "lean out and get stronger"),
    ("fat_loss,muscle_building,strength", "get stronger, leaner and fitter"),
    ("strength,endurance,general_fitness,muscle_building", "get stronger, build endurance and get in shape"), (None, "get in shape"),
])
def test_goal_phrase_is_their_goals(goal, expected):
    from onboarding_reengage import goal_phrase
    assert goal_phrase(NS(goal=goal)) == expected


def test_never_leaner_without_fat_loss():
    from onboarding_reengage import goal_phrase, nudge_text
    u = NS(goal="muscle_building,endurance,general_fitness", name="karina", user_timezone="America/Los_Angeles")
    assert "lean" not in goal_phrase(u)
    assert "lean" not in nudge_text(u, 1, replied=False)


# ─── 4. a held user is never nudged ───────────────────────────────────────────

def test_a_held_user_is_not_nudged(db, sms_capture, monkeypatch):
    from datetime import datetime, timedelta
    import onboarding_reengage as r
    from models import Message
    from sms import send_sms
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_START_AT", "")
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_MODEL_COPY_ENABLED", False)
    held, other = _signup(db), _signup(db, name="sam")
    for u in (held, other):
        send_sms(u.phone, "hey, it's cued. how's ur day going", user_id=u.id, message_type="onboarding")
    db.query(Message).update({Message.created_at: datetime(2026, 11, 5, 21, 3)})
    db.commit()
    now = datetime(2026, 11, 6, 18, 5)          # 10:05 PT the next morning, 21h on
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_HOLD_USER_IDS", frozenset({held.id}))
    assert r.eligible(db, held, now)[:2] == (False, "held")
    assert r.eligible(db, other, now)[:2] == (True, "first")
    sms_capture.clear()
    assert r.sweep(now) == 1
    assert {p for p, _b in sms_capture} == {other.phone}


def test_hold_list_is_empty_by_default():
    assert config.ONBOARDING_REENGAGE_HOLD_USER_IDS == frozenset()
