"""Rate-limiter keys behind Railway's edge (2026-10-16, after the launch-night probe).

Live: ten identical bad POSTs to /signup from one laptop → 400×5, 429×2, 400×3, the
429s landing on whichever Railway edge hop had already passed three. The limiter was
keying on the hop. Now: key = X-Real-IP (Railway overwrites it with the client IP),
else the rightmost X-Forwarded-For entry, else the peer — never a client-written left
entry; the sign-up routes are also limited per PHONE and share one global ceiling;
every 429 logs the resolved key and the raw forwarding headers."""
from __future__ import annotations

import json
import logging

import pytest

import config


@pytest.fixture
def limiter_on(_appmod):
    """conftest disables the limiter for the whole session; turn it on with a clean
    store for these tests only."""
    _appmod.limiter.enabled = True
    _appmod.limiter.reset()
    yield _appmod.limiter
    _appmod.limiter.reset()
    _appmod.limiter.enabled = False


@pytest.fixture
def no_provision(monkeypatch):
    import photon
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)


def _post_signup(client, phone, *, real_ip=None, xff=None, peer="10.0.0.1", name="Sis"):
    headers = {}
    if real_ip is not None:
        headers["X-Real-IP"] = real_ip
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    body = {"name": name, "phone": phone, "sms_consent": True, "goal": ["fat_loss"],
            "experience": "beginner", "equipment": "full_gym", "age": 22, "gender": "female"}
    return client.post("/signup", data=json.dumps(body), content_type="application/json",
                       headers=headers, environ_base={"REMOTE_ADDR": peer})


# ── key resolution ───────────────────────────────────────────────────────────

def test_x_real_ip_wins_then_rightmost_xff_then_peer(_appmod):
    from request_ip import client_ip
    app = _appmod.app
    with app.test_request_context(headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "1.1.1.1, 9.9.9.9"},
                                  environ_base={"REMOTE_ADDR": "152.233.76.9"}):
        assert client_ip() == "203.0.113.9"
    with app.test_request_context(headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.9"},
                                  environ_base={"REMOTE_ADDR": "152.233.76.9"}):
        assert client_ip() == "203.0.113.9"          # rightmost = the one the edge appended
    with app.test_request_context(headers={"X-Forwarded-For": "1.1.1.1, not-an-ip"},
                                  environ_base={"REMOTE_ADDR": "152.233.76.9"}):
        assert client_ip() == "1.1.1.1"              # garbage on the right is skipped, not trusted
    with app.test_request_context(environ_base={"REMOTE_ADDR": "152.233.76.9"}):
        assert client_ip() == "152.233.76.9"
    with app.test_request_context(headers={"X-Real-IP": "nope"}, environ_base={"REMOTE_ADDR": "152.233.76.9"}):
        assert client_ip() == "152.233.76.9"         # an invalid X-Real-IP never becomes the key


def test_phone_key_reads_json_or_form_and_falls_back_to_ip(_appmod):
    from request_ip import phone_key
    app = _appmod.app
    with app.test_request_context(json={"phone": "+1 (510) 555-0100"}, environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert phone_key() == "phone:5105550100"
    with app.test_request_context(data={"phone": "15105550100"}, environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert phone_key() == "phone:5105550100"     # +1 / 1 / bare collide on purpose
    with app.test_request_context(json={"phone": "abc"}, headers={"X-Real-IP": "203.0.113.9"},
                                  environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert phone_key() == "ip:203.0.113.9"


# ── the limits themselves ────────────────────────────────────────────────────

def test_per_ip_limit_follows_x_real_ip_not_the_edge_peer(db, client, limiter_on):
    # Same client (X-Real-IP), every request through a DIFFERENT edge hop — the live
    # failure shape. 3/minute → the 4th is a 429 regardless of the hop.
    codes = [_post_signup(client, "abc", real_ip="203.0.113.9", peer=f"152.233.76.{i}").status_code
             for i in (9, 10, 11, 12)]
    assert codes == [400, 400, 400, 429]
    # A different client through the SAME hop as the blocked one is untouched.
    r = _post_signup(client, "abc", real_ip="203.0.113.10", peer="152.233.76.12")
    assert r.status_code == 400
    assert r.get_json()["message"] == "Please enter a 10-digit US phone number."


def test_429_body_shape_and_log_line_name_the_key(db, client, limiter_on, caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            _post_signup(client, "abc", real_ip="203.0.113.9")
        r = _post_signup(client, "abc", real_ip="203.0.113.9", xff="1.1.1.1, 152.233.76.9", peer="152.233.76.9")
    assert r.status_code == 429
    assert r.get_json() == {"status": "error", "message": "Too many submissions. Try again in a minute."}
    line = next(rec.getMessage() for rec in caplog.records if "RATE_LIMITED" in rec.getMessage())
    assert "endpoint=signup_submit" in line and "'key': '203.0.113.9'" in line
    assert "'X-Forwarded-For': '1.1.1.1, 152.233.76.9'" in line and "'peer': '152.233.76.9'" in line


def test_same_phone_from_rotating_ips_is_capped_per_phone(db, client, limiter_on, no_provision, sms_capture):
    # Rotating IPs used to be a free pass: each new IP got a fresh bucket for the same
    # victim number. Now the phone itself is a key (3/hour per route).
    codes = [_post_signup(client, "5105550100", real_ip=f"203.0.113.{i}").status_code for i in range(1, 5)]
    assert codes == [200, 200, 200, 429]            # created, exists, exists, capped
    # Another number from a fresh IP is unaffected.
    assert _post_signup(client, "5105550101", real_ip="203.0.113.50").status_code == 200


def test_global_ceiling_is_shared_across_the_text_minting_routes(db, client, limiter_on, monkeypatch):
    # Distinct phones AND distinct IPs — only the shared ceiling can stop this. Read
    # per request, so an env change needs no code change.
    monkeypatch.setattr(config, "SIGNUP_GLOBAL_LIMIT", "2/minute")
    assert _post_signup(client, "abc", real_ip="203.0.113.1").status_code == 400
    r = client.post("/activate-sms", data={"phone": "abc"}, headers={"X-Real-IP": "203.0.113.2"},
                    environ_base={"REMOTE_ADDR": "10.0.0.2"})
    assert r.status_code == 400
    r = client.post("/waitlist", data=json.dumps({"name": "Sis", "phone": "abc", "sms_consent": True}),
                    content_type="application/json", headers={"X-Real-IP": "203.0.113.3"},
                    environ_base={"REMOTE_ADDR": "10.0.0.3"})
    assert r.status_code == 429
    # /signup/channel is phone-keyed but NOT under the shared ceiling (it never mints a row).
    r = client.post("/signup/channel", data=json.dumps({"phone": "5105550199", "channel": "sms"}),
                    content_type="application/json", headers={"X-Real-IP": "203.0.113.4"},
                    environ_base={"REMOTE_ADDR": "10.0.0.4"})
    assert r.status_code == 404


def test_limits_are_inert_when_the_limiter_is_off(db, client):
    # The session default (conftest): no limiter, so the rest of the suite is unaffected.
    codes = [_post_signup(client, "abc", real_ip="203.0.113.9").status_code for _ in range(6)]
    assert codes == [400] * 6
