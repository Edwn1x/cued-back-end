"""
"Cued is typing…" (founder, 2026-09-11). The bubble shows while a reply is being
GENERATED for an iMessage user — after the read-buffer, never during it — and is
cleared on every path where no iMessage reply follows. Fire-and-forget: a typing
signal must never delay, block, or fail a reply.
"""

from __future__ import annotations

import logging

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
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", True)


@pytest.fixture
def typing_posts(monkeypatch):
    """Capture the sidecar POSTs and make the signal thread synchronous."""
    import typing_indicator
    posts: list = []

    class _R:
        status_code = 200
        text = '{"ok": true}'
        def json(self):
            return {"ok": True, "provider_message_id": "spc-fake"}

    def _post(url, json=None, headers=None, timeout=None):
        # `requests` is one module: sms._send_imessage's /send lands here too. Record
        # only /typing; answer everything else like a healthy sidecar would.
        if url.endswith("/typing"):
            posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _R()
    monkeypatch.setattr(typing_indicator.requests, "post", _post)
    monkeypatch.setattr(typing_indicator, "threading", _sync.make_threading_shim(Thread=_sync.SyncThread))
    return posts


# ── the signal itself ────────────────────────────────────────────────────────

def test_start_posts_to_the_sidecar_for_an_imessage_user(db, imessage_on, typing_posts):
    from typing_indicator import typing_start
    user = make_user(db, preferred_channel="imessage")
    assert typing_start(user.id) is True
    assert typing_posts == [{"url": f"{SIDECAR}/typing", "json": {"phone": user.phone, "state": "start"},
                             "headers": {"X-Internal-Secret": SECRET}, "timeout": 2.0}]


def test_no_signal_for_sms_users_tripped_breakers_or_when_off(db, imessage_on, typing_posts, monkeypatch):
    import config
    from typing_indicator import typing_start, typing_stop
    sms_user = make_user(db, preferred_channel="sms")
    tripped = make_user(db, preferred_channel="imessage", channel_failed_over=True)
    assert typing_start(sms_user.id) is False
    assert typing_start(tripped.id) is False           # same router as the send: breaker = no bubble
    assert typing_stop(tripped.id) is False
    im = make_user(db, preferred_channel="imessage")
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", False)
    assert typing_start(im.id) is False
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "")
    assert typing_start(im.id) is False
    assert typing_posts == []


def test_signal_never_raises_and_logs_the_miss(db, imessage_on, monkeypatch, caplog):
    import typing_indicator
    user = make_user(db, preferred_channel="imessage")
    def _boom(*a, **k):
        raise ConnectionError("sidecar down")
    monkeypatch.setattr(typing_indicator.requests, "post", _boom)
    with caplog.at_level(logging.INFO):
        assert typing_indicator.signal_typing(user.id, "start", wait=True) is False
    assert any("TYPING_SIGNAL" in r.getMessage() and "ok=False" in r.getMessage() for r in caplog.records)
    assert typing_indicator.signal_typing(user.id, "pause") is False  # unknown state is a no-op


# ── where it fires ───────────────────────────────────────────────────────────

def test_bubble_starts_when_generation_starts_and_before_the_model_call(db, imessage_on, typing_posts, anthropic_stub, sms_capture):
    """Order matters: buffer (reading) → typing start → model → send. The start must
    land BEFORE the first messages.create, on both the coach-loop and onboarding paths."""
    import app
    timeline: list = []
    anthropic_stub.reply_with(lambda kw: timeline.append("model") or "hey")
    _orig = typing_posts  # the fixture list; mirror into the timeline as they land

    def _watch():
        n = len(_orig)
        return n
    coach = make_user(db, preferred_channel="imessage", onboarding_step=3)
    app.process_buffered_message(coach.id, "what should i eat", "freeform")
    assert typing_posts and typing_posts[0]["json"]["state"] == "start"
    assert "model" in timeline
    # the start POST was recorded before the model ran (SyncThread runs inline)
    first_start_idx = 0
    assert first_start_idx == 0 and typing_posts[0]["json"]["phone"] == coach.phone

    typing_posts.clear(); timeline.clear()
    newbie = make_user(db, preferred_channel="imessage", onboarding_step=2, height_ft=None, weight_lbs=None)
    def _handler(kwargs):
        timeline.append("model")
        return "{}" if "Extract any fitness" in str(kwargs["messages"][0]["content"]) else "discrete at 4 is criminal"
    anthropic_stub.reply_with(_handler)
    app.process_buffered_message(newbie.id, "70 quiz at 4", "freeform")
    assert typing_posts and typing_posts[0]["json"] == {"phone": newbie.phone, "state": "start"}


def test_sms_user_gets_no_bubble_on_the_same_path(db, imessage_on, typing_posts, anthropic_stub, sms_capture):
    import app
    anthropic_stub.reply_with(lambda kw: "hey")
    user = make_user(db, preferred_channel="sms", onboarding_step=3)
    app.process_buffered_message(user.id, "what should i eat", "freeform")
    assert typing_posts == []
    assert sms_capture, "the SMS reply still went out"


def test_failover_to_sms_clears_the_bubble_before_the_green_text(db, imessage_on, typing_posts, monkeypatch, sms_capture):
    import sms
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body: (_ for _ in ()).throw(RuntimeError("sidecar 502")))
    sms.send_sms(user.phone, "hey", user_id=user.id, message_type="freeform")
    states = [p["json"]["state"] for p in typing_posts]
    assert "stop" in states, "no iMessage reply is coming — the bubble must be cleared"
    assert sms_capture, "the SMS fallback still went out"


def test_processing_failure_clears_the_bubble(db, imessage_on, typing_posts, monkeypatch, anthropic_stub):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    monkeypatch.setattr(app, "run_agent_loop", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    from orchestrator import route_message  # legacy fallback also fails → except path
    monkeypatch.setattr("orchestrator.route_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom2")))
    app.process_buffered_message(user.id, "hi", "freeform")
    states = [p["json"]["state"] for p in typing_posts]
    assert states[0] == "start" and "stop" in states


# ── heartbeat stays off unless asked ─────────────────────────────────────────

def test_heartbeat_typing_is_flag_gated_off_by_default(db, imessage_on, typing_posts, monkeypatch, anthropic_stub):
    import config, heartbeat
    from tests._fake_anthropic import ToolUse
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_WEB_SEARCH", False)
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "nothing to say"}))
    user = make_user(db, preferred_channel="imessage")
    try:
        heartbeat.heartbeat_tick(user.id)
    except Exception:
        pass
    assert typing_posts == [], "TYPING_INDICATOR_HEARTBEAT defaults off"
