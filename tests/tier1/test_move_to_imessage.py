"""
move_to_imessage (agent_tools, 2026-11-21). Live 2026-11-20 15:56 PT, user 100 (Maddy, on SMS
after the "I don't have an iPhone" tap at sign-up): "Also I do have an iPhone can we go to imsg"
→ "i can't move us over from my end rn so we're stuck here for now". False — the Photon invite
link existed; the coach had no tool. Now: the tool texts the invite over Twilio (green, this
thread, never the router — their line hasn't heard from them, an iMessage send would hit the
consent gate); the channel flips on their FIRST BLUE TEXT (app.py inbound), which is also what
Theo (u62, 10-15) needed: he tapped the link on his own and stayed green for two weeks.
"""
from __future__ import annotations

import json
import logging

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"
PHOTON_ID = "73ad38f3-46af-4fb2-8858-c15919ba7aac"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "MOVE_TO_IMESSAGE_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "IMESSAGE_INBOUND_FLIPS_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "SPECTRUM_API_URL", "https://spectrum.photon.codes")
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)


def _tool_names(kw) -> set:
    return {t.get("name") for t in (kw.get("tools") or []) if isinstance(t, dict)}


def _out(db, user):
    from models import Message
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")
            .order_by(Message.id).all())


def _row(db, uid):
    from models import User
    db.expire_all()
    return db.get(User, uid)


# ── the handler ────────────────────────────────────────────────────────────

def test_sms_user_gets_the_invite_green_and_the_channel_waits_for_their_blue_text(db, sms_capture, caplog):
    from agent_tools import handle_move_to_imessage
    u = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID, imessage_opted_in_at=None)
    caplog.set_level(logging.INFO)
    r = handle_move_to_imessage(u.id, {})
    assert r.startswith("ok: the link went out as its own text")
    assert len(sms_capture) == 1, "one green text, over Twilio"
    body = sms_capture[0][1]
    assert f"https://spectrum.photon.codes/users/{PHOTON_ID}/redirect?msg=hey%20cued" in body
    assert "save that number as a contact" in body
    rows = _out(db, u)
    assert [(r.message_type, r.channel, r.delivery_status) for r in rows] == [("imessage_invite", "sms", "sent")]
    assert _row(db, u.id).preferred_channel == "sms", "NOT flipped here — their first blue text does that"
    assert "MOVE_TO_IMESSAGE user=%d via=invite_sms" % u.id in caplog.text


def test_already_on_imessage_sends_nothing(db, sms_capture):
    from agent_tools import handle_move_to_imessage
    u = make_user(db, preferred_channel="imessage", photon_user_id=PHOTON_ID, channel_failed_over=False)
    r = handle_move_to_imessage(u.id, {})
    assert r.startswith("already:") and "save the number as a contact" in r
    assert sms_capture == []


def test_opted_in_before_flips_now_and_resets_the_breaker(db, sms_capture):
    """Theo's shape: he texted the line once (opted in), then the row said sms. The line works
    for him — flip now, no link; this turn's reply goes blue."""
    from datetime import datetime
    from agent_tools import handle_move_to_imessage
    u = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID, imessage_opted_in_at=datetime(2026, 10, 15, 19, 37),
                  channel_failed_over=True)
    r = handle_move_to_imessage(u.id, {})
    assert r.startswith("ok: switched")
    assert sms_capture == []
    row = _row(db, u.id)
    assert row.preferred_channel == "imessage" and row.channel_failed_over is False


def test_no_line_and_no_provisioning_is_an_honest_error(db, sms_capture):
    from agent_tools import handle_move_to_imessage
    u = make_user(db, preferred_channel="sms", photon_user_id=None)
    r = handle_move_to_imessage(u.id, {})
    assert r.startswith("error: no imessage line is available")
    assert sms_capture == [] and _row(db, u.id).preferred_channel == "sms"


def test_imessage_off_is_an_honest_error(db, sms_capture, monkeypatch):
    from agent_tools import handle_move_to_imessage
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", False)
    u = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID)
    assert handle_move_to_imessage(u.id, {}).startswith("error: imessage isn't available")
    assert sms_capture == []


# ── the loop offers it only to a green thread ─────────────────────────────

def test_the_tool_is_offered_to_an_sms_user_and_not_to_an_imessage_user(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    import sms
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body: "photon-x")
    seen = {}
    def script(kw):
        if kw.get("tools"):
            seen["tools"] = _tool_names(kw)
            return "ok"
        return "{}"
    anthropic_stub.reply_with(script)
    green = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID)
    appmod.process_buffered_message(green.id, "hey", "freeform")
    assert "move_to_imessage" in seen["tools"]
    blue = make_user(db, preferred_channel="imessage", photon_user_id=PHOTON_ID)
    appmod.process_buffered_message(blue.id, "hey", "freeform")
    assert "move_to_imessage" not in seen["tools"]


def test_maddys_turn_end_to_end(db, sms_capture, anthropic_stub):
    """"Also I do have an iPhone can we go to imsg" → the tool fires, the link rides as its own
    green bubble, the coach's line is the sentence around it — never "i can't move us over"."""
    import app as appmod
    u = make_user(db, name="Maddy", preferred_channel="sms", photon_user_id=PHOTON_ID, imessage_opted_in_at=None)
    state = {"n": 0}

    def script(kw):
        state["n"] += 1
        if state["n"] == 1:
            return ToolUse("move_to_imessage", {})
        return "yep, tap that once and say hey, i'll pick up over there"
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "Also I do have an iPhone can we go to imsg", "freeform")
    bodies = [b for _p, b in sms_capture]
    assert len(bodies) == 2
    assert "redirect?msg=hey%20cued" in bodies[0], "the link bubble first"
    assert bodies[1] == "yep, tap that once and say hey, i'll pick up over there"
    assert _row(db, u.id).preferred_channel == "sms"


# ── their first blue text flips the channel ──────────────────────────────

def _post_inbound(client, payload):
    return client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET},
                       content_type="application/json")


def _payload(phone, text, msg_id="photon-in-1"):
    return {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
            "service": "iMessage", "line_phone": "+16282647704",
            "timestamp": "2026-11-21T00:16:29.000Z", "attachments": []}


def test_first_blue_text_from_a_provisioned_sms_user_flips_the_channel(db, client, caplog):
    u = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID, imessage_opted_in_at=None)
    caplog.set_level(logging.INFO)
    r = _post_inbound(client, _payload(u.phone, "hey cued"))
    assert r.status_code == 200
    row = _row(db, u.id)
    assert row.preferred_channel == "imessage" and row.imessage_opted_in_at is not None
    assert "IMESSAGE_CHANNEL_FLIPPED_ON_INBOUND user_id=%d" % u.id in caplog.text


def test_an_unprovisioned_user_or_the_flag_off_does_not_flip(db, client, monkeypatch, caplog):
    u = make_user(db, preferred_channel="sms", photon_user_id=None)
    _post_inbound(client, _payload(u.phone, "hey cued", msg_id="photon-in-2"))
    assert _row(db, u.id).preferred_channel == "sms", "no Photon seat: nothing to flip to"
    monkeypatch.setattr(config, "IMESSAGE_INBOUND_FLIPS_CHANNEL_ENABLED", False)
    v = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID)
    _post_inbound(client, _payload(v.phone, "hey cued", msg_id="photon-in-3"))
    assert _row(db, v.id).preferred_channel == "sms"


def test_a_green_text_never_flips_anything(db, driver):
    u = make_user(db, preferred_channel="sms", photon_user_id=PHOTON_ID)
    driver.send(u, "hey")
    assert _row(db, u.id).preferred_channel == "sms"
