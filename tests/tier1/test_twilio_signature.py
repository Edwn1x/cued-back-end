"""Inbound-webhook authenticity (2026-10-15 probe): unsigned POSTs to /webhook with a
forged From= must not reach the pipeline once TWILIO_SIGNATURE_MODE=enforce; shadow
mode logs the verdict and still processes (rollout evidence); off never checks."""
from __future__ import annotations

import logging

import pytest

from tests.factories import make_user

TOKEN = "unit-test-auth-token"
URL = "http://localhost/webhook"   # what Flask's test client presents as request.url


def _sig(params):
    from twilio.request_validator import RequestValidator
    return RequestValidator(TOKEN).compute_signature(URL, params)


@pytest.fixture
def armed(monkeypatch):
    import app
    got = []
    monkeypatch.setattr(app, "buffer_message", lambda **kw: got.append(kw))
    return got


@pytest.fixture
def token(monkeypatch):
    import config
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TOKEN)


def _post(client, user, headers=None, sid="SM-sig-1"):
    params = {"From": user.phone, "Body": "hey", "MessageSid": sid, "NumMedia": "0"}
    return client.post("/webhook", data=params, headers=headers or {}), params


def test_enforce_accepts_a_correctly_signed_request(db, client, armed, token, monkeypatch):
    import config
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "enforce")
    user = make_user(db, preferred_channel="sms")
    params = {"From": user.phone, "Body": "hey", "MessageSid": "SM-sig-ok", "NumMedia": "0"}
    r = client.post("/webhook", data=params, headers={"X-Twilio-Signature": _sig(params)})
    assert r.status_code == 200
    assert len(armed) == 1 and armed[0]["phone"] == user.phone


def test_enforce_rejects_a_forged_request_before_any_processing(db, client, armed, token, monkeypatch, caplog):
    import config
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "enforce")
    user = make_user(db, preferred_channel="sms")
    with caplog.at_level(logging.INFO):
        r, _ = _post(client, user, headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403
    assert armed == []
    assert any("TWILIO_SIG ok=False mode=enforce" in rec.getMessage() for rec in caplog.records)
    # the forged From never shows in full in the log line
    assert not any(user.phone in rec.getMessage() for rec in caplog.records if "TWILIO_SIG" in rec.getMessage())


def test_enforce_rejects_a_missing_signature(db, client, armed, token, monkeypatch):
    import config
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "enforce")
    user = make_user(db, preferred_channel="sms")
    r, _ = _post(client, user)
    assert r.status_code == 403 and armed == []


def test_shadow_logs_the_mismatch_but_still_processes(db, client, armed, token, monkeypatch, caplog):
    import config
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "shadow")
    user = make_user(db, preferred_channel="sms")
    with caplog.at_level(logging.INFO):
        r, _ = _post(client, user, headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 200
    assert len(armed) == 1
    assert any("TWILIO_SIG ok=False mode=shadow" in rec.getMessage() for rec in caplog.records)


def test_shadow_logs_ok_on_a_valid_signature(db, client, armed, token, monkeypatch, caplog):
    import config
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "shadow")
    user = make_user(db, preferred_channel="sms")
    params = {"From": user.phone, "Body": "hey", "MessageSid": "SM-sig-ok2", "NumMedia": "0"}
    with caplog.at_level(logging.INFO):
        r = client.post("/webhook", data=params, headers={"X-Twilio-Signature": _sig(params)})
    assert r.status_code == 200 and len(armed) == 1
    assert any("TWILIO_SIG ok=True mode=shadow" in rec.getMessage() for rec in caplog.records)


def test_off_never_checks(db, client, armed, token, monkeypatch, caplog):
    import config
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "off")
    user = make_user(db, preferred_channel="sms")
    with caplog.at_level(logging.INFO):
        r, _ = _post(client, user, headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 200 and len(armed) == 1
    assert not any("TWILIO_SIG" in rec.getMessage() for rec in caplog.records)


def test_no_auth_token_means_skip_with_a_warning_even_in_enforce(db, client, armed, monkeypatch, caplog):
    """Enforce without a token would silently drop the whole SMS rail — so it skips and warns."""
    import config
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", None)
    monkeypatch.setattr(config, "TWILIO_SIGNATURE_MODE", "enforce")
    user = make_user(db, preferred_channel="sms")
    with caplog.at_level(logging.WARNING):
        r, _ = _post(client, user)
    assert r.status_code == 200 and len(armed) == 1
    assert any("TWILIO_SIG skipped=no_auth_token" in rec.getMessage() for rec in caplog.records)


def test_default_mode_is_shadow():
    import importlib, os
    os.environ.pop("TWILIO_SIGNATURE_MODE", None)
    import config
    importlib.reload(config)
    assert config.TWILIO_SIGNATURE_MODE == "shadow"
