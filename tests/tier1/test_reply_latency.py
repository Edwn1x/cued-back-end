"""Reply latency (founder 2026-11-20: "it always takes forever to reply… but we don't
want to miss texts either").

Measured that day (prod, 7d): p50 text→reply 36s against a 10–15s band. One real turn:
3s of webhook pre-pass BEFORE the timer started, 10s band, ~3s of serial lookups after
the flush, then the model. And a text landing mid-turn paid its own full band on top of
waiting for the in-flight reply. Four changes, none of which drops a text:

  1. The band counts from ARRIVAL, not from the end of the pre-pass — the pre-pass now
     overlaps the wait instead of adding to it (message_buffer.buffer_message arrived_at).
  2. A text that lands while a turn is in flight (or seconds after a flush) is a
     CONTINUATION: it gets a ~2s band, and while it waits for the in-flight reply the
     buffer stays open so a further text joins the same turn.
  3. A text that reads complete (a question, a direct ask, terminal punctuation) gets the
     shorter REPLY_BUFFER_COMPLETE_S band; fragments keep the full band; photos and
     onboarding are untouched.
  4. The read receipt and typing signal do their DB lookups INSIDE their thread — the
     caller no longer pays two round trips per signal before the model runs.

Plus a TURN_LATENCY line per turn so the split is visible in prod from now on.
"""
from __future__ import annotations

import logging
import threading
import time

import pytest

import config
import message_buffer
from tests import _sync
from tests._sync import PENDING_TIMERS
from tests.factories import make_user

PHONE = "+15550109999"
BAND = (10, 10)          # a degenerate band so the drawn delay is deterministic


