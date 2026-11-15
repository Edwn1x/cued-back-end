"""
/waitlist stores the site's "occupation?" row (founder, 2026-11-14): `occupation` as the
free text the form phrases ("student at uc berkeley" / "nurse" / "student at sjsu, works
as barista" / "other"), `school` on its own column. Profiles by occupation and by school
are planned; non-students sign up too. Blank stays NULL so onboarding still asks — and
the existing student heuristics read the text as before.
"""

from __future__ import annotations

import json

import pytest


def _waitlist(client, phone, **extra):
    body = dict(name="Pat Lee", phone=phone, sms_consent=True, source="hero")
    body.update(extra)
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


@pytest.fixture(autouse=True)
def _no_photon(monkeypatch):
    import photon
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)
    import app as appmod
    from onboarding_agent import send_onboarding_hook
    monkeypatch.setattr(appmod, "start_onboarding",
                        lambda user, reason="start_onboarding": send_onboarding_hook(user.id, reason=reason))


def _user(db, phone):
    from models import User
    return db.query(User).filter(User.phone == phone).one()


def test_student_with_a_school_is_stored_and_read_as_a_student(db, client):
    from onboarding_agent import _assume_student, _is_berkeley_student
    from connect_offers import is_student
    code, data = _waitlist(client, "5105550911", occupation="student at uc berkeley", school="uc berkeley",
                           occupation_kind="student", job=None)
    assert code == 200 and data["status"] == "ok"
    u = _user(db, "+15105550911")
    assert (u.occupation, u.school) == ("student at uc berkeley", "uc berkeley")
    assert is_student(u) and _assume_student(u) and _is_berkeley_student(u)
    assert "School: uc berkeley" in u.profile_summary


def test_a_worker_is_stored_and_not_assumed_a_student(db, client):
    from onboarding_agent import _assume_student
    from connect_offers import is_student
    code, _ = _waitlist(client, "5105550912", occupation="nurse", school=None, age="31")
    assert code == 200
    u = _user(db, "+15105550912")
    assert (u.occupation, u.school) == ("nurse", None)
    assert not is_student(u) and not _assume_student(u)


def test_another_school_is_a_student_but_not_berkeley(db, client):
    from onboarding_agent import _assume_student, _is_berkeley_student
    code, _ = _waitlist(client, "5105550913", occupation="student at sjsu, works as barista", school="sjsu")
    assert code == 200
    u = _user(db, "+15105550913")
    assert u.school == "sjsu" and _assume_student(u) and not _is_berkeley_student(u)


def test_blank_stays_null_and_nonsense_is_refused(db, client):
    code, _ = _waitlist(client, "5105550914", occupation="", school="   ")
    assert code == 200
    u = _user(db, "+15105550914")
    assert u.occupation is None and u.school is None
    code, data = _waitlist(client, "5105550915", occupation="x" * 201)
    assert code == 400 and "occupation" in data["message"]
    code, data = _waitlist(client, "5105550916", school="y" * 201)
    assert code == 400 and "school" in data["message"]
