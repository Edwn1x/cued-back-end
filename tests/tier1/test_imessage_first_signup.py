"""
iMessage-first signup (founder, 2026-09-14): "make 'text me on iMessage' the
default and have an optional 'don't have an iPhone' button".

With an opt-in link in hand the hook is DEFERRED at signup. Three ways it goes out:
  1. their first iMessage → hook as the reply, blue (process_buffered_message)
  2. "I don't have an iPhone" → POST /signup/channel → hook by SMS
  3. neither for ONBOARDING_HOOK_FALLBACK_MINUTES → scheduler sends it by SMS + link
Without a link there is no choice: the hook goes out immediately as before.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

SECRET = "test-internal-secret"


@pytest.fixture
def photon_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", True)
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_ID", "proj")
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_SECRET", "sec")


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


@pytest.fixture
def sidecar_ok(monkeypatch):
    import sms
    calls: list = []

    def _fake(phone, body, reply_to=None):
        calls.append((phone, body))
        return f"photon-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
    return calls


def _signup(client, phone, name="Sis"):
    body = {"name": name, "age": 22, "gender": "female", "goal": ["fat_loss"], "experience": "beginner",
            "equipment": "full_gym", "phone": phone, "sms_consent": True}
    return client.post("/signup", data=json.dumps(body), content_type="application/json").get_json()


def _outbound(db, user_id):
    from models import Message
    db.expire_all()
    return db.query(Message).filter(Message.user_id == user_id, Message.direction == "out").order_by(Message.id).all()


def _post_imessage(client, phone, text, msg_id):
    payload = {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
               "service": "iMessage", "line_phone": "+16287895365", "timestamp": "2026-09-14T05:27:00.000Z",
               "attachments": []}
    return client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET},
                       content_type="application/json")


def test_signup_with_a_link_defers_the_hook(db, client, photon_on, monkeypatch, sms_capture, caplog):
    import photon
    from models import User
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "ph-1")
    with caplog.at_level(logging.INFO):
        data = _signup(client, "5105550201")
    assert data["status"] == "ok" and data["imessage_link"] and data["hook_deferred"] is True
    u = db.query(User).filter(User.phone == "+15105550201").one()
    assert (u.onboarding_step or 0) == 0
    assert _outbound(db, u.id) == [] and sms_capture == [], "hook must NOT go out yet"
    assert any("ONBOARDING_HOOK_DEFERRED" in r.getMessage() for r in caplog.records)


def test_signup_without_a_link_sends_the_hook_immediately(db, client, photon_on, monkeypatch, sms_capture):
    import photon
    from models import User

    import app as appmod
    from onboarding_agent import send_onboarding_hook

    def _cap(phone, name=None):
        raise photon.PhotonError("402 user cap")
    monkeypatch.setattr(photon, "add_user", _cap)
    # start_onboarding is a real daemon thread; run its body inline for determinism
    monkeypatch.setattr(appmod, "start_onboarding", lambda user: send_onboarding_hook(user.id))
    data = _signup(client, "5105550202")
    assert data["hook_deferred"] is False and data["imessage_link"] is None
    u = db.query(User).filter(User.phone == "+15105550202").one()
    assert len(_outbound(db, u.id)) == 1 and (u.onboarding_step or 0) == 1
    assert sms_capture and "redirect" not in sms_capture[-1][1]


def test_no_iphone_button_puts_them_on_sms_and_sends_the_hook_once(db, client, imessage_on, sms_capture):
    from models import User
    user = make_user(db, phone="+15105550203", onboarding_step=0, preferred_channel="imessage", photon_user_id="ph-3")
    r = client.post("/signup/channel", data=json.dumps({"phone": "5105550203", "channel": "sms"}),
                    content_type="application/json")
    assert r.status_code == 200 and r.get_json()["hook_sent"] is True
    db.expire_all()
    u = db.get(User, user.id)
    assert u.preferred_channel == "sms" and u.onboarding_step == 1 and u.photon_user_id == "ph-3"
    rows = _outbound(db, u.id)
    assert [(m.channel, m.delivery_status) for m in rows] == [("sms", "sent")], "went by SMS, no iMessage attempt"
    assert "redirect" not in rows[0].body

    # idempotent: a second tap flips nothing and sends nothing
    r2 = client.post("/signup/channel", data=json.dumps({"phone": "+15105550203", "channel": "sms"}),
                     content_type="application/json")
    assert r2.get_json()["hook_sent"] is False and len(_outbound(db, u.id)) == 1


def test_signup_channel_rejects_bad_input(db, client):
    r = client.post("/signup/channel", data=json.dumps({"phone": "5105550299", "channel": "sms"}),
                    content_type="application/json")
    assert r.status_code == 404
    r = client.post("/signup/channel", data=json.dumps({"phone": "5105550299", "channel": "imessage"}),
                    content_type="application/json")
    assert r.status_code == 400


def test_first_imessage_triggers_the_hook_blue_and_skips_the_model(db, client, imessage_on, sidecar_ok, anthropic_stub, sms_capture):
    from models import User
    from tests._sync import PENDING_TIMERS
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("model must not run on the opt-in text")))
    user = make_user(db, phone="+15105550204", onboarding_step=0, preferred_channel="imessage", photon_user_id="ph-4")

    assert _post_imessage(client, user.phone, "hey cued", "spc-optin-1").status_code == 200
    t = PENDING_TIMERS.pop(user.phone, None)
    assert t is not None
    t.fire()

    db.expire_all()
    u = db.get(User, user.id)
    assert u.onboarding_step == 1 and u.channel_failed_over is False
    assert len(sidecar_ok) == 1 and sms_capture == [], "hook went blue, nothing green"
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type) for m in rows] == [("imessage", "onboarding")]


def test_fallback_job_sends_by_sms_with_the_link_after_the_wait(db, imessage_on, monkeypatch, sms_capture, caplog):
    import config, sms
    from models import User, get_session
    from onboarding_agent import send_fallback_hooks
    monkeypatch.setattr(config, "ONBOARDING_HOOK_FALLBACK_MINUTES", 10)

    def _gate(phone, body, reply_to=None):
        raise RuntimeError("Target not allowed for this project")
    monkeypatch.setattr(sms, "_send_imessage", _gate)

    old = make_user(db, phone="+15105550205", onboarding_step=0, preferred_channel="imessage", photon_user_id="ph-5")
    fresh = make_user(db, phone="+15105550206", onboarding_step=0, preferred_channel="imessage", photon_user_id="ph-6")
    chose_sms = make_user(db, phone="+15105550207", onboarding_step=1, preferred_channel="sms", photon_user_id="ph-7")
    s = get_session()
    try:  # naive UTC like prod writes
        s.get(User, old.id).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=11)
        s.get(User, fresh.id).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
        s.commit()
    finally:
        s.close()

    with caplog.at_level(logging.INFO):
        assert send_fallback_hooks() == 1
    db.expire_all()
    assert db.get(User, old.id).onboarding_step == 1
    assert db.get(User, fresh.id).onboarding_step == 0 and _outbound(db, fresh.id) == []
    assert _outbound(db, chose_sms.id) == []
    rows = _outbound(db, old.id)
    assert [(m.channel, m.delivery_status) for m in rows] == [("imessage", "failed"), ("sms", "sent")]
    assert "/users/ph-5/redirect" in rows[1].body, "the SMS carries the opt-in link"
    assert any("ONBOARDING_HOOK_FALLBACK user" in r.getMessage() for r in caplog.records)

    assert send_fallback_hooks() == 0, "picked up once, never again"
