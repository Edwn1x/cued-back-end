"""
Alex (user 42) thread watch, 2026-11-20 → 11-21 — three fixes:

1. TAPBACK PREFERENCE. 22:01 PT: "Instead of thumbs up bro just like the message frr" →
   the model reacted ❤️ and said "hearts from now on"; the coaching summary recorded it;
   and 3 of the next 4 reactions were 👍 again — every one a code-side tapback (closing-ack
   suppression, sentinel backfill, continuation react) with "like" hardcoded. The ask is
   captured in code from the text and every code-side react reads it.

2. PHOTO MID-FLIGHT. 15:41:58 "Ima drink this instead sangria" → flushed; 15:42:15 the label
   photo landed while the model was in flight → NOT a continuation ("a photo keeps its
   hold") → the reply at :19 guessed 180 cal / 45g from memory; the photo's own turn only
   came 50s later and only after "I sent the macros". A photo landing while a turn is in
   flight (or seconds after a flush) is a continuation too: a short band, it waits for the
   reply it continues, and the model is told to read it against what it just said.

3. PAST DAYS. "have I been gradually losing weight?" → "a couple days were over 2000 (one
   around 3400)" — no day in 30 days was over 2820. The model recalled, because context
   carried today + yesterday only. The last 7 logged days ride as code-computed totals.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

import pytest

import config
import message_buffer
from tests._sync import PENDING_TIMERS
from tests.factories import make_user
from tests.tier1.test_reactions_and_replies import imessage_on, sidecar, _inbound, _rows, SECRET  # noqa: F401 — fixtures
from tests.tier1.test_reply_latency import clock, _clean_buffer  # noqa: F401 — fixtures

IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}
ALEX_ASK = "Instead of thumbs up bro just like the message frr"


def _post_imessage(client, phone, text, msg_id):
    payload = {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
               "service": "iMessage", "line_phone": "+16287895365", "timestamp": "2026-11-21T05:01:00.000Z",
               "attachments": []}
    return client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET},
                       content_type="application/json")


def _seed_meal(db, user_id, desc, cal, pro, when):
    from models import Meal
    m = Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro, carbs_g=20, fat_g=6,
             source="text", log_type="user_reported", eaten_at=when, logged_at=when)
    db.add(m)
    db.commit()
    return m.id


# ── 1. tapback preference ─────────────────────────────────────────────────────

def test_tapback_preference_is_read_from_their_words():
    from sms import tapback_preference_from_text
    assert tapback_preference_from_text(ALEX_ASK) == "love"                     # the live ask
    assert tapback_preference_from_text("can u heart my messages instead") == "love"
    assert tapback_preference_from_text("use ❤️ not 👍") == "love"
    assert tapback_preference_from_text("no more thumbs up pls") == "love"
    assert tapback_preference_from_text("thumbs up is fine actually") == "like"
    assert tapback_preference_from_text("stop with the hearts lol") == "like"
    for none in ("i like the message u sent", "thumbs up on the bench pr", "heart rate was 150", "ok", ""):
        assert tapback_preference_from_text(none) is None, none


def test_the_ask_is_captured_in_code_and_the_closing_ack_reacts_with_it(db, imessage_on, sidecar, client, anthropic_stub, sms_capture):
    """Deterministic: the webhook stores the preference off the text; the next bare 'Alr'
    (closing-ack suppression — no model) gets ❤️, not 👍."""
    from models import User
    from sms import _log_message, preferred_tapback
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert preferred_tapback(user.id) == "like"
    anthropic_stub.reply_with(lambda kw: "alr bro, hearts from now on")
    r = _post_imessage(client, user.phone, ALEX_ASK, "spc-msg-ask")
    assert r.status_code == 200
    db.expire_all()
    assert db.get(User, user.id).preferred_tapback == "love"
    assert preferred_tapback(user.id) == "love"

    _log_message(user.id, "with it ur at 1385 cal, 109g. lmk if u drink it", "freeform", channel="imessage",
                 provider_sid="spc-prior", delivery_status="sent")
    sidecar.clear()
    r = _post_imessage(client, user.phone, "Alr", "spc-msg-alr")
    assert r.status_code == 200
    reacts = [j for r_, j in sidecar if r_ == "react"]
    assert reacts == [{"phone": user.phone, "message_id": "spc-msg-alr", "emoji": "love"}], reacts
    out = [m for m in _rows(db, user) if m.message_type == "reaction"]
    assert out and "❤️" in out[-1].body


def test_every_code_side_react_reads_the_preference(db, imessage_on, sidecar, anthropic_stub, sms_capture, monkeypatch):
    """The loop's sentinel backfill + continuation react, app's react helper, onboarding's thumbs."""
    import app
    from models import User
    user = make_user(db, preferred_channel="imessage", onboarding_step=3, preferred_tapback="love")
    monkeypatch.setattr(config, "SILENT_SENTINEL_BACKFILL_ENABLED", True)
    _inbound(db, user, "Ight", "spc-msg-ight")
    anthropic_stub.reply_with(lambda kw: "[silent]")
    app.process_buffered_message(user.id, "Ight", "freeform")
    assert [j["emoji"] for r_, j in sidecar if r_ == "react"] == ["love"]

    sidecar.clear()
    app._react_to_latest_inbound(user.id, "like")          # the default name = "whatever they asked for"
    assert [j["emoji"] for r_, j in sidecar if r_ == "react"] == ["love"]

    sidecar.clear()
    app._react_to_latest_inbound(user.id, "emphasize")     # a deliberate non-default tapback stays
    assert [j["emoji"] for r_, j in sidecar if r_ == "react"] == ["emphasize"]

    sidecar.clear()
    from onboarding_agent import _react_thumbs
    _react_thumbs(user.id)
    assert [j["emoji"] for r_, j in sidecar if r_ == "react"] == ["love"]

    # Back to thumbs on request.
    db.get(User, user.id).preferred_tapback = "like"
    db.commit()
    sidecar.clear()
    app._react_to_latest_inbound(user.id, "like")
    assert [j["emoji"] for r_, j in sidecar if r_ == "react"] == ["like"]


