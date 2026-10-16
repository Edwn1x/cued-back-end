"""Legacy public endpoints locked down (2026-10-15 probe): /signup took SQL-injection
strings as phone numbers and made a users row + a send attempt out of each; a JSON
integer phone 500'd with the exception text echoed; /activate-sms let anyone re-trigger
the first text to any member. Now: strict phone, real name, same limiter as /waitlist,
idempotent activate, generic error copy."""
from __future__ import annotations

import json

import pytest

from tests.factories import make_user


@pytest.fixture(autouse=True)
def _legacy_signup_on(monkeypatch):
    """These tests exercise the retired JSON /signup (+ /activate-sms); they stay as the
    rollback-path coverage behind LEGACY_SIGNUP_ENABLED."""
    import config
    monkeypatch.setattr(config, "LEGACY_SIGNUP_ENABLED", True)

PROBE_PHONES = ["+1111", "+1'OR'1'='1", "+11';DROPTABLEusers;", "+1admin'", "+11'ANDSLEEP3", "", "abc"]


def _signup(client, phone, name="Sis", **over):
    body = {"name": name, "age": 22, "gender": "female", "goal": ["fat_loss"], "experience": "beginner",
            "equipment": "full_gym", "phone": phone, "sms_consent": True}
    body.update(over)
    r = client.post("/signup", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


def _count_users(db):
    from models import User
    db.expire_all()
    return db.query(User).count()


@pytest.fixture
def no_provision(monkeypatch):
    import photon
    calls = []
    monkeypatch.setattr(photon, "provision_user", lambda uid: calls.append(uid) or False)
    return calls


@pytest.mark.parametrize("phone", PROBE_PHONES)
def test_probe_phones_are_rejected_without_a_row_or_a_send(db, client, sms_capture, no_provision, phone):
    before = _count_users(db)
    code, body = _signup(client, phone, name="test")
    assert code == 400 and body["status"] == "error"
    assert _count_users(db) == before
    assert sms_capture == [] and no_provision == []


def test_integer_phone_is_a_400_not_a_500(db, client):
    before = _count_users(db)
    code, body = _signup(client, 5105550199)        # JSON int — used to 500 on .strip()
    # an int that IS a valid number normalizes; the point is no crash and no leak
    assert code in (200, 400)
    assert "has no attribute" not in json.dumps(body)
    before = _count_users(db)
    code, body = _signup(client, 12)
    assert code == 400 and _count_users(db) == before


def test_blank_name_is_rejected(db, client):
    before = _count_users(db)
    code, _ = _signup(client, "5105550123", name="   ")
    assert code == 400 and _count_users(db) == before


def test_valid_signup_still_creates_the_user(db, client, no_provision):
    code, body = _signup(client, "5105550124", name="Sis")
    assert code == 200 and body["status"] == "ok"
    from models import User
    assert db.query(User).filter(User.phone == "+15105550124").one().name == "Sis"


def test_server_error_does_not_echo_the_exception(db, client, monkeypatch, no_provision):
    import app
    def boom(user):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(app, "start_onboarding", boom)
    code, body = _signup(client, "5105550125")
    assert code == 500
    assert "secret internal detail" not in json.dumps(body)


def test_signup_is_rate_limited_like_waitlist(db, client, no_provision):
    import app
    app.limiter.reset()
    app.limiter.enabled = True
    try:
        codes = [_signup(client, f"510555013{i}")[0] for i in range(4)]
    finally:
        app.limiter.enabled = False
        app.limiter.reset()
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


# ─── /activate-sms ───────────────────────────────────────────────────────────

def test_activate_sms_rejects_probe_phones(db, client, sms_capture):
    r = client.post("/activate-sms", data={"phone": "+1'OR'1'='1"})
    assert r.status_code == 400 and sms_capture == []


def test_activate_sms_is_a_noop_for_a_member_already_texted(db, client, monkeypatch):
    import app
    from models import Message
    user = make_user(db)
    db.add(Message(user_id=user.id, direction="out", body="hey", message_type="onboarding")); db.commit()
    started = []
    monkeypatch.setattr(app, "start_onboarding", lambda u: started.append(u.id))
    r = client.post("/activate-sms", data={"phone": user.phone})
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert started == []


def test_activate_sms_starts_a_never_texted_user(db, client, monkeypatch):
    import app
    user = make_user(db, onboarding_step=0)
    started = []
    monkeypatch.setattr(app, "start_onboarding", lambda u: started.append(u.id))
    r = client.post("/activate-sms", data={"phone": user.phone})
    assert r.status_code == 200 and started == [user.id]
