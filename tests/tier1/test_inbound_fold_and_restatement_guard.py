"""Text burst + photo close together → ONE reply, not two (live 2026-10-02 13:16 ×3/24h).

Layer A — FOLD (message_buffer.buffer_message): a photo landing while a text turn is
still pending joins that turn and extends it to the photo band; a text landing while a
photo is pending joins the photo turn. One flush, one model call that sees the texts and
the image, one reply. The #119 per-timer token is preserved: a flush that already
started keeps its turn and the photo starts a fresh one (that residual is layer B's).

Layer B — RESTATEMENT GUARD (agent_loop._apply_restatement_guard): a turn that made NO
writes and whose reply restates the outbound sent inside the window (same day-total
figure, or near-dup ≥ 0.6) is replaced by a minimal ack — 👍 tapback on iMessage, else
"got it". A reply with a NEW day total, a question, a correction, or an answer to the
user's question is never suppressed.

Layer C — the photo turn's system carries the same lowercase friend-voice reminder the
text path lives by (register only).
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from voice_norm import lowercase_lead   # the voice pass lowers every reply

import config
import message_buffer
from tests._fake_anthropic import ToolUse
from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"
IMG = lambda tag: {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": tag}}

TEXT_BAND = (10, 15)
PHOTO_BAND = (45, 60)

# The live 2026-10-02 13:16 pair.
PREV_REPLY = "cut it to 450, banana's in\n555 cal, 20g protein so far"
RESTATEMENT = "That banana's already in — logged it at 105 cal. Ur at 555 for the day."


@pytest.fixture(autouse=True)
def _bands(monkeypatch):
    monkeypatch.setattr(config, "REPLY_BUFFER_S", TEXT_BAND)
    monkeypatch.setattr(config, "PHOTO_BUFFER_S", PHOTO_BAND)
    monkeypatch.setattr(config, "INBOUND_FOLD_PHOTO_INTO_PENDING_TEXT", True)
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_GUARD_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_WINDOW_S", 180)
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_NEAR_DUP_THRESHOLD", 0.6)
    monkeypatch.setattr(config, "MULTI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "MAX_INBOUND_IMAGES", 5)
    message_buffer._buffers.clear()
    yield
    message_buffer._buffers.clear()


@pytest.fixture
def imessage_on(monkeypatch):
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "IMESSAGE_REACTIONS_ENABLED", True)
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", False)
    monkeypatch.setattr(config, "READ_RECEIPTS_ENABLED", False)
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", False)   # no receipt pre-classifier call
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)


@pytest.fixture
def sidecar(monkeypatch):
    """Fake sidecar: records /send, /react, /typing; answers ok."""
    import requests
    calls: list = []

    class _R:
        def __init__(self, payload, status=200):
            self._p, self.status_code, self.text = payload, status, str(payload)

        def json(self):
            return self._p

    def _post(url, json=None, headers=None, timeout=None):
        route = url.rsplit("/", 1)[-1]
        calls.append((route, json))
        return _R({"ok": True, "provider_message_id": f"spc-{route}-{len(calls)}"})
    monkeypatch.setattr(requests, "post", _post)
    return calls


# ─── helpers ──────────────────────────────────────────────────────────────────

def _cb(calls):
    def cb(user_id, body, message_type, image_url, images=None):
        calls.append({"body": body, "image_url": image_url, "images": list(images or [])})
    return cb


def _band(phone):
    return message_buffer._buffers[phone]["band"]


def _timer(phone):
    return message_buffer._buffers[phone]["timer"]


def _outbound(db, user, body, minutes_ago=1.0, message_type="freeform"):
    """A coach text sent `minutes_ago` — the restatement reference."""
    from models import get_session, Message
    s = get_session()
    try:
        m = Message(user_id=user.id, direction="out", body=body, message_type=message_type,
                    channel="sms", delivery_status="sent",
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago))
        s.add(m); s.commit(); return m.id
    finally:
        s.close()


def _inbound(db, user, body, sid, minutes_ago=0.5):
    from models import get_session, Message
    s = get_session()
    try:
        m = Message(user_id=user.id, direction="in", body=body, message_type="freeform",
                    channel="imessage", provider_sid=sid, delivery_status="delivered",
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago))
        s.add(m); s.commit(); return m.id
    finally:
        s.close()


def _payload(phone, text, pid):
    return {"phone": phone, "text": text, "provider_message_id": pid,
            "chat_guid": f"any;-;{phone}", "service": "iMessage", "line_phone": "+15102646604",
            "timestamp": "2026-10-02T20:16:00.000Z", "attachments": []}


def _post_text(client, phone, text, pid):
    return client.post("/internal/inbound", json=_payload(phone, text, pid),
                       headers={"X-Internal-Secret": SECRET})


def _post_photo(client, phone, text, pid, names=("banana.jpg",)):
    data = {"payload": json.dumps(_payload(phone, text, pid))}
    for i, n in enumerate(names):
        data[f"attachment_{i}"] = (io.BytesIO(b"\xff\xd8\xff\xe0" + bytes([i])), n, "image/jpeg")
    return client.post("/internal/inbound", data=data, headers={"X-Internal-Secret": SECRET},
                       content_type="multipart/form-data")


def _is_main_turn(kw):
    return bool(kw.get("tools"))   # the coach loop passes tools; extraction calls don't


def _has_image(kw):
    msgs = kw.get("messages") or []
    content = msgs[0].get("content") if msgs else None
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content)


def _user_text(kw):
    content = (kw.get("messages") or [{}])[0].get("content")
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return content or ""


# ═══════════════════════════════════════════════════════════════════════════
# Layer A — fold (message_buffer)
# ═══════════════════════════════════════════════════════════════════════════

def test_photo_inside_the_text_band_folds_into_the_pending_text_turn():
    """text at t=0 (10–15s band), captionless photo at t=5 → ONE buffer on the photo
    band, ONE flush whose input carries both the text and the image."""
    calls = []
    phone = "+15550001001"
    message_buffer.buffer_message(phone, "did not finish it", 1, "freeform",
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    assert _band(phone) == TEXT_BAND
    stale = _timer(phone)

    message_buffer.buffer_message(phone, "", 1, "freeform", image_url=IMG("banana"), images=[IMG("banana")],
                                  process_callback=_cb(calls), delay_override=PHOTO_BAND)
    assert _band(phone) == PHOTO_BAND, "the pending text turn is extended to the photo band"
    assert len(message_buffer._buffers[phone]["messages"]) == 2

    # #119 token guard: the text turn's superseded timer firing late is a no-op.
    stale.fire()
    assert calls == []
    _timer(phone).fire()
    assert len(calls) == 1, "one turn"
    assert calls[0]["body"] == "did not finish it"
    assert calls[0]["images"] == [IMG("banana")]
    assert calls[0]["image_url"] == IMG("banana")


def test_captioned_photo_joining_pending_text_also_gets_the_photo_band():
    """A photo WITH a caption would normally ride the text band; folded into pending
    text it gets the photo band too (a trailing 'also log this' often follows)."""
    calls = []
    phone = "+15550001002"
    message_buffer.buffer_message(phone, "left like half", 1, "freeform",
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    message_buffer.buffer_message(phone, "this one", 1, "freeform", image_url=IMG("p"), images=[IMG("p")],
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    assert _band(phone) == PHOTO_BAND
    _timer(phone).fire()
    assert len(calls) == 1 and calls[0]["body"] == "left like half\nthis one" and calls[0]["images"] == [IMG("p")]


def test_text_arriving_while_a_photo_turn_is_pending_folds_into_it():
    """photo at t=0 (45–60s), text at t=5 → one turn; the caption's own band applies."""
    calls = []
    phone = "+15550001003"
    message_buffer.buffer_message(phone, "", 1, "freeform", image_url=IMG("p"), images=[IMG("p")],
                                  process_callback=_cb(calls), delay_override=PHOTO_BAND)
    assert _band(phone) == PHOTO_BAND
    message_buffer.buffer_message(phone, "also log this banana", 1, "freeform",
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    assert _band(phone) == TEXT_BAND, "the caption arrived — no reason to hold the photo band"
    _timer(phone).fire()
    assert len(calls) == 1
    assert calls[0]["body"] == "also log this banana"
    assert calls[0]["images"] == [IMG("p")]


def test_photo_after_the_text_turn_flushed_is_a_second_turn():
    """The residual: the photo lands AFTER the text flush (t=20s) → two turns, as
    expected — the fold can't join a turn that already started; layer B covers it."""
    calls = []
    phone = "+15550001004"
    message_buffer.buffer_message(phone, "did not finish it", 1, "freeform",
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    _timer(phone).fire()
    assert len(calls) == 1
    message_buffer.buffer_message(phone, "", 1, "freeform", image_url=IMG("p"), images=[IMG("p")],
                                  process_callback=_cb(calls), delay_override=PHOTO_BAND)
    assert _band(phone) == PHOTO_BAND, "a fresh photo turn rides the photo band"
    _timer(phone).fire()
    assert len(calls) == 2
    assert calls[1]["body"] == "" and calls[1]["images"] == [IMG("p")]


def test_fold_flag_off_keeps_the_new_messages_own_band(monkeypatch):
    monkeypatch.setattr(config, "INBOUND_FOLD_PHOTO_INTO_PENDING_TEXT", False)
    calls = []
    phone = "+15550001005"
    message_buffer.buffer_message(phone, "left like half", 1, "freeform",
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    # captioned photo: its own (text) band — pre-fold behaviour
    message_buffer.buffer_message(phone, "this", 1, "freeform", image_url=IMG("p"), images=[IMG("p")],
                                  process_callback=_cb(calls), delay_override=TEXT_BAND)
    assert _band(phone) == TEXT_BAND
    # it still appends (the append itself predates the fold)
    _timer(phone).fire()
    assert len(calls) == 1 and calls[0]["images"] == [IMG("p")]


def test_fold_logs_its_kind(caplog):
    phone = "+15550001006"
    with caplog.at_level(logging.INFO, logger="cued.buffer"):
        message_buffer.buffer_message(phone, "t", 1, "freeform", process_callback=lambda *a, **k: None,
                                      delay_override=TEXT_BAND)
        message_buffer.buffer_message(phone, "", 1, "freeform", image_url=IMG("p"), images=[IMG("p")],
                                      process_callback=lambda *a, **k: None, delay_override=PHOTO_BAND)
    assert any("BUFFER_FOLD" in r.message and "kind=photo_into_text" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# Layer A end-to-end — the 2026-10-02 13:16 sequence inside one buffer window
# ═══════════════════════════════════════════════════════════════════════════

def test_13_16_sequence_within_the_window_yields_one_turn_and_one_reply(
        db, client, driver, imessage_on, monkeypatch, anthropic_stub, sms_capture):
    """texts "Did not finish it" / "Left like half" / [image] / "Also log this banana"
    all inside ~10s → ONE flush, ONE vision call that carries all three texts and the
    image, ONE reply. No second "already in" turn exists to suppress."""
    import image_normalize
    monkeypatch.setattr(image_normalize, "normalize_image", lambda b, m, name=None: IMG(name))
    seen = []

    def handler(kw):
        if _is_main_turn(kw):
            seen.append(kw)
            return PREV_REPLY
        return "ok"
    anthropic_stub.reply_with(handler)

    user = make_user(db, preferred_channel="sms")
    assert _post_text(client, user.phone, "Did not finish it", "p-1").status_code == 200
    assert _post_text(client, user.phone, "Left like half", "p-2").status_code == 200
    assert _post_photo(client, user.phone, "", "p-3").status_code == 200
    assert _band(user.phone) == PHOTO_BAND
    assert _post_text(client, user.phone, "Also log this banana", "p-4").status_code == 200
    assert len(message_buffer._buffers[user.phone]["messages"]) == 4

    driver.flush(user.phone)

    assert len(seen) == 1, "one model turn"
    assert _has_image(seen[0]), "the single turn sees the photo"
    utext = _user_text(seen[0])
    for t in ("Did not finish it", "Left like half", "Also log this banana"):
        assert t in utext
    replies = [b for (_p, b) in sms_capture]
    assert replies == [PREV_REPLY]
    assert not any("already in" in b for b in replies)


# ═══════════════════════════════════════════════════════════════════════════
# Layer B — restatement detector (pure)
# ═══════════════════════════════════════════════════════════════════════════

def test_detector_catches_the_live_pair_by_the_same_day_total():
    from agent_loop import _restatement_figure
    assert _restatement_figure(RESTATEMENT, "", PREV_REPLY) == 555


def test_detector_catches_the_10_01_double_correction_by_figure():
    from agent_loop import _restatement_figure
    prev = "fixed — logged right, ur at 1070 for the day"
    assert _restatement_figure("all good, fixed it. you're at 1,070 today", "", prev) == 1070


def test_detector_lets_new_total_question_correction_and_answers_through():
    from agent_loop import _restatement_figure
    assert _restatement_figure("added the apple, 95 cal. ur at 650 for the day", "", PREV_REPLY) is None
    assert _restatement_figure("banana's in at 555. want me to cut the rice too?", "", PREV_REPLY) is None
    assert _restatement_figure("actually that's 555 not 600 for the day", "", PREV_REPLY) is None
    assert _restatement_figure(RESTATEMENT, "did u get the banana", PREV_REPLY) is None
    assert _restatement_figure(RESTATEMENT, "what am i at?", PREV_REPLY) is None
    assert _restatement_figure("137g protein so far", "protein", PREV_REPLY) is None   # new figure
    assert _restatement_figure("nice, go get some sleep", "", PREV_REPLY) is None     # distinct
    assert _restatement_figure(RESTATEMENT, "", "") is None                           # nothing prior


def test_detector_near_dup_without_a_day_total():
    from agent_loop import _restatement_figure
    assert _restatement_figure("cut it to 450 and the banana's in", "", PREV_REPLY) == "neardup"


def test_dispatch_tool_tracks_writes_only_for_successful_non_read_tools(monkeypatch):
    import agent_tools
    agent_tools.begin_turn(777)
    monkeypatch.setitem(agent_tools._HANDLERS, "usda_food_lookup", lambda uid, inp, message_id=None: "ok: 105 cal")
    agent_tools.dispatch_tool("usda_food_lookup", {}, 777)
    assert agent_tools.turn_wrote(777) is False, "a lookup is not a write"
    monkeypatch.setitem(agent_tools._HANDLERS, "log_meal", lambda uid, inp, message_id=None: "error: nope")
    agent_tools.dispatch_tool("log_meal", {}, 777)
    assert agent_tools.turn_wrote(777) is False, "a failed write is not a write"
    monkeypatch.setitem(agent_tools._HANDLERS, "log_meal", lambda uid, inp, message_id=None: "ok: logged id 9")
    agent_tools.dispatch_tool("log_meal", {}, 777)
    assert agent_tools.turn_wrote(777) is True
    agent_tools.pop_turn_state(777)


# ═══════════════════════════════════════════════════════════════════════════
# Layer B — the guard in the loop
# ═══════════════════════════════════════════════════════════════════════════

def test_no_write_restatement_60s_after_the_outbound_is_suppressed_to_got_it(db, anthropic_stub, caplog):
    from agent_loop import run_agent_loop
    user = make_user(db, preferred_channel="sms")
    _outbound(db, user, PREV_REPLY, minutes_ago=1)
    anthropic_stub.push(RESTATEMENT)
    with caplog.at_level(logging.INFO, logger="cued.agent_loop"):
        out = run_agent_loop(user, "", "freeform")
    assert out == "got it"
    line = next(r.message for r in caplog.records if "AGENT_LOOP_RESTATEMENT_SUPPRESSED" in r.message)
    assert f"user={user.id}" in line and "figure=555" in line


def test_restatement_on_imessage_becomes_a_thumbs_up_tapback(db, anthropic_stub, imessage_on, sidecar):
    from agent_loop import run_agent_loop
    user = make_user(db, preferred_channel="imessage")
    _outbound(db, user, PREV_REPLY, minutes_ago=1)
    _inbound(db, user, "", "spc-photo-1")
    anthropic_stub.push(RESTATEMENT)
    out = run_agent_loop(user, "", "freeform")
    assert out == "", "a reaction-only turn: the tapback IS the reply"
    reacts = [j for (route, j) in sidecar if route == "react"]
    assert reacts and reacts[0]["message_id"] == "spc-photo-1" and reacts[0]["emoji"] == "like"
    assert not [j for (route, j) in sidecar if route == "send"]


def test_turn_that_wrote_is_never_suppressed(db, anthropic_stub, monkeypatch):
    import agent_tools
    from agent_loop import run_agent_loop
    monkeypatch.setitem(agent_tools._HANDLERS, "log_meal", lambda uid, inp, message_id=None: "ok: logged id 3")
    user = make_user(db, preferred_channel="sms")
    _outbound(db, user, PREV_REPLY, minutes_ago=1)
    anthropic_stub.push(ToolUse("log_meal", {"description": "apple", "calories": 95}), RESTATEMENT)
    assert run_agent_loop(user, "and an apple", "freeform") == lowercase_lead(RESTATEMENT)


def test_new_day_total_is_not_suppressed(db, anthropic_stub):
    from agent_loop import run_agent_loop
    user = make_user(db, preferred_channel="sms")
    _outbound(db, user, PREV_REPLY, minutes_ago=1)
    new = "added the apple, 95 cal. ur at 650 for the day"
    anthropic_stub.push(new)
    assert run_agent_loop(user, "", "freeform") == new


def test_reply_with_a_question_is_not_suppressed(db, anthropic_stub):
    from agent_loop import run_agent_loop
    user = make_user(db, preferred_channel="sms")
    _outbound(db, user, PREV_REPLY, minutes_ago=1)
    new = "banana's in, ur at 555 for the day. was the rice the full plate or half?"
    anthropic_stub.push(new)
    assert run_agent_loop(user, "", "freeform") == new


def test_outbound_outside_the_window_does_not_count(db, anthropic_stub):
    from agent_loop import run_agent_loop
    user = make_user(db, preferred_channel="sms")
    _outbound(db, user, PREV_REPLY, minutes_ago=10)
    anthropic_stub.push(RESTATEMENT)
    assert run_agent_loop(user, "", "freeform") == lowercase_lead(RESTATEMENT)


def test_guard_flag_off_restores_the_original_reply(db, anthropic_stub, monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_RESTATEMENT_GUARD_ENABLED", False)
    from agent_loop import run_agent_loop
    user = make_user(db, preferred_channel="sms")
    _outbound(db, user, PREV_REPLY, minutes_ago=1)
    anthropic_stub.push(RESTATEMENT)
    assert run_agent_loop(user, "", "freeform") == lowercase_lead(RESTATEMENT)


# ═══════════════════════════════════════════════════════════════════════════
# Layers A+B end-to-end — the photo lands AFTER the text flush (the live shape)
# ═══════════════════════════════════════════════════════════════════════════

def test_13_16_sequence_with_a_late_photo_yields_one_substantive_reply(
        db, client, driver, imessage_on, monkeypatch, anthropic_stub, sms_capture):
    """texts flush and reply; the photo reaches Flask after that flush → a second turn
    that writes nothing and restates → suppressed to the ack. The user sees the real
    reply once, never "That banana's already in — Ur at 555"."""
    import image_normalize
    monkeypatch.setattr(image_normalize, "normalize_image", lambda b, m, name=None: IMG(name))
    turns = []

    def handler(kw):
        if _is_main_turn(kw):
            turns.append(kw)
            return RESTATEMENT if _has_image(kw) else PREV_REPLY
        return "ok"
    anthropic_stub.reply_with(handler)

    user = make_user(db, preferred_channel="sms")
    _post_text(client, user.phone, "Did not finish it", "p-1")
    _post_text(client, user.phone, "Left like half", "p-2")
    _post_text(client, user.phone, "Also log this banana", "p-3")
    driver.flush(user.phone)                       # the text turn flushes on its short band
    _post_photo(client, user.phone, "", "p-4")     # the photo lands after that
    driver.flush(user.phone)                       # second turn

    assert len(turns) == 2 and _has_image(turns[1])
    bodies = [b for (_p, b) in sms_capture]
    assert bodies == [PREV_REPLY, "got it"]
    assert not any("already in" in b for b in bodies)


# ═══════════════════════════════════════════════════════════════════════════
# Layer C — image-path register reminder
# ═══════════════════════════════════════════════════════════════════════════

def test_photo_turn_system_carries_the_lowercase_register_reminder(db, anthropic_stub, monkeypatch):
    from agent_loop import run_agent_loop, _IMAGE_REGISTER_REMINDER
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", False)
    seen = {}

    def handler(kw):
        if _is_main_turn(kw):
            seen["system"] = kw.get("system")
        return "ok"
    anthropic_stub.reply_with(handler)
    user = make_user(db)

    run_agent_loop(user, "", "freeform", image_data=IMG("p"))
    sys_text = seen["system"] if isinstance(seen["system"], str) else "\n".join(b["text"] for b in seen["system"])
    assert _IMAGE_REGISTER_REMINDER in sys_text
    assert "lowercase" in _IMAGE_REGISTER_REMINDER

    run_agent_loop(user, "hey", "freeform")
    sys_text = seen["system"] if isinstance(seen["system"], str) else "\n".join(b["text"] for b in seen["system"])
    assert _IMAGE_REGISTER_REMINDER not in sys_text, "text turns are unchanged"