# ── 2. a photo landing mid-flight is a continuation ───────────────────────────

def test_photo_landing_during_an_inflight_turn_is_a_continuation_with_a_short_band(clock, monkeypatch):
    monkeypatch.setattr(config, "PHOTO_CONTINUATION_BUFFER_S", (6, 6))
    message_buffer._in_flight["+15550109999"] = threading.Event()        # the text turn is being answered
    message_buffer.buffer_message("+15550109999", "", 1, "freeform", image_url=IMG, images=[IMG],
                                  process_callback=lambda *a, **k: None,
                                  delay_override=(45, 45), arrived_at=clock.monotonic())
    t = PENDING_TIMERS.get("+15550109999")
    assert t is not None and t.interval == pytest.approx(6.0, abs=0.01)
    assert message_buffer._buffers["+15550109999"]["continuation"] is True


def test_photo_landing_seconds_after_a_flush_is_a_continuation_too(clock, monkeypatch):
    monkeypatch.setattr(config, "PHOTO_CONTINUATION_BUFFER_S", (6, 6))
    monkeypatch.setattr(config, "BUFFER_JOIN_WINDOW_S", 2.0)
    message_buffer._last_flush["+15550109999"] = time.monotonic()
    message_buffer.buffer_message("+15550109999", "", 1, "freeform", image_url=IMG, images=[IMG],
                                  process_callback=lambda *a, **k: None,
                                  delay_override=(45, 45), arrived_at=clock.monotonic())
    assert PENDING_TIMERS.get("+15550109999").interval == pytest.approx(6.0, abs=0.01)
    assert message_buffer._buffers["+15550109999"]["continuation"] is True


def test_a_fresh_photo_keeps_its_hold_and_the_flag_restores_the_old_rule(clock, monkeypatch):
    # Nothing in flight, nothing just flushed → the captionless-photo hold, as before.
    message_buffer.buffer_message("+15550109999", "", 1, "freeform", image_url=IMG, images=[IMG],
                                  process_callback=lambda *a, **k: None,
                                  delay_override=(45, 45), arrived_at=clock.monotonic())
    assert PENDING_TIMERS.get("+15550109999").interval == pytest.approx(45.0, abs=0.01)
    assert message_buffer._buffers["+15550109999"]["continuation"] is False
    message_buffer._buffers.clear()
    # Flag off → a mid-flight photo is never a continuation (the 2026-11-20 rule).
    monkeypatch.setattr(config, "PHOTO_CONTINUATION_ENABLED", False)
    message_buffer._in_flight["+15550109999"] = threading.Event()
    message_buffer.buffer_message("+15550109999", "", 1, "freeform", image_url=IMG, images=[IMG],
                                  process_callback=lambda *a, **k: None,
                                  delay_override=(45, 45), arrived_at=clock.monotonic())
    assert PENDING_TIMERS.get("+15550109999").interval == pytest.approx(45.0, abs=0.01)
    assert message_buffer._buffers["+15550109999"]["continuation"] is False


def test_photo_continuation_flush_waits_for_the_inflight_reply_and_carries_the_image(monkeypatch):
    """End to end in the buffer: the photo's flush blocks on the in-flight event, then the
    callback gets continuation=True AND the image."""
    monkeypatch.setattr(config, "PHOTO_CONTINUATION_BUFFER_S", (1, 1))
    phone = "+15550109998"
    prior = threading.Event()
    message_buffer._in_flight[phone] = prior
    got = {}

    def cb(user_id, body, mtype, image_url=None, images=None, continuation=False, timing=None):
        got.update(user_id=user_id, body=body, image_url=image_url, images=images, continuation=continuation,
                   prior_done=prior.is_set())
    message_buffer.buffer_message(phone, "", 1, "freeform", image_url=IMG, images=[IMG], process_callback=cb,
                                  delay_override=(45, 45))
    tok = message_buffer._buffers[phone]["token"]
    done = threading.Thread(target=message_buffer._flush_buffer, args=(phone, cb, tok), daemon=True)
    done.start()
    time.sleep(0.2)
    assert not got, "the flush must wait for the in-flight turn"
    prior.set()
    done.join(5)
    assert got and got["continuation"] is True and got["images"] == [IMG] and got["image_url"] == IMG
    assert got["prior_done"] is True


