"""
send_image — the coach puts a PICTURE from a public url in the thread (outbound_images.py
+ agent_tools.SEND_IMAGE_TOOL). HTTP is mocked (webfetch's envelope is the same one
test_fetch_page.py pins); the sidecar attachment / Twilio MMS senders are captured.

Pinned here:
  * iMessage user → a real attachment bubble (normalized bytes, right mime + name);
    SMS user → an MMS whose media url is the picture's own public url.
  * The Message row carries the "[sent picture: …]" marker (history + repeat guard).
  * Honest envelope: not-a-picture / unreachable / private host → "error: …" strings; an
    attachment the pipe refuses degrades to the LINK as text and the result SAYS link;
    Photon's ambiguous "dispatch may have completed" counts as sent, no text double.
  * Caps: per-turn cap on the turn state, repeat guard on the same url, flag off = inert.
  * The loop offers the tool only when the flag is on; voice.md carries the rule.
"""
from __future__ import annotations

import io
import socket

import pytest

import config
import webfetch
from tests.factories import make_user
from tests.tier1.test_fetch_page import _Resp, _patch_http

SIDECAR = "http://sidecar.test:8080"


def _png(w=120, h=80) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "SEND_IMAGE_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "FETCH_PAGE_MEDIA_ENABLED", True)
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "testsecret")

    def fake_gai(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(webfetch.socket, "getaddrinfo", fake_gai)
    webfetch._CACHE.clear()
    import sms
    monkeypatch.setattr(sms, "outage_active", lambda: False)
    yield
    webfetch._CACHE.clear()


@pytest.fixture
def attachment_ok(monkeypatch):
    import sms
    calls: list = []

    def _fake(phone, data, name, mime_type):
        calls.append((phone, data, name, mime_type))
        return f"photon-att-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage_attachment", _fake)
    return calls


@pytest.fixture
def mms_ok(monkeypatch):
    import sms
    calls: list = []

    def _fake(phone, media_url, body=""):
        calls.append((phone, media_url))
        return f"MM{len(calls):030d}"
    monkeypatch.setattr(sms, "_send_single_mms", _fake)
    return calls


def _out(db, user_id):
    from models import Message
    return (db.query(Message).filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.id).all())


MENU_URL = "https://cdn.example/content/Fall+11%3A2%3A2026+Menu+.png"


def test_imessage_user_gets_the_picture_as_an_attachment_bubble(db, attachment_ok, monkeypatch):
    import agent_tools
    png = _png(1056, 816)
    _patch_http(monkeypatch, {MENU_URL: _Resp(png, ctype="image/png")})
    u = make_user(db, preferred_channel="imessage")
    agent_tools.begin_turn(u.id)
    try:
        out = agent_tools.dispatch_tool("send_image", {"url": MENU_URL, "caption": "grizzly chefs menu 11/2"}, u.id)
    finally:
        agent_tools.pop_turn_state(u.id)
    assert out.startswith("ok: sent the picture (1056x816)") and "sentence around it" in out
    phone, data, name, mime = attachment_ok[0]
    assert phone == u.phone and data == png and mime == "image/png" and name.endswith(".png")
    rows = _out(db, u.id)
    assert len(rows) == 1
    assert rows[0].body == f"[sent picture: grizzly chefs menu 11/2 — {MENU_URL}]"
    assert (rows[0].channel, rows[0].delivery_status, rows[0].provider_sid) == ("imessage", "sent", "photon-att-1")


def test_sms_user_gets_an_mms_pointing_at_the_public_url(db, mms_ok, sms_capture, monkeypatch):
    import agent_tools
    _patch_http(monkeypatch, {MENU_URL: _Resp(_png(), ctype="image/png")})
    u = make_user(db)                       # no imessage preference → sms
    out = agent_tools.dispatch_tool("send_image", {"url": MENU_URL}, u.id)
    assert out.startswith("ok: sent the picture")
    assert mms_ok == [(u.phone, MENU_URL)] and not sms_capture
    rows = _out(db, u.id)
    assert rows[0].channel == "sms" and rows[0].body.startswith("[sent picture: Fall+11:2:2026+Menu+.png")


