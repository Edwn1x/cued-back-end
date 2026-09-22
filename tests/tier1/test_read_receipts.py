"""
"Read 11:04" (founder, 2026-09-11 — "read receipts only for now"). The receipt lands
the moment the inbound is logged — then the dots, then the reply (founder 2026-09-22:
"read receipt as soon as they send their message followed by the typing animation") —
and is re-asserted when generation begins. iMessage only; fire-and-forget; never raises.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from tests import _sync
from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "READ_RECEIPTS_ENABLED", True)
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", True)
    monkeypatch.setattr(config, "IMESSAGE_REACTIONS_ENABLED", True)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)


@pytest.fixture
def sidecar(monkeypatch):
    """Records every sidecar route call in order; threads synchronous."""
    import requests, read_receipts, typing_indicator
    calls: list = []

    class _R:
        status_code = 200
        text = '{"ok": true}'
        def json(self):
            return {"ok": True, "provider_message_id": "spc-x"}

    def _post(url, json=None, headers=None, timeout=None):
        calls.append((url.rsplit("/", 1)[-1], json))
        return _R()
    monkeypatch.setattr(requests, "post", _post)
    shim = _sync.make_threading_shim(Thread=_sync.SyncThread)
    monkeypatch.setattr(read_receipts, "threading", shim)
    monkeypatch.setattr(typing_indicator, "threading", shim)
    return calls


def _inbound(db, user, body, sid):
    from models import get_session, Message
    s = get_session()
    try:
        m = Message(user_id=user.id, direction="in", body=body, message_type="freeform", channel="imessage",
                    provider_sid=sid, delivery_status="delivered",
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None))
        s.add(m); s.commit(); return m.id
    finally:
        s.close()


def test_mark_read_posts_their_latest_imessage_for_imessage_users_only(db, imessage_on, sidecar, monkeypatch):
    import config
    from read_receipts import mark_read
    user = make_user(db, preferred_channel="imessage")
    _inbound(db, user, "hey", "spc-old")
    _inbound(db, user, "why tho?", "spc-latest")  # a question still gets READ (it's not a tapback)
    assert mark_read(user.id) is True
    assert sidecar == [("read", {"phone": user.phone, "message_id": "spc-latest"})]

    sidecar.clear()
    assert mark_read(make_user(db, preferred_channel="sms").id) is False
    assert mark_read(make_user(db, preferred_channel="imessage", channel_failed_over=True).id) is False
    no_inbound = make_user(db, preferred_channel="imessage")
    assert mark_read(no_inbound.id) is False
    monkeypatch.setattr(config, "READ_RECEIPTS_ENABLED", False)
    assert mark_read(user.id) is False
    assert sidecar == []


def test_mark_read_never_raises(db, imessage_on, monkeypatch, caplog):
    import read_receipts
    user = make_user(db, preferred_channel="imessage")
    _inbound(db, user, "hey", "spc-1")
    def _boom(*a, **k):
        raise ConnectionError("sidecar down")
    monkeypatch.setattr(read_receipts.requests, "post", _boom)
    with caplog.at_level(logging.INFO):
        assert read_receipts.mark_read(user.id, wait=True) is False
    assert any("READ_RECEIPT" in r.getMessage() and "ok=False" in r.getMessage() for r in caplog.records)


def test_read_lands_before_typing_when_generation_begins(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """Flush re-asserts both (a cold sidecar can drop the arrival receipt; a long
    buffer can outlive the client's indicator) in the same order: read, typing, send."""
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "what should i eat", "spc-q1")
    anthropic_stub.reply_with(lambda kw: "protein first, then whatever's easy")
    app.process_buffered_message(user.id, "what should i eat", "freeform")
    routes = [r for r, _ in sidecar]
    assert routes[:2] == ["read", "typing"], routes
    assert sidecar[0][1] == {"phone": user.phone, "message_id": "spc-q1"}
    assert sidecar[1][1]["state"] == "start"
    assert "send" in routes


