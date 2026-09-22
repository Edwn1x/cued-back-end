"""STOP opt-out flow (optout.handle_optout_flow) — deliberately high-friction to
prevent ACCIDENTAL opt-outs. Trigger is exact "STOP."/"UNSUBSCRIBE." (caps + period),
which sends a confirmation; only a second "STOP." opts out; "pause" pauses; any inbound
resumes. Flag-gated."""
from __future__ import annotations

import pytest

import config


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "STOP_OPTOUT_ENABLED", True)
    monkeypatch.setattr(config, "STOP_PAUSE_DAYS", 4)
    yield


def _run(db, user, body):
    from models import get_session, User
    from optout import handle_optout_flow
    s = get_session()
    try:
        u = s.get(User, user.id)
        terminal = handle_optout_flow(s, u, body, channel="imessage")
        s.commit()
        return terminal
    finally:
        s.close()


def _reget(db, uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


# ─── trigger precision (no accidental opt-outs) ───────────────────────────────

@pytest.mark.parametrize("body,triggers", [
    ("STOP.", True), ("UNSUBSCRIBE.", True), (" STOP. ", True),   # exact (trim ok)
    ("stop.", False), ("STOP", False), ("Stop.", False),          # wrong case / no period
    ("stop", False), ("stop asking me questions", False),          # casual
    ("STOP MESSAGING ME", False), ("please STOP.", False),        # not the whole message
])
def test_trigger_only_on_exact_caps_period(db, sms_capture, body, triggers):
    from tests.factories import make_user
    user = make_user(db)
    terminal = _run(db, user, body)
    u = _reget(db, user.id)
    if triggers:
        assert terminal is True and u.pending_optout_confirm is True
        assert any("reply STOP." in m for _p, m in sms_capture)   # confirmation sent
        assert u.opted_out is False                                # NOT opted out yet
    else:
        assert terminal is False and u.pending_optout_confirm is False and u.opted_out is False


# ─── confirmation resolution ──────────────────────────────────────────────────

def test_second_stop_confirms_optout(db, sms_capture):
    from tests.factories import make_user
    user = make_user(db)
    _run(db, user, "STOP.")                       # triggers confirmation
    assert _reget(db, user.id).pending_optout_confirm is True
    terminal = _run(db, user, "STOP.")            # confirm
    u = _reget(db, user.id)
    assert terminal is True and u.opted_out is True and u.pending_optout_confirm is False
    assert any("off the texts" in m for _p, m in sms_capture)     # goodbye


def test_pause_takes_days_off_not_optout(db, sms_capture):
    from tests.factories import make_user
    user = make_user(db)
    _run(db, user, "STOP.")
    terminal = _run(db, user, "pause")
    u = _reget(db, user.id)
    assert terminal is True and u.opted_out is False and u.quiet_until is not None
    assert any("going quiet" in m for _p, m in sms_capture)


def test_anything_else_cancels_confirmation_and_stays(db, sms_capture):
    from tests.factories import make_user
    user = make_user(db)
    _run(db, user, "STOP.")
    terminal = _run(db, user, "nvm i meant stop asking me stuff")
    u = _reget(db, user.id)
    assert terminal is False                       # not terminal — coach handles it
    assert u.opted_out is False and u.pending_optout_confirm is False


# ─── resume: any inbound brings them back ─────────────────────────────────────

def test_any_inbound_resumes_opted_out_user(db):
    from tests.factories import make_user
    from models import get_session, User
    user = make_user(db)
    s = get_session()
    try:
        s.get(User, user.id).opted_out = True
        s.commit()
    finally:
        s.close()
    terminal = _run(db, user, "yo what's good")    # any inbound
    u = _reget(db, user.id)
    assert terminal is False                        # not terminal — coach responds
    assert u.opted_out is False


# ─── suppression + flag off ───────────────────────────────────────────────────

def test_opted_out_suppresses_sends(db, monkeypatch, sms_capture):
    from tests.factories import make_user
    from models import get_session, User
    from sms import send_sms
    user = make_user(db, phone="+15105557777")
    s = get_session()
    try:
        s.get(User, user.id).opted_out = True
        s.commit()
    finally:
        s.close()
    send_sms(user.phone, "proactive nudge", user_id=user.id, message_type="heartbeat")
    assert not any("proactive nudge" in m for _p, m in sms_capture), "sent to an opted-out user"


def test_heartbeat_guardrail_blocks_opted_out(db):
    from tests.factories import make_user
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db)
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.opted_out = True
        s.commit()
        assert guardrail_reason(u, s) == "opted_out"
    finally:
        s.close()


def test_flag_off_is_noop(db, monkeypatch, sms_capture):
    from tests.factories import make_user
    monkeypatch.setattr(config, "STOP_OPTOUT_ENABLED", False)
    user = make_user(db)
    terminal = _run(db, user, "STOP.")
    assert terminal is False                        # no opt-out machinery when disabled
    assert _reget(db, user.id).pending_optout_confirm is False