def test_a_page_url_or_a_bad_url_is_an_honest_error_and_sends_nothing(db, attachment_ok, mms_ok, monkeypatch):
    import agent_tools
    _patch_http(monkeypatch, {"https://grizzlychefs.com/weekly-menu": _Resp("<html><body><main><p>menu</p></main></body></html>")})
    u = make_user(db, preferred_channel="imessage")
    out = agent_tools.dispatch_tool("send_image", {"url": "https://grizzlychefs.com/weekly-menu"}, u.id)
    assert out.startswith("error:") and "isn't a picture" in out
    webfetch._CACHE.clear()
    out = agent_tools.dispatch_tool("send_image", {"url": "http://127.0.0.1/x.png"}, u.id)
    assert out.startswith("error:") and "couldn't get that picture" in out
    out = agent_tools.dispatch_tool("send_image", {"url": "https://cdn.example/missing.png"}, u.id)
    assert out.startswith("error:") and "404" in out
    assert not attachment_ok and not mms_ok and not _out(db, u.id)


def test_refused_attachment_degrades_to_the_link_and_says_so(db, sms_capture, monkeypatch):
    import agent_tools, sms
    _patch_http(monkeypatch, {MENU_URL: _Resp(_png(), ctype="image/png")})
    monkeypatch.setattr(sms, "_send_imessage_attachment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sidecar /send-attachment 502: nope")))
    u = make_user(db, preferred_channel="imessage")
    out = agent_tools.dispatch_tool("send_image", {"url": MENU_URL}, u.id)
    assert out.startswith("ok:") and "LINK" in out and "don't claim they can see it inline" in out
    assert sms_capture and sms_capture[-1][1] == MENU_URL
    assert not any(r.body.startswith("[sent picture:") for r in _out(db, u.id))


def test_ambiguous_dispatch_counts_as_sent_with_no_text_double(db, sms_capture, monkeypatch):
    import agent_tools, sms
    _patch_http(monkeypatch, {MENU_URL: _Resp(_png(), ctype="image/png")})
    monkeypatch.setattr(sms, "_send_imessage_attachment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("502: dispatch may have completed")))
    u = make_user(db, preferred_channel="imessage")
    out = agent_tools.dispatch_tool("send_image", {"url": MENU_URL}, u.id)
    assert out.startswith("ok: sent the picture")
    assert not sms_capture
    rows = _out(db, u.id)
    assert len(rows) == 1 and rows[0].body.startswith("[sent picture:") and rows[0].provider_sid is None


def test_repeat_guard_and_per_turn_cap(db, attachment_ok, monkeypatch):
    import agent_tools
    _patch_http(monkeypatch, default=_Resp(_png(), ctype="image/png"))
    u = make_user(db, preferred_channel="imessage")
    agent_tools.begin_turn(u.id)
    try:
        assert agent_tools.dispatch_tool("send_image", {"url": "https://cdn.example/a.png"}, u.id).startswith("ok:")
        again = agent_tools.dispatch_tool("send_image", {"url": "https://cdn.example/a.png"}, u.id)
        assert again.startswith("error:") and "already in the thread" in again
        assert agent_tools.dispatch_tool("send_image", {"url": "https://cdn.example/b.png"}, u.id).startswith("ok:")
        third = agent_tools.dispatch_tool("send_image", {"url": "https://cdn.example/c.png"}, u.id)
        assert third.startswith("error:") and "limit" in third
    finally:
        agent_tools.pop_turn_state(u.id)
    assert len(attachment_ok) == 2


def test_flag_off_is_inert_and_the_loop_offers_it_only_when_on(db, attachment_ok, monkeypatch):
    import agent_tools, agent_loop
    monkeypatch.setattr(config, "SEND_IMAGE_TOOL_ENABLED", False)
    u = make_user(db, preferred_channel="imessage")
    out = agent_tools.dispatch_tool("send_image", {"url": MENU_URL}, u.id)
    assert out.startswith("error:") and "not enabled" in out and not attachment_ok
    src = open(agent_loop.__file__).read()
    assert "if config.SEND_IMAGE_TOOL_ENABLED:" in src and "SEND_IMAGE_TOOL" in src


def test_tool_description_and_prompt_carry_the_rules():
    import agent_tools
    desc = agent_tools.SEND_IMAGE_TOOL["description"]
    assert "never a photo THEY sent you" in desc and "only a link" in desc and "don't paste a url" in desc
    voice = open("prompts/voice.md").read()
    assert "send_image" in voice and "Show it when seeing beats reading" in voice
    assert 'never "see the pic" when it was a link' in voice
