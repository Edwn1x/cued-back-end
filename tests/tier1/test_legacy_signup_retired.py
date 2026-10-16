"""2026-10-15 retirement: the backend-hosted sign-up surface is gone. GET /signup sends
old links to the site; POST /signup and /activate-sms answer 410 (unless the rollback flag
LEGACY_SIGNUP_ENABLED is on); /signup/channel (still used by the live success screen) is
strict on the phone; /admin basic-auth throttles brute force per IP."""
from __future__ import annotations

import base64
import json

import pytest

from tests.factories import make_user


def _signup(client, phone="5105550123", name="Sis"):
    body = {"name": name, "age": 22, "gender": "female", "goal": ["fat_loss"], "experience": "beginner",
            "equipment": "full_gym", "phone": phone, "sms_consent": True}
    return client.post("/signup", data=json.dumps(body), content_type="application/json")


def _users(db):
    from models import User
    db.expire_all()
    return db.query(User).count()


def test_get_signup_redirects_to_the_site(client):
    r = client.get("/signup")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://cued.fit")


def test_post_signup_is_gone_by_default(db, client, sms_capture, monkeypatch):
    import config
    monkeypatch.setattr(config, "LEGACY_SIGNUP_ENABLED", False)
    before = _users(db)
    r = _signup(client)
    assert r.status_code == 410 and r.get_json()["status"] == "error"
    assert _users(db) == before and sms_capture == []
    # form-encoded too
    r = client.post("/signup", data={"name": "x", "phone": "5105550123", "sms_consent": "on"})
    assert r.status_code == 410


def test_activate_sms_is_gone_by_default(db, client, sms_capture, monkeypatch):
    import config
    monkeypatch.setattr(config, "LEGACY_SIGNUP_ENABLED", False)
    user = make_user(db, onboarding_step=0)
    r = client.post("/activate-sms", data={"phone": user.phone})
    assert r.status_code == 410 and sms_capture == []


def test_rollback_flag_restores_the_locked_down_behaviour(db, client, monkeypatch):
    import config, photon
    monkeypatch.setattr(config, "LEGACY_SIGNUP_ENABLED", True)
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)
    r = _signup(client, phone="5105550177")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert _signup(client, phone="+1'OR'1'='1").status_code == 400     # #235 lockdown still applies


def test_signup_channel_rejects_a_probe_phone(db, client):
    r = client.post("/signup/channel", data=json.dumps({"phone": "+1'OR'1'='1", "channel": "sms"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_signup_channel_still_flips_a_real_user(db, client, monkeypatch):
    import onboarding_agent
    monkeypatch.setattr(onboarding_agent, "send_onboarding_hook", lambda uid, reason=None: True)
    user = make_user(db, onboarding_step=0, preferred_channel="imessage")
    r = client.post("/signup/channel", data=json.dumps({"phone": user.phone, "channel": "sms"}),
                    content_type="application/json")
    assert r.status_code == 200 and r.get_json()["channel"] == "sms"


# ─── /admin brute-force throttle ────────────────────────────────────────────

def _basic(pw):
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


@pytest.fixture
def admin_pw(monkeypatch):
    import app, config
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "right-password")
    monkeypatch.setattr(config, "ADMIN_AUTH_MAX_FAILS", 5)
    monkeypatch.setattr(config, "ADMIN_AUTH_FAIL_WINDOW_S", 600)
    app._ADMIN_FAILS.clear()
    yield
    app._ADMIN_FAILS.clear()


def test_admin_throttles_after_max_failed_guesses_even_for_the_right_password(client, admin_pw):
    ip = {"REMOTE_ADDR": "203.0.113.9"}
    for _ in range(5):
        assert client.get("/admin", headers=_basic("wrong"), environ_base=ip).status_code == 401
    assert client.get("/admin", headers=_basic("wrong"), environ_base=ip).status_code == 429
    assert client.get("/admin", headers=_basic("right-password"), environ_base=ip).status_code == 429


def test_admin_throttle_is_per_ip(client, admin_pw):
    bad = {"REMOTE_ADDR": "203.0.113.9"}
    good = {"REMOTE_ADDR": "198.51.100.4"}
    for _ in range(5):
        client.get("/admin", headers=_basic("wrong"), environ_base=bad)
    assert client.get("/admin", headers=_basic("wrong"), environ_base=bad).status_code == 429
    assert client.get("/admin", headers=_basic("right-password"), environ_base=good).status_code == 200


def test_admin_unauthenticated_first_hits_do_not_count_as_guesses(client, admin_pw):
    ip = {"REMOTE_ADDR": "203.0.113.10"}
    for _ in range(20):
        assert client.get("/admin", environ_base=ip).status_code == 401     # browser prompt, no header
    assert client.get("/admin", headers=_basic("right-password"), environ_base=ip).status_code == 200


def test_admin_throttle_window_drains(client, admin_pw, monkeypatch):
    import app
    ip = {"REMOTE_ADDR": "203.0.113.11"}
    for _ in range(5):
        client.get("/admin", headers=_basic("wrong"), environ_base=ip)
    assert client.get("/admin", headers=_basic("right-password"), environ_base=ip).status_code == 429
    # age every recorded failure past the window
    for q in app._ADMIN_FAILS.values():
        for i in range(len(q)):
            q[i] -= 10_000
    assert client.get("/admin", headers=_basic("right-password"), environ_base=ip).status_code == 200
