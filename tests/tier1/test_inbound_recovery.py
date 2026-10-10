"""A deploy can't silently drop a buffered turn (live 2026-10-08 13:44 PT, user 48: a photo
landed 2 min before a container swap; its timer died with the process, no reply ever came).

  1. message_buffer keeps an inbound_pending row per armed turn and clears it at flush.
  2. drain_all (the SIGTERM path) flushes every pending buffer synchronously.
  3. inbound_recovery.recover_orphans on boot: an old TEXT marker replays the stored inbound
     rows through the pipeline; a PHOTO marker gets one honest "send it again" line; a fresh
     marker is left alone; a stale one is dropped.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

import config
import message_buffer
from tests.factories import make_user


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "INBOUND_RECOVERY_ENABLED", True)
    monkeypatch.setattr(config, "INBOUND_RECOVERY_MIN_AGE_S", 90)
    monkeypatch.setattr(config, "INBOUND_RECOVERY_MAX_AGE_MIN", 15)
    message_buffer._buffers.clear()
    yield
    for ph in list(message_buffer._buffers):
        try:
            message_buffer._buffers[ph]["timer"].cancel()
        except Exception:
            pass
    message_buffer._buffers.clear()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _inbound(db, user, body, *, ago_s=0):
    from models import Message
    m = Message(user_id=user.id, direction="in", body=body, message_type="freeform",
                created_at=_utcnow() - timedelta(seconds=ago_s))
    db.add(m); db.commit()
    return m.id


def _markers(db):
    from models import InboundPending
    db.expire_all()
    return {r.phone: r for r in db.query(InboundPending).all()}


def _cb(calls):
    def cb(user_id, body, message_type, image_url, images=None):
        calls.append({"user_id": user_id, "body": body, "type": message_type, "image_url": image_url, "images": list(images or [])})
    return cb


# ── 1. the marker's lifecycle ────────────────────────────────────────────────

def test_arming_the_buffer_marks_pending_and_flushing_clears_it(db):
    u = make_user(db, phone="+15550001001")
    mid = _inbound(db, u, "ate a burger")
    calls = []
    message_buffer.buffer_message(u.phone, "ate a burger", u.id, "freeform", process_callback=_cb(calls), delay_override=(60, 60))
    mk = _markers(db)
    assert u.phone in mk and mk[u.phone].user_id == u.id and mk[u.phone].first_message_id == mid and mk[u.phone].has_image is False
    # a photo joining the turn flips has_image; the first id stays
    _inbound(db, u, "[image attached]")
    message_buffer.buffer_message(u.phone, "", u.id, "freeform", image_url={"x": 1}, images=[{"x": 1}],
                                  process_callback=_cb(calls), delay_override=(60, 60))
    mk = _markers(db)
    assert mk[u.phone].has_image is True and mk[u.phone].first_message_id == mid
    # flush → marker gone, callback ran once with both parts
    tok = message_buffer._buffers[u.phone]["token"]
    message_buffer._buffers[u.phone]["timer"].cancel()
    message_buffer._flush_buffer(u.phone, _cb(calls), tok)
    assert _markers(db) == {} and len(calls) == 1 and calls[0]["body"] == "ate a burger"


def test_flag_off_writes_no_marker(db, monkeypatch):
    monkeypatch.setattr(config, "INBOUND_RECOVERY_ENABLED", False)
    u = make_user(db, phone="+15550001002")
    message_buffer.buffer_message(u.phone, "hey", u.id, "freeform", process_callback=_cb([]), delay_override=(60, 60))
    assert _markers(db) == {}


# ── 2. the SIGTERM drain ─────────────────────────────────────────────────────

def test_drain_all_flushes_every_pending_turn_now(db):
    a = make_user(db, phone="+15550001003"); b = make_user(db, phone="+15550001004")
    _inbound(db, a, "one"); _inbound(db, b, "two")
    calls = []
    message_buffer.buffer_message(a.phone, "one", a.id, "freeform", process_callback=_cb(calls), delay_override=(60, 60))
    message_buffer.buffer_message(b.phone, "two", b.id, "freeform", process_callback=_cb(calls), delay_override=(60, 60))
    assert len(_markers(db)) == 2
    n = message_buffer.drain_all("test")
    assert n == 2 and sorted(c["body"] for c in calls) == ["one", "two"]
    assert message_buffer._buffers == {} and _markers(db) == {}
    assert message_buffer.drain_all("again") == 0


# ── 3. boot-time recovery ────────────────────────────────────────────────────

def _marker(db, user, *, first_id, has_image, age_s):
    from models import InboundPending
    t = _utcnow() - timedelta(seconds=age_s)
    db.add(InboundPending(phone=user.phone, user_id=user.id, first_message_id=first_id, has_image=has_image,
                          created_at=t, updated_at=t))
    db.commit()


def test_an_old_text_marker_replays_the_stored_rows_once(db, sms_capture):
    from inbound_recovery import recover_orphans
    u = make_user(db, phone="+15550001005")
    _inbound(db, u, "earlier unrelated", ago_s=900)
    first = _inbound(db, u, "ate 6oz of raspberries", ago_s=200)
    _inbound(db, u, "and a banana", ago_s=195)
    _marker(db, u, first_id=first, has_image=False, age_s=190)
    calls = []
    out = recover_orphans(_cb(calls))
    assert out == {"replayed": 1, "photo_notes": 0, "dropped": 0, "fresh": 0}
    assert len(calls) == 1 and calls[0]["user_id"] == u.id and calls[0]["body"] == "ate 6oz of raspberries\nand a banana"
    assert calls[0]["image_url"] is None and calls[0]["images"] == []
    assert _markers(db) == {} and sms_capture == []
    assert recover_orphans(_cb(calls)) == {"replayed": 0, "photo_notes": 0, "dropped": 0, "fresh": 0}   # claimed once


def test_an_old_photo_marker_asks_them_to_resend_instead_of_replaying(db, sms_capture):
    from inbound_recovery import recover_orphans, PHOTO_RECOVERY_LINE
    from models import Message
    u = make_user(db, phone="+15550001006")
    first = _inbound(db, u, "[image attached]", ago_s=200)
    _marker(db, u, first_id=first, has_image=True, age_s=190)
    calls = []
    out = recover_orphans(_cb(calls))
    assert out["photo_notes"] == 1 and calls == []
    assert [b for _p, b in sms_capture] == [PHOTO_RECOVERY_LINE]
    db.expire_all()
    row = db.query(Message).filter_by(user_id=u.id, direction="out").order_by(Message.id.desc()).first()
    assert row.message_type == "inbound_recovery"
    assert _markers(db) == {}


def test_fresh_markers_are_left_and_stale_ones_dropped(db, sms_capture):
    from inbound_recovery import recover_orphans
    fresh = make_user(db, phone="+15550001007"); stale = make_user(db, phone="+15550001008")
    _marker(db, fresh, first_id=_inbound(db, fresh, "hi", ago_s=30), has_image=False, age_s=30)
    _marker(db, stale, first_id=_inbound(db, stale, "old", ago_s=2000), has_image=False, age_s=2000)
    calls = []
    out = recover_orphans(_cb(calls))
    assert out == {"replayed": 0, "photo_notes": 0, "dropped": 1, "fresh": 1} and calls == [] and sms_capture == []
    assert set(_markers(db)) == {fresh.phone}


def test_recovery_flag_off_is_inert(db, monkeypatch):
    from inbound_recovery import recover_orphans
    u = make_user(db, phone="+15550001009")
    _marker(db, u, first_id=_inbound(db, u, "x", ago_s=300), has_image=False, age_s=300)
    monkeypatch.setattr(config, "INBOUND_RECOVERY_ENABLED", False)
    calls = []
    assert recover_orphans(_cb(calls)) == {"replayed": 0, "photo_notes": 0, "dropped": 0, "fresh": 0} and calls == []
    assert set(_markers(db)) == {u.phone}


def test_app_wires_the_drain_and_the_recovery():
    import inspect, app
    src = inspect.getsource(app)
    assert "signal.signal(signal.SIGTERM, _on_term)" in src and "drain_all(\"sigterm\")" in src
    assert "_install_shutdown_drain()" in src and "_recover_inbound_in_background()" in src
    assert "recover_orphans(process_buffered_message)" in src



# ── the marker outlives a turn that dies mid-callback (live 2026-10-09 20:30 PT) ─────

def test_marker_is_cleared_only_after_the_callback_completes(db):
    u = make_user(db, phone="+15550001010")
    _inbound(db, u, "What do you think")
    order = []

    def cb(user_id, body, message_type, image_url, images=None, **kw):
        order.append(("during", len(_markers(db))))      # still marked while the turn runs
    message_buffer.buffer_message(u.phone, "What do you think", u.id, "freeform", process_callback=cb, delay_override=(60, 60))
    tok = message_buffer._buffers[u.phone]["token"]
    message_buffer._buffers[u.phone]["timer"].cancel()
    message_buffer._flush_buffer(u.phone, cb, tok)
    assert order == [("during", 1)] and _markers(db) == {}


def test_a_callback_that_dies_leaves_the_marker_for_the_next_boot(db):
    u = make_user(db, phone="+15550001011")
    first = _inbound(db, u, "What do you think")

    def boom(user_id, body, message_type, image_url, images=None, **kw):
        raise RuntimeError("killed mid-reply")
    message_buffer.buffer_message(u.phone, "What do you think", u.id, "freeform", process_callback=boom, delay_override=(60, 60))
    tok = message_buffer._buffers[u.phone]["token"]
    message_buffer._buffers[u.phone]["timer"].cancel()
    message_buffer._flush_buffer(u.phone, boom, tok)
    mk = _markers(db)
    assert u.phone in mk and mk[u.phone].first_message_id == first      # survives → replayable at boot


def test_boot_recovery_retries_while_a_marker_is_too_fresh(monkeypatch):
    """app._recover_inbound_in_background loops (bounded) while recover_orphans reports a
    fresh marker, so a drained-then-killed turn is picked up once it has aged."""
    import app, inbound_recovery, threading
    calls = []
    results = iter([{"replayed": 0, "photo_notes": 0, "dropped": 0, "fresh": 1},
                    {"replayed": 1, "photo_notes": 0, "dropped": 0, "fresh": 0}])
    monkeypatch.setattr(inbound_recovery, "recover_orphans", lambda cb: calls.append(1) or next(results))
    monkeypatch.setattr(config, "INBOUND_RECOVERY_MIN_AGE_S", 0)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    monkeypatch.setattr(threading, "Thread", lambda target=None, name=None, daemon=None: type("T", (), {"start": lambda self: target()})())
    app._recover_inbound_in_background()
    assert len(calls) == 2
