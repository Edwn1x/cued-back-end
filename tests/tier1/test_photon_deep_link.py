"""
Photon shared-pool consent gate → the one-tap iMessage opt-in link.

Live 2026-09-12 (user 28): a freshly provisioned shared user can't be messaged
until THEY text their assigned line ("Target not allowed for this project"), so
the hook failed over to SMS and every blue send since has been refused. Fix:
/signup provisions synchronously and returns `imessage_link` (the site shows a
button); the SMS hook carries the same link when the gate refuses it; the gate
is logged as "not opted in", not as a dead pipe.
"""

from __future__ import annotations

import json
import logging

import pytest

from tests.factories import make_user


@pytest.fixture
def photon_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", True)
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_ID", "proj")
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_SECRET", "sec")
    monkeypatch.setattr(config, "SPECTRUM_API_URL", "https://spectrum.photon.codes")


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")


def _signup(client, phone="5105550123", name="Sis"):
    body = {"name": name, "age": 22, "gender": "female", "goal": ["fat_loss"], "experience": "beginner",
            "equipment": "full_gym", "phone": phone, "sms_consent": True}
    r = client.post("/signup", data=json.dumps(body), content_type="application/json")
    return r.get_json()


def test_link_builder_shape():
    from photon import imessage_link
    assert imessage_link(None) is None
    link = imessage_link("10ef9202-abcd", msg="hey cued")
    assert link == "https://spectrum.photon.codes/users/10ef9202-abcd/redirect?msg=hey%20cued"


def test_signup_provisions_synchronously_and_returns_the_link(db, client, photon_on, monkeypatch):
    import app as appmod, photon
    from models import User
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "ph-new-1")
    monkeypatch.setattr(appmod, "start_onboarding", lambda user: None)

    data = _signup(client)
    assert data["status"] == "ok"
    assert data["imessage_link"] == "https://spectrum.photon.codes/users/ph-new-1/redirect?msg=hey%20cued"
    u = db.query(User).filter(User.phone == "+15105550123").one()
    assert u.photon_user_id == "ph-new-1" and u.preferred_channel == "imessage"


def test_signup_without_photon_still_succeeds_with_no_link(db, client, photon_on, monkeypatch):
    import app as appmod, photon
    from models import User

    def _cap(phone, name=None):
        raise photon.PhotonError("photon users API 402: free tier user cap")
    monkeypatch.setattr(photon, "add_user", _cap)
    monkeypatch.setattr(appmod, "start_onboarding", lambda user: None)

    data = _signup(client, phone="5105550124")
    assert data["status"] == "ok" and data["imessage_link"] is None
    u = db.query(User).filter(User.phone == "+15105550124").one()
    assert u.photon_user_id is None and (u.preferred_channel or "sms") == "sms"


def test_signup_flag_off_returns_no_link(db, client, monkeypatch):
    import app as appmod, config
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    monkeypatch.setattr(appmod, "start_onboarding", lambda user: None)
    data = _signup(client, phone="5105550125")
    assert data["status"] == "ok" and data["imessage_link"] is None


def test_returning_user_gets_their_link_on_the_exists_path(db, client, photon_on):
    user = make_user(db, phone="+15105550126", photon_user_id="ph-old-9", preferred_channel="imessage")
    data = _signup(client, phone="5105550126", name=user.name)
    assert data["status"] == "exists"
    assert data["imessage_link"].endswith("/users/ph-old-9/redirect?msg=hey%20cued")


def test_consent_gate_is_logged_as_not_opted_in_and_the_hook_carries_the_link(
        db, imessage_on, monkeypatch, sms_capture, caplog):
    import sms
    from models import User

    def _gate(phone, body):
        raise RuntimeError("sidecar /send 502: Target not allowed for this project")
    monkeypatch.setattr(sms, "_send_imessage", _gate)
    user = make_user(db, preferred_channel="imessage", photon_user_id="ph-sis")

    with caplog.at_level(logging.INFO, logger="cued.sms"):
        sms.send_sms(user.phone, "hey it's cued — quick q to get us going", user_id=user.id,
                     message_type="onboarding")

    assert any("IMESSAGE_NOT_OPTED_IN" in r.getMessage() for r in caplog.records)
    assert not any("IMESSAGE_SEND_FAILED" in r.getMessage() for r in caplog.records)
    phone, body = sms_capture[-1]
    assert "/users/ph-sis/redirect?msg=hey%20cued" in body, body
    assert body.startswith("hey it's cued")
    db.expire_all()
    assert db.get(User, user.id).channel_failed_over is True  # still trips; inbound resets it


def test_consent_gate_on_a_non_hook_message_does_not_append_the_link(db, imessage_on, monkeypatch, sms_capture):
    import sms

    def _gate(phone, body):
        raise RuntimeError("Target not allowed for this project")
    monkeypatch.setattr(sms, "_send_imessage", _gate)
    user = make_user(db, preferred_channel="imessage", photon_user_id="ph-sis")
    sms.send_sms(user.phone, "how'd the run go?", user_id=user.id, message_type="heartbeat")
    assert "redirect" not in sms_capture[-1][1]


def test_other_sidecar_failures_still_log_send_failed(db, imessage_on, monkeypatch, sms_capture, caplog):
    import sms

    def _dead(phone, body):
        raise RuntimeError("sidecar /send 503: upstream unavailable")
    monkeypatch.setattr(sms, "_send_imessage", _dead)
    user = make_user(db, preferred_channel="imessage", photon_user_id="ph-sis")
    with caplog.at_level(logging.INFO, logger="cued.sms"):
        sms.send_sms(user.phone, "hook text", user_id=user.id, message_type="onboarding")
    assert any("IMESSAGE_SEND_FAILED" in r.getMessage() for r in caplog.records)
    assert "redirect" not in sms_capture[-1][1]