def test_suppressed_ack_gets_read_then_thumbs_up(db, imessage_on, sidecar, client, anthropic_stub, sms_capture):
    import json
    from sms import _log_message
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _log_message(user.id, "go study.", "freeform", channel="imessage", provider_sid="spc-prior", delivery_status="sent")
    r = client.post("/internal/inbound", data=json.dumps({
        "phone": user.phone, "text": "Ok", "provider_message_id": "spc-msg-ok", "chat_guid": "g", "service": "iMessage",
        "line_phone": "+16282649335", "timestamp": "2026-09-11T20:38:29Z", "attachments": []}),
        headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 200
    routes = [(r_, j) for r_, j in sidecar if r_ in ("read", "react")]
    assert routes == [("read", {"phone": user.phone, "message_id": "spc-msg-ok"}),
                      ("react", {"phone": user.phone, "message_id": "spc-msg-ok", "emoji": "like"})]
    assert sms_capture == []


def test_sms_user_gets_no_receipt_on_the_same_paths(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    import app
    user = make_user(db, preferred_channel="sms", onboarding_step=3)
    anthropic_stub.reply_with(lambda kw: "hey")
    app.process_buffered_message(user.id, "what should i eat", "freeform")
    assert [r for r, _ in sidecar if r == "read"] == []
    assert sms_capture


def _post_text(client, user, text, sid):
    import json
    return client.post("/internal/inbound", data=json.dumps({
        "phone": user.phone, "text": text, "provider_message_id": sid, "chat_guid": "g", "service": "iMessage",
        "line_phone": "+16282649335", "timestamp": "2026-09-22T20:38:29Z", "attachments": []}),
        headers={"X-Internal-Secret": SECRET}, content_type="application/json")


def test_read_then_dots_go_up_on_arrival_before_the_buffer_flushes(db, imessage_on, sidecar, client, monkeypatch, sms_capture):
    """Founder 2026-09-22: onboarding felt slow because "Read" and the dots waited out
    the buffer. Now the sequence on ARRIVAL is read → typing start, and the buffer is
    only armed after — nothing sent yet."""
    import app
    armed = []
    monkeypatch.setattr(app, "buffer_message", lambda **kw: armed.append(kw))
    user = make_user(db, preferred_channel="imessage", onboarding_step=1)
    assert _post_text(client, user, "175", "spc-onb-1").status_code == 200
    routes = [(r, j) for r, j in sidecar]
    assert routes == [("read", {"phone": user.phone, "message_id": "spc-onb-1"}),
                      ("typing", {"phone": user.phone, "state": "start"})], routes
    assert sms_capture == []
    assert len(armed) == 1 and armed[0]["delay_override"] == (5, 8)   # onboarding: one short answer at a time


def test_onboarding_buffer_is_seconds_not_half_a_minute(db, imessage_on, sidecar, client, monkeypatch):
    """Post-onboarding bands are untouched: a fresh thread still waits out double-texts."""
    import app
    armed = []
    monkeypatch.setattr(app, "buffer_message", lambda **kw: armed.append(kw))
    onboarding = make_user(db, preferred_channel="imessage", onboarding_step=2)
    done = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _post_text(client, onboarding, "chicken and rice mostly", "spc-onb-2")
    _post_text(client, done, "what should i eat", "spc-done-1")
    assert [a["delay_override"] for a in armed] == [(5, 8), (90, 150)]


def test_sms_inbound_gets_no_receipt_or_dots_on_arrival(db, imessage_on, sidecar, client, monkeypatch):
    import app
    monkeypatch.setattr(app, "buffer_message", lambda **kw: None)
    user = make_user(db, preferred_channel="sms", onboarding_step=1)
    r = client.post("/webhook", data={"From": user.phone, "Body": "175", "MessageSid": "SM-onb-1", "NumMedia": "0"})
    assert r.status_code == 200
    assert [route for route, _ in sidecar] == []