def test_continuation_block_tells_the_model_to_read_the_photo_against_what_it_said(db):
    from agent_loop import _continuation_block, CONTINUATION_HDR
    from models import get_session, User, Message
    user = make_user(db, onboarding_step=3, name="Alex")
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="Ima drink this instead sangria", message_type="freeform"))
        s.add(Message(user_id=user.id, direction="out", body="the señorial soda? that's about 180 cal and 45g sugar",
                      message_type="freeform"))
        s.commit()
        blk = _continuation_block(s.get(User, user.id), "", s, has_image=True)
    finally:
        s.close()
    assert blk and CONTINUATION_HDR in blk
    assert "photo" in blk.lower() and "label" in blk.lower()
    assert "180 cal" in blk, "quotes the reply it continues"
    assert '""' not in blk, "an empty caption is described, not quoted as empty"


def test_loop_passes_the_photo_flag_into_the_continuation_block(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_loop import run_agent_loop, CONTINUATION_HDR
    from models import get_session, User, Message
    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    user = make_user(db, onboarding_step=3, name="Alex")
    seen = []

    def _handler(kw):
        if kw.get("tools"):
            seen.append(json.dumps(kw.get("system"), default=str, ensure_ascii=False)
                        + json.dumps(kw["messages"][0]["content"], default=str, ensure_ascii=False))
        return "mb, the label says 100 cal and 25g sugar per glass"
    anthropic_stub.reply_with(_handler)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="that's about 180 cal and 45g sugar", message_type="freeform"))
        s.commit()
        run_agent_loop(s.get(User, user.id), "", "freeform", image_data=IMG, image_data_list=[IMG], continuation=True)
    finally:
        s.close()
    assert seen and CONTINUATION_HDR in seen[0] and "label" in seen[0].lower()
    assert '"type": "image"' in seen[0], "the image rides with the continuation turn"


# ── 3. past days ride as code-computed totals ─────────────────────────────────

def test_context_carries_the_last_seven_logged_days_as_totals(db):
    from agent_loop import build_loop_context
    from models import get_session, User
    from timefmt import local_day_bounds
    user = make_user(db, onboarding_step=3, name="Alex", calorie_target=2000)
    start, _ = local_day_bounds(user)
    _seed_meal(db, user.id, "chipotle bowl", 660, 47, start + timedelta(hours=5))                 # today
    _seed_meal(db, user.id, "ham sandwiches", 590, 40, start - timedelta(hours=3))                  # yesterday
    _seed_meal(db, user.id, "fairlife shake", 230, 42, start - timedelta(hours=2))                  # yesterday
    _seed_meal(db, user.id, "chilaquiles + eggs", 1075, 42, start - timedelta(days=2, hours=10))    # 2 days ago
    _seed_meal(db, user.id, "pie", 400, 4, start - timedelta(days=2, hours=8))                      # 2 days ago
    _seed_meal(db, user.id, "in n out", 460, 33, start - timedelta(days=6, hours=1))                # 6 days ago
    _seed_meal(db, user.id, "old pizza", 1200, 30, start - timedelta(days=9))                       # out of window
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## PAST DAYS" in ctx
    blk = ctx.split("## PAST DAYS", 1)[1].split("\n## ", 1)[0]
    lines = [ln for ln in blk.splitlines() if "cal" in ln and "protein" in ln]
    assert len(lines) == 3, blk                       # yesterday, 2 days ago, 6 days ago — nothing else
    assert any("820" in ln and "82g" in ln and "2 meals" in ln for ln in lines), blk     # yesterday summed
    assert any("1475" in ln and "46g" in ln for ln in lines), blk                          # 2 days ago
    assert any("460" in ln and "33g" in ln for ln in lines), blk                           # 6 days ago
    assert "1200" not in blk and "660" not in blk, "9 days ago and today are not past-days rows"
    assert "unknown" in blk.lower() and "never" in blk.lower(), "days without a log are unknown, not zero; never recall a total"
    assert "2000" in blk, "the target rides so over/under reads off the block"


def test_past_days_block_is_absent_without_logged_days_and_off_switchable(db, monkeypatch):
    from agent_loop import build_loop_context
    from models import get_session, User
    from timefmt import local_day_bounds
    user = make_user(db, onboarding_step=3, name="Alex")
    s = get_session()
    try:
        assert "## PAST DAYS" not in build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    start, _ = local_day_bounds(user)
    _seed_meal(db, user.id, "pie", 400, 4, start - timedelta(days=2))
    monkeypatch.setattr(config, "PAST_DAYS_TOTALS_ENABLED", False)
    s = get_session()
    try:
        assert "## PAST DAYS" not in build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
