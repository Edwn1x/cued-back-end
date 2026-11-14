"""Inbound kinds initiative, step 0 — Flask-side guardrails. No behaviour change;
these lock in what /internal/inbound does TODAY so the sidecar can start forwarding
new content kinds (effect, richlink, edit, unsend, voice, poll_option) one PR at a
time without a surprise on the Flask side.

Two facts the later steps lean on:
  1. Unknown top-level payload keys are ignored → the sidecar may add fields
     (`links`, `edit_of`, `kinds`, `poll`) before Flask reads them.
  2. A non-image attachment (an audio/* voice memo) is stored as "[image attached]"
     and the model turn runs text-only. That wording is WRONG for a voice memo —
     step 5a replaces it with a voice-memo marker. This test documents the bug so
     5a's red→green is visible.
"""
from __future__ import annotations

import io
import json

import pytest

from tests.factories import make_user

SECRET = "testsecret"


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.local")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)


def _payload(phone, text, **extra):
    return {"phone": phone, "text": text, "provider_message_id": f"photon-in-{abs(hash(text)) % 10**6}",
            "chat_guid": f"any;-;{phone}", "service": "iMessage", "line_phone": "+15102646604",
            "timestamp": "2026-11-14T12:00:00.000Z", "attachments": [], **extra}


def _post_json(client, payload):
    return client.post("/internal/inbound", json=payload, headers={"X-Internal-Secret": SECRET})


def _post_files(client, payload, files):
    data = {"payload": json.dumps(payload)}
    for i, (name, mime, blob) in enumerate(files):
        data[f"attachment_{i}"] = (io.BytesIO(blob), name, mime)
    return client.post("/internal/inbound", data=data, headers={"X-Internal-Secret": SECRET},
                       content_type="multipart/form-data")


def _stored_in(db, user):
    from models import get_session, Message
    s = get_session()
    try:
        return [m.body for m in s.query(Message).filter(Message.user_id == user.id, Message.direction == "in")
                .order_by(Message.id).all()]
    finally:
        s.close()


def _model_turns(store):
    def handler(kw):
        store.append(kw.get("messages") or [])
        return "ok"
    return handler


def test_unknown_payload_keys_are_ignored(db, client, imessage_on, driver, anthropic_stub):
    """Forward-compat: fields the later steps add ride along harmlessly until Flask reads them."""
    user = make_user(db)
    seen = []
    anthropic_stub.reply_with(_model_turns(seen))
    r = _post_json(client, _payload(user.phone, "pasted a link", links=["https://example.com/recipe"],
                                    kinds=["richlink"], edit_of=None, poll=None, future_field={"x": 1}))
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True}
    driver.flush(user.phone)
    assert _stored_in(db, user) == ["pasted a link"]
    assert seen, "the reply turn ran on the text alone"


def test_audio_attachment_today_is_stored_as_a_picture_marker(db, client, imessage_on, driver, anthropic_stub):
    """DOCUMENTS A BUG step 5a fixes. An audio/* attachment can't normalize to an image,
    so image_data is None (text-only turn, no image blocks) — correct — but the stored
    row says "[image attached]", so the coach would talk about a picture that was a
    voice memo. Step 5a changes this assertion to a voice-memo marker."""
    from sms import IMAGE_MARKER
    user = make_user(db)
    seen = []
    anthropic_stub.reply_with(_model_turns(seen))
    r = _post_files(client, _payload(user.phone, ""), [("Audio Message.caf", "audio/x-caf", b"caff\x00\x01" * 8)])
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True}
    driver.flush(user.phone)
    assert _stored_in(db, user) == [IMAGE_MARKER]
    first = seen[0][0]["content"] if seen and seen[0] else None
    if isinstance(first, list):
        assert not any(isinstance(b, dict) and b.get("type") == "image" for b in first), "audio never reaches vision"


def test_typing_and_read_never_reach_flask_as_text(db, client, imessage_on, driver, anthropic_stub):
    """If the sidecar ever forwarded a presence event with empty text, Flask drops it
    before any model turn: the `ignored: empty` path. Locks the floor the sidecar relies on."""
    user = make_user(db)
    seen = []
    anthropic_stub.reply_with(_model_turns(seen))
    r = _post_json(client, _payload(user.phone, "", kinds=["typing"]))
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True, "ignored": "empty"}
    driver.flush(user.phone)
    assert _stored_in(db, user) == [] and seen == []
