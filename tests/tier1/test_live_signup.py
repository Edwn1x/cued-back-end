"""
Live sign-ups from the site (founder, 2026-10-15): "switch our frontend site from
waitlist to live signups and straight to the product. the new button reads 'text
cued'; the user still completes the onboarding popup and then instead of the
waitlist text they get the first actual text from the coach."

Same route (/waitlist — the site's chat posts there), new outcome under
LIVE_SIGNUPS_ENABLED (default on): the row is live (no pending marker, activated_at
stamped), and the coach's first text follows /signup's iMessage-first rules —
  1. link minted → hook DEFERRED; their "hey cued" tap sends it blue as the reply
  2. "I don't have an iPhone" → /signup/channel → hook by SMS
  3. no link (Photon cap/off) → hook now via start_onboarding
A leftover pending waitlister who signs up again is activated on the spot. The
2026-09-19 waitlist behaviour is the rollback (test_waitlist_profile.py, flag off).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest

from tests.factories import make_user

SECRET = "test-internal-secret"


@pytest.fixture(autouse=True)
def live_mode(monkeypatch):
    import config
    monkeypatch.setattr(config, "LIVE_SIGNUPS_ENABLED", True)


@pytest.fixture
def photon_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", True)
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_ID", "proj")
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_SECRET", "sec")


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


@pytest.fixture
def sidecar_ok(monkeypatch):
    import sms
    calls: list = []

    def _fake(phone, body, reply_to=None):
        calls.append((phone, body))
        return f"photon-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
    return calls


@pytest.fixture
def inline_hook(monkeypatch):
    """start_onboarding is a real daemon thread; run its body inline for determinism."""
    import app as appmod
    from onboarding_agent import send_onboarding_hook
    monkeypatch.setattr(appmod, "start_onboarding",
                        lambda user, reason="start_onboarding": send_onboarding_hook(user.id, reason=reason))


def _signup(client, phone, **over):
    body = {"name": "Nate", "full_name": "Nate Ruiz", "phone": phone, "email": None, "source": "hero",
            "ts": "2026-10-15T18:00:00.000Z", "timezone": "America/Los_Angeles",
            "age": 21, "gender": "male", "goal": ["fat_loss", "strength"],
            "biggest_obstacle": "consistency", "experience": "intermediate",
            "equipment": "full_gym", "sms_consent": True, "height_cm": 180, "weight_kg": 75,
            "workout_days": 4, "workout_time": "evening", "diet": "omnivore"}
    body.update(over)
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


def _outbound(db, user_id):
    from models import Message
    db.expire_all()
    return db.query(Message).filter(Message.user_id == user_id, Message.direction == "out").order_by(Message.id).all()


def _post_imessage(client, phone, text, msg_id):
    payload = {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
               "service": "iMessage", "line_phone": "+16287895365", "timestamp": "2026-10-15T18:27:00.000Z",
               "attachments": []}
    return client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET},
                       content_type="application/json")


def _user(db, phone):
    from models import User
    db.expire_all()
    return db.query(User).filter(User.phone == phone).one()


# ─── the row is live, the profile lands, nothing is pending ──────────────────

def test_live_signup_creates_an_active_row_with_the_profile(db, client, inline_hook, sms_capture, caplog):
    import photon
    with caplog.at_level(logging.INFO):
        code, data = _signup(client, "5105550401")
    assert code == 200 and data["status"] == "ok" and data["live"] is True, data
    u = _user(db, "+15105550401")
    assert u.waitlist_status is None, "live: no pending marker"
    assert u.activated_at is not None, "activated the moment they sign up (admin Adoption counts this)"
    assert (u.name, u.full_name, u.age, u.goal, u.experience, u.equipment) == \
        ("Nate", "Nate Ruiz", 21, "fat_loss,strength", "intermediate", "full_gym")
    assert (u.height_ft, u.height_in, str(u.workout_days), u.diet) == (5, 11, "4", "omnivore")
    assert any("SIGNUP_LIVE_NEW" in r.getMessage() for r in caplog.records)
    assert not any("WAITLIST_NEW" in r.getMessage() for r in caplog.records)


def test_no_link_sends_the_coachs_first_text_immediately(db, client, inline_hook, sms_capture, monkeypatch):
    import photon
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)
    code, data = _signup(client, "5105550402")
    assert code == 200 and data["live"] is True and data["hook_deferred"] is False and data["imessage_link"] is None
    u = _user(db, "+15105550402")
    assert u.onboarding_step == 1, "the hook moved them to step 1"
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type) for m in rows] == [("sms", "onboarding")]
    assert rows[0].body.startswith("hey Nate, it's cued. ur in. quick setup before i can actually help")
    assert "spot" not in rows[0].body, "the plain signup hook, not the waitlist-activation one"
    assert len(sms_capture) == 1


def test_with_a_link_the_hook_waits_for_their_first_blue_text(db, client, photon_on, imessage_on, sidecar_ok,
                                                             monkeypatch, sms_capture, caplog):
    import photon
    from tests._sync import PENDING_TIMERS
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "ph-l1")
    with caplog.at_level(logging.INFO):
        code, data = _signup(client, "5105550403")
    assert code == 200 and data["live"] is True and data["hook_deferred"] is True
    assert "/users/ph-l1/redirect" in data["imessage_link"]
    u = _user(db, "+15105550403")
    assert u.waitlist_status is None and u.preferred_channel == "imessage" and (u.onboarding_step or 0) == 0
    assert _outbound(db, u.id) == [] and sms_capture == [], "deferred: nothing yet"
    assert any("ONBOARDING_HOOK_DEFERRED" in r.getMessage() for r in caplog.records)

    # Their "hey cued" tap: NOT the waitlist holding line — the coach's first text, blue.
    # (The inbound goes through the buffer like any live user's text; fire its timer.)
    with caplog.at_level(logging.INFO):
        assert _post_imessage(client, u.phone, "hey cued", "live-optin-1").status_code == 200
        assert not any("WAITLIST_INBOUND_HELD" in r.getMessage() for r in caplog.records)
        t = PENDING_TIMERS.pop(u.phone, None)
        assert t is not None, "buffered like a live user, not held at the waitlist gate"
        t.fire()
    u = _user(db, "+15105550403")
    assert u.onboarding_step == 1 and u.imessage_opted_in_at is not None
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type, m.delivery_status) for m in rows] == [("imessage", "onboarding", "sent")]
    assert rows[0].body.startswith("hey Nate, it's cued. ur in.")
    assert "on the list" not in rows[0].body and not any(m.message_type == "waitlist_hold" for m in rows)
    assert len(sidecar_ok) == 1 and sms_capture == []
    assert any("ONBOARDING_HOOK_ON_FIRST_TEXT" in r.getMessage() for r in caplog.records)


def test_no_iphone_sends_the_hook_by_sms_right_away(db, client, photon_on, imessage_on, monkeypatch, sms_capture):
    import photon
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "ph-l2")
    code, data = _signup(client, "5105550404")
    assert code == 200 and data["hook_deferred"] is True
    r = client.post("/signup/channel", data=json.dumps({"phone": "5105550404", "channel": "sms"}),
                    content_type="application/json")
    d = r.get_json()
    assert r.status_code == 200 and d["status"] == "ok" and d["hook_sent"] is True and d["waitlist"] is False
    u = _user(db, "+15105550404")
    assert u.preferred_channel == "sms" and u.onboarding_step == 1
    assert [(m.channel, m.message_type) for m in _outbound(db, u.id)] == [("sms", "onboarding")]
    assert len(sms_capture) == 1 and "ur in" in sms_capture[0][1]


def test_fallback_sweep_picks_up_a_live_signup_who_never_chose(db, client, photon_on, monkeypatch, sms_capture):
    """Deferred, then silence: the scheduler's sweep sends the hook by SMS — the
    same rule as /signup. (The waitlist version refused pending users; live rows
    have no marker, so they qualify.)"""
    from datetime import timedelta
    from models import User, get_session
    from onboarding_agent import send_fallback_hooks
    import photon
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "ph-l3")
    code, data = _signup(client, "5105550405")
    assert code == 200 and data["hook_deferred"] is True
    u = _user(db, "+15105550405")
    s = get_session()
    try:
        s.get(User, u.id).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        s.commit()
    finally:
        s.close()
    assert send_fallback_hooks() == 1
    u = _user(db, "+15105550405")
    assert u.onboarding_step == 1
    assert [(m.channel, m.message_type) for m in _outbound(db, u.id)] == [("sms", "onboarding")]
    assert len(sms_capture) == 1 and "ur in" in sms_capture[0][1]


# ─── repeats ──────────────────────────────────────────────────────────────────

def test_active_user_signing_up_again_gets_exists_and_the_link_and_no_text(db, client, sms_capture):
    make_user(db, phone="+15105550406", name="First", onboarding_step=3, photon_user_id="ph-l6",
              preferred_channel="imessage")
    code, data = _signup(client, "5105550406", name="Second")
    assert code == 200 and data["status"] == "exists" and data["live"] is True
    assert "/users/ph-l6/redirect" in data["imessage_link"]
    u = _user(db, "+15105550406")
    assert u.name == "First" and u.onboarding_step == 3, "an unauthenticated route never rewrites a row"
    assert _outbound(db, u.id) == [] and sms_capture == []


def test_leftover_waitlister_signing_up_again_is_activated_on_the_spot(db, client, imessage_on, sidecar_ok,
                                                                       inline_hook, sms_capture, caplog):
    """A pending row from the waitlist days: the site is live now, so their re-submit
    is the start — same as the admin button (status cleared, activated_at, the
    'ur spot's open' hook, blue because they already texted the line)."""
    pending = make_user(db, phone="+15105550407", name="Nate", onboarding_step=0, waitlist_status="pending",
                        preferred_channel="imessage", photon_user_id="ph-l7",
                        imessage_opted_in_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        goal="fat_loss", wake_time=None, sleep_time=None)
    with caplog.at_level(logging.INFO):
        code, data = _signup(client, "5105550407", name="Other", goal=["endurance"])
    assert code == 200 and data["status"] == "ok" and data["live"] is True and data["activated"] is True
    assert data["hook_deferred"] is False
    u = _user(db, "+15105550407")
    assert u.waitlist_status is None and u.activated_at is not None and u.onboarding_step == 1
    assert u.name == "Nate" and u.goal == "fat_loss", "activated, not rewritten"
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type, m.delivery_status) for m in rows] == [("imessage", "onboarding", "sent")]
    assert rows[0].body.startswith("hey Nate, it's cued. ur spot's open.")
    assert sms_capture == []
    assert any("SIGNUP_LIVE_ACTIVATED_PENDING" in r.getMessage() for r in caplog.records)


# ─── rollback ─────────────────────────────────────────────────────────────────

def test_flag_off_is_the_old_waitlist(db, client, monkeypatch, sms_capture):
    import config
    import photon
    monkeypatch.setattr(config, "LIVE_SIGNUPS_ENABLED", False)
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)
    code, data = _signup(client, "5105550408")
    assert code == 200 and data["status"] == "ok" and "live" not in data
    u = _user(db, "+15105550408")
    assert u.waitlist_status == "pending" and u.activated_at is None and (u.onboarding_step or 0) == 0
    assert _outbound(db, u.id) == [] and sms_capture == []
