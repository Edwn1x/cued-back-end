"""Inbound kinds initiative, step 0 — Flask-side guardrails. No behaviour change;
these lock in what /internal/inbound does TODAY so the sidecar can start forwarding
new content kinds (effect, richlink, edit, unsend, voice, poll_option) one PR at a
time without a surprise on the Flask side.

Two facts the later steps lean on:
  1. Unknown top-level payload keys are ignored → the sidecar may add fields
     (`links`, `edit_of`, `kinds`, `poll`) before Flask reads them.
  2. (step 0 → 5a) A non-image attachment (an audio/* voice memo) used to be stored
     as "[image attached]" with a text-only model turn — wrong wording for a voice
     memo. Step 5a (below) stores "[voice memo attached]" instead and the model turn
     carries a code note: can't listen yet, say so, ask them to type it.
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


def _first_user_text(seen):
    """The text the model saw on the first turn (string content, or the text block of a list)."""
    first = seen[0][0]["content"] if seen and seen[0] else None
    if isinstance(first, list):
        assert not any(isinstance(b, dict) and b.get("type") == "image" for b in first), "audio never reaches vision"
        return " ".join(b.get("text", "") for b in first if isinstance(b, dict))
    return first or ""


def test_voice_memo_is_stored_as_a_voice_marker_and_the_turn_says_so(db, client, imessage_on, driver, anthropic_stub):
    """Step 5a. The sidecar (INBOUND_EXTRA_KINDS=voice) forwards an iMessage audio
    message as attachment_0 with an audio/* mime + kinds:["voice"]. Flask: the stored
    row says a voice memo came — never "[image attached]" — the bytes never go near
    Pillow or vision, and the model turn carries the honesty note (can't listen yet,
    ask them to type it) so the coach neither ignores the memo nor guesses at it."""
    from sms import IMAGE_MARKER, VOICE_MARKER
    user = make_user(db)
    seen = []
    anthropic_stub.reply_with(_model_turns(seen))
    r = _post_files(client, _payload(user.phone, "", attachments=[{"name": "Audio Message.caf", "mime_type": "audio/x-caf", "size": 48}],
                                     kinds=["voice"]),
                    [("Audio Message.caf", "audio/x-caf", b"caff\x00\x01" * 8)])
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True}
    driver.flush(user.phone)
    assert _stored_in(db, user) == [VOICE_MARKER]
    assert IMAGE_MARKER not in _stored_in(db, user)[0]
    text = _first_user_text(seen).lower()
    assert "voice memo" in text and "can't listen" in text, text
    assert VOICE_MARKER not in text, "the raw marker is swapped for the note, not handed to the model twice"


def test_voice_memo_with_a_caption_keeps_the_words(db, client, imessage_on, driver, anthropic_stub):
    """A memo sent with text (iMessage groups them) → the words are stored and reach the
    model, the marker rides behind them, the note follows."""
    from sms import VOICE_MARKER
    user = make_user(db)
    seen = []
    anthropic_stub.reply_with(_model_turns(seen))
    _post_files(client, _payload(user.phone, "heres how today went", kinds=["voice"]),
                [("Audio Message.caf", "audio/x-caf", b"caff\x00\x01" * 8)])
    driver.flush(user.phone)
    assert _stored_in(db, user) == [f"heres how today went {VOICE_MARKER}"]
    text = _first_user_text(seen)
    assert text.startswith("heres how today went") and "voice memo" in text.lower()


def test_voice_memo_lost_in_an_outage_is_still_a_voice_memo(db, client, imessage_on, driver, anthropic_stub):
    """The retry queue's last resort forwards text-only with attachments_unavailable + kinds.
    A lone memo has no words — before 5a an empty-text payload with no files was dropped
    as `ignored: empty`, so the memo vanished. Now: stored as a voice memo (not a picture),
    and the coach gets to say it didn't get it."""
    from sms import IMAGE_MARKER, VOICE_MARKER
    user = make_user(db)
    seen = []
    anthropic_stub.reply_with(_model_turns(seen))
    r = _post_json(client, _payload(user.phone, "", attachments_unavailable=1, kinds=["voice"]))
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True}
    driver.flush(user.phone)
    assert _stored_in(db, user) == [VOICE_MARKER]
    assert IMAGE_MARKER not in _stored_in(db, user)[0]


def test_photo_and_voice_together_store_both_markers(db, client, imessage_on, driver, anthropic_stub):
    from sms import IMAGE_MARKER, VOICE_MARKER
    user = make_user(db)
    anthropic_stub.reply_with(_model_turns([]))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    _post_files(client, _payload(user.phone, "", kinds=["voice"]),
                [("IMG_1.png", "image/png", png), ("Audio Message.caf", "audio/x-caf", b"caff" * 8)])
    driver.flush(user.phone)
    (row,) = _stored_in(db, user)
    assert IMAGE_MARKER in row and VOICE_MARKER in row


def test_voice_prompt_pins_the_voice_memo_rule():
    """The honesty section names the marker and the rule (can't hear it, ask for the words)."""
    from agent_loop import _voice_prompt
    from sms import VOICE_MARKER
    voice = _voice_prompt()
    assert VOICE_MARKER in voice
    assert "voice memo" in voice.lower()


def test_a_voice_memo_row_is_not_a_sent_photo(db):
    """capabilities._sent_a_photo keys on the image marker; a memo must not count as a photo."""
    from capabilities import _sent_a_photo
    from models import get_session, Message
    from sms import VOICE_MARKER
    user = make_user(db)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body=VOICE_MARKER, message_type="freeform",
                      channel="imessage", provider_sid="spc-v1", delivery_status="delivered"))
        s.commit()
        assert _sent_a_photo(s, user) is False
    finally:
        s.close()


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