class FakeClock:
    """A controllable monotonic clock shared by app.py and message_buffer."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def monotonic(self):
        return self.t

    def advance(self, s: float):
        self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(message_buffer, "_monotonic", c.monotonic)
    import app
    monkeypatch.setattr(app, "_monotonic", c.monotonic)
    return c


@pytest.fixture(autouse=True)
def _clean_buffer():
    message_buffer._buffers.clear()
    message_buffer._in_flight.clear()
    message_buffer._last_flush.clear()
    yield
    message_buffer._buffers.clear()
    message_buffer._in_flight.clear()
    message_buffer._last_flush.clear()


def _timer(phone=PHONE):
    t = PENDING_TIMERS.get(phone)
    assert t is not None, "no buffer timer armed"
    return t


# ── 1. the band counts from arrival ──────────────────────────────────────────

def test_timer_is_shortened_by_the_time_already_spent_since_arrival(clock):
    arrived = clock.monotonic()
    clock.advance(3.0)                                   # the webhook pre-pass took 3s
    message_buffer.buffer_message(PHONE, "hey", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=arrived)
    assert _timer().interval == pytest.approx(7.0, abs=0.01)


def test_timer_never_goes_below_the_floor_when_the_prepass_outlasted_the_band(clock, monkeypatch):
    monkeypatch.setattr(config, "BUFFER_MIN_REMAINING_S", 1.0)
    arrived = clock.monotonic()
    clock.advance(14.0)
    message_buffer.buffer_message(PHONE, "hey", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=arrived)
    assert _timer().interval == pytest.approx(1.0, abs=0.01)


def test_without_an_arrival_stamp_the_full_band_applies(clock):
    message_buffer.buffer_message(PHONE, "hey", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND)
    assert _timer().interval == pytest.approx(10.0, abs=0.01)


def test_an_appended_text_restarts_from_its_own_arrival(clock):
    a1 = clock.monotonic()
    message_buffer.buffer_message(PHONE, "first", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=a1)
    clock.advance(6.0)
    a2 = clock.monotonic()
    clock.advance(1.5)                                   # its own pre-pass
    message_buffer.buffer_message(PHONE, "second", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=a2)
    assert _timer().interval == pytest.approx(8.5, abs=0.01)
    assert [m["body"] for m in message_buffer._buffers[PHONE]["messages"]] == ["first", "second"]


def test_the_webhook_stamps_arrival_before_its_prepass(db, client, clock, monkeypatch):
    """The pre-pass (safety regex, classify, state writes…) runs between arrival and the
    buffer. Make it 'take' 3s on the fake clock: the armed timer must be band − 3."""
    import app
    monkeypatch.setattr(config, "REPLY_BUFFER_S", BAND)
    monkeypatch.setattr(config, "REPLY_BUFFER_COMPLETE_S", BAND)
    user = make_user(db)
    real = app.apply_safety_signals_task

    def slow_prepass(uid, body):
        clock.advance(3.0)
        return real(uid, body)
    monkeypatch.setattr(app, "apply_safety_signals_task", slow_prepass)
    resp = client.post("/webhook", data={"From": user.phone, "Body": "did legs today and",
                                         "NumMedia": "0", "MessageSid": "SMlat0001"})
    assert resp.status_code == 200
    assert _timer(user.phone).interval == pytest.approx(7.0, abs=0.01)


# ── 2. a mid-turn text is a continuation: short band, joins while waiting ────

def test_text_landing_during_an_inflight_turn_gets_the_continuation_band(clock, monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_BUFFER_S", (2, 2))
    message_buffer._in_flight[PHONE] = threading.Event()    # a turn is being answered
    message_buffer.buffer_message(PHONE, "also", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=clock.monotonic())
    assert _timer().interval == pytest.approx(2.0, abs=0.01)
    assert message_buffer._buffers[PHONE]["continuation"] is True


def test_text_landing_seconds_after_a_flush_gets_the_continuation_band(clock, monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_BUFFER_S", (2, 2))
    monkeypatch.setattr(config, "BUFFER_JOIN_WINDOW_S", 2.0)
    message_buffer._last_flush[PHONE] = time.monotonic()    # _last_flush uses the real clock
    message_buffer.buffer_message(PHONE, "wait also", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=clock.monotonic())
    assert _timer().interval == pytest.approx(2.0, abs=0.01)


def test_a_photo_is_never_a_continuation_and_keeps_its_band(clock, monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_BUFFER_S", (2, 2))
    message_buffer._in_flight[PHONE] = threading.Event()
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
    message_buffer.buffer_message(PHONE, "", 1, "freeform", image_url=img, images=[img],
                                  process_callback=lambda *a, **k: None,
                                  delay_override=(45, 45), arrived_at=clock.monotonic())
    assert _timer().interval == pytest.approx(45.0, abs=0.01)
    assert message_buffer._buffers[PHONE]["continuation"] is False


def test_continuation_band_is_off_switchable(clock, monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_SHORT_BAND_ENABLED", False)
    message_buffer._in_flight[PHONE] = threading.Event()
    message_buffer.buffer_message(PHONE, "also", 1, "freeform", process_callback=lambda *a, **k: None,
                                  delay_override=BAND, arrived_at=clock.monotonic())
    assert _timer().interval == pytest.approx(10.0, abs=0.01)


def test_a_text_arriving_while_the_continuation_waits_joins_the_same_turn(clock, monkeypatch):
    """Flush fires while the previous turn is still in flight. The buffer must stay OPEN
    during that wait: a further text appends to it (and supersedes the waiting flush),
    so the user gets ONE reply to both, not two turns."""
    monkeypatch.setattr(config, "CONTINUATION_BUFFER_S", (2, 2))
    monkeypatch.setattr(config, "BUFFER_INFLIGHT_WAIT_S", 5)
    turns: list = []

    def cb(user_id, body, message_type, image_url, images=None, continuation=False):
        turns.append((body, continuation))

    prior = threading.Event()
    message_buffer._in_flight[PHONE] = prior
    message_buffer.buffer_message(PHONE, "also did curls", 1, "freeform", process_callback=cb,
                                  delay_override=BAND, arrived_at=clock.monotonic())
    first_timer = _timer()
    waiting = threading.Thread(target=first_timer.fire, daemon=True)
    waiting.start()
    # Let the flush reach its wait on the prior turn.
    deadline = time.time() + 2
    while PHONE not in message_buffer._waiting_on_prior and time.time() < deadline:
        time.sleep(0.01)
    assert PHONE in message_buffer._waiting_on_prior
    # A second text lands while we wait: it must APPEND, not open a new turn.
    message_buffer.buffer_message(PHONE, "and 10 min bike", 1, "freeform", process_callback=cb,
                                  delay_override=BAND, arrived_at=clock.monotonic())
    assert [m["body"] for m in message_buffer._buffers[PHONE]["messages"]] == ["also did curls", "and 10 min bike"]
    prior.set()                                          # the previous turn finishes
    waiting.join(timeout=3)
    assert turns == [], "the superseded flush must not run the turn"
    _timer().fire()                                      # the restarted timer owns the turn
    assert turns == [("also did curls\nand 10 min bike", True)]


def test_continuation_still_waits_for_the_inflight_turn_then_runs_once(clock, monkeypatch):
    monkeypatch.setattr(config, "CONTINUATION_BUFFER_S", (2, 2))
    monkeypatch.setattr(config, "BUFFER_INFLIGHT_WAIT_S", 5)
    turns: list = []

    def cb(user_id, body, message_type, image_url, images=None, continuation=False):
        turns.append((body, continuation))

    prior = threading.Event()
    message_buffer._in_flight[PHONE] = prior
    message_buffer.buffer_message(PHONE, "also", 1, "freeform", process_callback=cb,
                                  delay_override=BAND, arrived_at=clock.monotonic())
    t = threading.Thread(target=_timer().fire, daemon=True)
    t.start()
    time.sleep(0.05)
    assert turns == []                                   # still waiting on the prior turn
    prior.set()
    t.join(timeout=3)
    assert turns == [("also", True)]
    assert PHONE not in message_buffer._buffers


# ── 3. a complete-looking text gets the shorter band ─────────────────────────

@pytest.mark.parametrize("text", [
    "whats my workout today?",
    "how much protein should i eat",
    "send my card",
    "what's my link",
    "can u move my 3pm to 4",
    "I did legs today.",
    "logged 3 eggs and toast!",
    "gimme the rsf line",
    "wyd rn?",
])
def test_complete_texts(text):
    from inbound_band import looks_complete
    assert looks_complete(text) is True


@pytest.mark.parametrize("text", [
    "did legs today and",
    "ok",
    "yeah",
    "lol",
    "wait...",
    "so i was thinking about the plan for next week since my schedule",
    "bench 135, then",
    "",
    "   ",
])
def test_fragments_and_bare_acks_keep_the_full_band(text):
    from inbound_band import looks_complete
    assert looks_complete(text) is False


def test_pick_band_routes_complete_text_to_the_short_band(monkeypatch):
    from inbound_band import pick_reply_band
    monkeypatch.setattr(config, "REPLY_BUFFER_S", (10, 15))
    monkeypatch.setattr(config, "REPLY_BUFFER_COMPLETE_S", (6, 8))
    monkeypatch.setattr(config, "ADAPTIVE_REPLY_BAND_ENABLED", True)
    assert pick_reply_band("whats my workout?") == (6, 8)
    assert pick_reply_band("did legs and") == (10, 15)


def test_pick_band_is_off_switchable(monkeypatch):
    from inbound_band import pick_reply_band
    monkeypatch.setattr(config, "REPLY_BUFFER_S", (10, 15))
    monkeypatch.setattr(config, "REPLY_BUFFER_COMPLETE_S", (6, 8))
    monkeypatch.setattr(config, "ADAPTIVE_REPLY_BAND_ENABLED", False)
    assert pick_reply_band("whats my workout?") == (10, 15)


def test_webhook_arms_the_short_band_for_a_question_and_the_full_band_for_a_fragment(db, client, clock, monkeypatch):
    monkeypatch.setattr(config, "REPLY_BUFFER_S", (10, 10))
    monkeypatch.setattr(config, "REPLY_BUFFER_COMPLETE_S", (6, 6))
    monkeypatch.setattr(config, "ADAPTIVE_REPLY_BAND_ENABLED", True)
    u1 = make_user(db)
    u2 = make_user(db)
    client.post("/webhook", data={"From": u1.phone, "Body": "whats my workout today?",
                                  "NumMedia": "0", "MessageSid": "SMlat0101"})
    client.post("/webhook", data={"From": u2.phone, "Body": "did legs today and",
                                  "NumMedia": "0", "MessageSid": "SMlat0102"})
    assert _timer(u1.phone).interval == pytest.approx(6.0, abs=0.01)
    assert _timer(u2.phone).interval == pytest.approx(10.0, abs=0.01)


def test_onboarding_and_photo_bands_are_untouched_by_the_adaptive_rule(db, client, clock, monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_BUFFER_S", (5, 5))
    monkeypatch.setattr(config, "REPLY_BUFFER_COMPLETE_S", (3, 3))
    monkeypatch.setattr(config, "ADAPTIVE_REPLY_BAND_ENABLED", True)
    newbie = make_user(db, onboarding_step=1)
    client.post("/webhook", data={"From": newbie.phone, "Body": "4 days a week?",
                                  "NumMedia": "0", "MessageSid": "SMlat0201"})
    assert _timer(newbie.phone).interval == pytest.approx(5.0, abs=0.01)


# ── 4. read receipt + typing signal never block the caller on the DB ─────────

@pytest.fixture
def imessage_on(monkeypatch):
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.railway.internal:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "testsecret")
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", True)
    monkeypatch.setattr(config, "READ_RECEIPTS_ENABLED", True)


class _RecordingThread(threading.Thread):
    """A real thread that remembers itself so the test can join it."""
    instances: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _RecordingThread.instances.append(self)


def _ok_post(url, json=None, headers=None, timeout=None):
    class _R:
        status_code = 200
    return _R()


def test_typing_signal_looks_up_the_user_inside_its_thread(db, imessage_on, monkeypatch):
    import typing_indicator
    user = make_user(db, preferred_channel="imessage")
    seen: list = []
    real = typing_indicator._phone_and_channel

    def spy(uid):
        seen.append(threading.current_thread().name)
        return real(uid)
    monkeypatch.setattr(typing_indicator, "_phone_and_channel", spy)
    monkeypatch.setattr(typing_indicator.requests, "post", _ok_post)
    _RecordingThread.instances.clear()
    monkeypatch.setattr(typing_indicator, "threading", _sync.make_threading_shim(Thread=_RecordingThread))
    assert typing_indicator.typing_start(user.id) is True
    for t in _RecordingThread.instances:
        t.join(timeout=3)
    assert seen and all(n != threading.main_thread().name for n in seen), seen


def test_read_receipt_looks_up_the_user_inside_its_thread(db, imessage_on, monkeypatch):
    import read_receipts
    user = make_user(db, preferred_channel="imessage")
    seen: list = []
    real = read_receipts._latest_inbound_sid

    def spy(uid):
        seen.append(threading.current_thread().name)
        return real(uid)
    monkeypatch.setattr(read_receipts, "_latest_inbound_sid", spy)
    monkeypatch.setattr(read_receipts.requests, "post", _ok_post)
    _RecordingThread.instances.clear()
    monkeypatch.setattr(read_receipts, "threading", _sync.make_threading_shim(Thread=_RecordingThread))
    assert read_receipts.mark_read(user.id) is True
    for t in _RecordingThread.instances:
        t.join(timeout=3)
    assert seen and all(n != threading.main_thread().name for n in seen), seen


def test_typing_signal_still_reports_no_dispatch_for_an_sms_user_when_run_inline(db, imessage_on, monkeypatch):
    """The existing contract under the synchronous shim: an SMS user gets no bubble and the
    call says so (tests/tier1/test_typing_indicator.py relies on this)."""
    import typing_indicator
    monkeypatch.setattr(typing_indicator.requests, "post", _ok_post)
    monkeypatch.setattr(typing_indicator, "threading", _sync.make_threading_shim(Thread=_sync.SyncThread))
    sms_user = make_user(db, preferred_channel="sms")
    im_user = make_user(db, preferred_channel="imessage")
    assert typing_indicator.typing_start(sms_user.id) is False
    assert typing_indicator.typing_start(im_user.id) is True


# ── 5. the split is logged per turn ──────────────────────────────────────────

def test_turn_latency_line_is_logged_with_the_split(db, driver, anthropic_stub, caplog, monkeypatch):
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    user = make_user(db)
    anthropic_stub.push("bet, legs it is")
    with caplog.at_level(logging.INFO):
        driver.send(user, "whats my workout today?")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TURN_LATENCY")]
    assert len(lines) == 1, lines
    line = lines[0]
    for field in ("wait_s=", "prelude_s=", "loop_s=", "send_s=", "total_s="):
        assert field in line, line
    assert f"user={user.id}" in line


def test_agent_loop_timing_line_splits_context_from_model(db, driver, anthropic_stub, caplog, monkeypatch):
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    user = make_user(db)
    anthropic_stub.push("bet")
    with caplog.at_level(logging.INFO):
        driver.send(user, "whats my workout today?")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("AGENT_LOOP_TIMING")]
    assert len(lines) == 1, lines
    assert "context_ms=" in lines[0] and "model_ms=" in lines[0] and "calls=1" in lines[0]
