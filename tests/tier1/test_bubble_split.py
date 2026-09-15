"""
Voice rewrite (2026-09-15): a coach reply with `---` sends as separate bubbles on
BOTH channels; typing-speed delay between them; cap 3; failover intact.
"""

from __future__ import annotations

import pytest

from tests.factories import make_user


def test_split_bubbles_cap_and_fold():
    from sms import split_bubbles
    assert split_bubbles("a --- b") == ["a", "b"]
    assert split_bubbles("just one") == ["just one"]
    assert split_bubbles("a --- b --- c --- d --- e") == ["a", "b", "c\n\nd\n\ne"]   # cap 3, extras fold
    assert split_bubbles("  ---  a  ---  ") == ["a"]                                   # empty parts dropped


def test_bubble_delay_scales_with_length():
    from sms import bubble_delay
    assert bubble_delay("go") == 1.0
    assert bubble_delay("x" * 500) == 3.5
    assert 1.0 < bubble_delay("x" * 50) < 3.5


def test_imessage_sends_each_bubble_separately(db, monkeypatch):
    import config, sms
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://s.test:8080")
    monkeypatch.setattr(sms.time, "sleep", lambda *_: None)
    sent = []
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body, reply_to=None: (sent.append((body, reply_to)), f"pm-{len(sent)}")[1])
    user = make_user(db, preferred_channel="imessage")
    sid = sms.send_sms(user.phone, "damn ok --- u eat yet", user_id=user.id, message_type="freeform", reply_to_sid="spc-q")
    assert [b for b, _ in sent] == ["damn ok", "u eat yet"]
    assert sent[0][1] == "spc-q" and sent[1][1] is None       # threaded on the first bubble only
    assert sid == "pm-1"                                        # returns the first bubble's id
    from models import get_session, Message
    s = get_session()
    try:
        rows = s.query(Message).filter_by(user_id=user.id, direction="out", channel="imessage").order_by(Message.id).all()
        assert [r.body for r in rows] == ["damn ok", "u eat yet"] and all(r.delivery_status == "sent" for r in rows)
    finally:
        s.close()


def test_imessage_first_bubble_failure_falls_the_whole_message_over_to_sms(db, monkeypatch, sms_capture):
    import config, sms
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://s.test:8080")

    def _boom(phone, body, reply_to=None):
        raise RuntimeError("sidecar /send 502: down")
    monkeypatch.setattr(sms, "_send_imessage", _boom)
    from models import User
    user = make_user(db, preferred_channel="imessage")
    sms.send_sms(user.phone, "bubble one --- bubble two", user_id=user.id, message_type="freeform")
    # SMS fallover carries the FULL message (bubbles rejoined by the SMS splitter)
    assert [b for _, b in sms_capture] == ["bubble one", "bubble two"]
    db.expire_all()
    assert db.get(User, user.id).channel_failed_over is True


def test_imessage_later_bubble_failure_keeps_the_earlier_ones(db, monkeypatch, sms_capture):
    import config, sms
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://s.test:8080")
    monkeypatch.setattr(sms.time, "sleep", lambda *_: None)
    calls = []

    def _second_fails(phone, body, reply_to=None):
        calls.append(body)
        if len(calls) == 2:
            raise RuntimeError("bubble 2 down")
        return f"pm-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _second_fails)
    from models import User
    user = make_user(db, preferred_channel="imessage")
    sid = sms.send_sms(user.phone, "one --- two", user_id=user.id, message_type="freeform")
    assert sid == "pm-1" and sms_capture == []                       # no SMS fallover on a late bubble
    db.expire_all()
    assert db.get(User, user.id).channel_failed_over is False        # breaker not tripped
