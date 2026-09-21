"""
Waitlist with a full profile + iMessage opt-in up front (founder, 2026-09-19).

The live site's waitlist becomes the chat sign-up (same body as /signup) posting to
/waitlist: the profile is stored on the pending row, the Photon line is provisioned so
the success screen can show "Text me on iMessage", and the coach gets a running start
at activation. The load-bearing piece is the GATE: a pending user who texts the line
gets one code-owned holding line and never reaches the buffer, the model, or the hook.
Their opt-in is recorded (imessage_opted_in_at) so activation goes blue first try.

See INVESTIGATION.md §2.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

SECRET = "test-internal-secret"


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
def sidecar_gated(monkeypatch):
    """Photon's shared-pool refusal for a number that hasn't texted its line."""
    import sms

    def _gate(phone, body, reply_to=None):
        raise RuntimeError("Target not allowed for this project")
    monkeypatch.setattr(sms, "_send_imessage", _gate)


@pytest.fixture
def model_forbidden(anthropic_stub):
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("model must not run for a waitlist user")))
    return anthropic_stub


def _waitlist(client, phone, **over):
    body = {"name": "Nate", "phone": phone, "email": None, "source": "hero",
            "ts": "2026-09-19T18:00:00.000Z", "timezone": "America/Los_Angeles",
            "age": 21, "gender": "male", "goal": ["fat_loss", "strength"],
            "biggest_obstacle": "consistency", "experience": "intermediate",
            "equipment": "full_gym", "sms_consent": True}
    body.update(over)
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


def _outbound(db, user_id):
    from models import Message
    db.expire_all()
    return db.query(Message).filter(Message.user_id == user_id, Message.direction == "out").order_by(Message.id).all()


def _post_imessage(client, phone, text, msg_id):
    payload = {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
               "service": "iMessage", "line_phone": "+16287895365", "timestamp": "2026-09-19T18:27:00.000Z",
               "attachments": []}
    return client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET},
                       content_type="application/json")


def _post_sms(client, phone, text, sid):
    return client.post("/webhook", data={"From": phone, "Body": text, "MessageSid": sid, "NumMedia": "0"})


def _pending(db, **over):
    kw = dict(onboarding_step=0, waitlist_status="pending", name="Nate", age=21, gender="male",
              goal="fat_loss,strength", experience="intermediate", equipment="full_gym",
              wake_time=None, sleep_time=None)
    kw.update(over)
    return make_user(db, **kw)


# ─── §2.1 /waitlist stores the profile, provisions the line, sends nothing ────

def test_waitlist_stores_the_profile_fields(db, client, sms_capture):
    from models import User
    code, data = _waitlist(client, "5105550301", age="21")
    assert code == 200 and data["status"] == "ok", data
    u = db.query(User).filter(User.phone == "+15105550301").one()
    assert u.waitlist_status == "pending" and (u.onboarding_step or 0) == 0
    assert (u.name, u.age, u.gender, u.goal, u.biggest_obstacle, u.experience, u.equipment) == \
        ("Nate", 21, "male", "fat_loss,strength", "consistency", "intermediate", "full_gym")
    assert u.signup_source == "hero" and u.user_timezone == "America/Los_Angeles"
    assert _outbound(db, u.id) == [] and sms_capture == [], "a waitlist sign-up never texts"


def test_waitlist_keeps_the_full_name_aside_and_addresses_by_first_name(db, client):
    """The chat asks for the full name (for us). `name` — what every hook template and
    trigger prompt injects — must be the first name only, whatever the client sent."""
    from models import User
    code, _ = _waitlist(client, "5105550308", name="Nate", full_name="Nate Ruiz")
    assert code == 200
    u = db.query(User).filter(User.phone == "+15105550308").one()
    assert (u.name, u.full_name) == ("Nate", "Nate Ruiz")

    # an older client sends the full name AS `name`: split it here, in code
    code, _ = _waitlist(client, "5105550309", name="  Sam   Okafor ")
    assert code == 200
    u = db.query(User).filter(User.phone == "+15105550309").one()
    assert (u.name, u.full_name) == ("Sam", "Sam   Okafor".strip())

    # a single first name and nothing else: full_name stays empty, nothing invented
    code, _ = _waitlist(client, "5105550310", name="Priya")
    assert code == 200
    u = db.query(User).filter(User.phone == "+15105550310").one()
    assert (u.name, u.full_name) == ("Priya", None)


def test_admin_waitlist_tab_shows_the_full_name(db, client):
    _pending(db, phone="+15105550311", name="Nate", full_name="Nate Ruiz")
    html = client.get("/admin").get_data(as_text=True)
    assert "Nate Ruiz" in html and "Full name" in html


def test_waitlist_goal_csv_and_defaults_match_signup(db, client):
    from models import User
    code, _ = _waitlist(client, "5105550302", goal="muscle_building", gender=None, experience="",
                        equipment=None, biggest_obstacle=None, age=None)
    assert code == 200
    u = db.query(User).filter(User.phone == "+15105550302").one()
    assert u.goal == "muscle_building" and u.age is None and u.biggest_obstacle is None
    assert (u.gender, u.experience, u.equipment) == ("prefer_not_to_say", "none", "full_gym")


def test_waitlist_requires_sms_consent(db, client):
    from models import User
    for consent in (None, False, "skip"):
        code, data = _waitlist(client, "5105550303", sms_consent=consent)
        assert code == 400 and data["status"] == "error" and "agree" in data["message"].lower()
    assert db.query(User).filter(User.phone == "+15105550303").first() is None, "no row without consent"


def test_waitlist_rejects_oversized_profile_values(db, client):
    from models import User
    code, data = _waitlist(client, "5105550304", gender="x" * 40)
    assert code == 400 and data["status"] == "error"
    assert db.query(User).filter(User.phone == "+15105550304").first() is None


def test_waitlist_provisions_photon_and_returns_the_link(db, client, photon_on, monkeypatch, sms_capture):
    import photon
    from models import User
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "ph-w1")
    code, data = _waitlist(client, "5105550305")
    assert code == 200 and "/users/ph-w1/redirect" in (data.get("imessage_link") or ""), data
    u = db.query(User).filter(User.phone == "+15105550305").one()
    assert u.photon_user_id == "ph-w1" and u.preferred_channel == "imessage"
    assert u.waitlist_status == "pending" and (u.onboarding_step or 0) == 0
    assert _outbound(db, u.id) == [] and sms_capture == [], "provisioned, but the hook waits for activation"


def test_waitlist_resubmit_while_pending_returns_the_link_and_changes_nothing(db, client, sms_capture):
    from models import User
    _pending(db, phone="+15105550307", name="First", preferred_channel="imessage", photon_user_id="ph-w7")
    code, data = _waitlist(client, "5105550307", name="Second", goal=["endurance"])
    assert code == 200 and data["status"] == "exists" and "/users/ph-w7/redirect" in data["imessage_link"]
    u = db.query(User).filter(User.phone == "+15105550307").one()
    assert u.name == "First" and u.goal == "fat_loss,strength", "an unauthenticated route never rewrites a row"
    assert _outbound(db, u.id) == [] and sms_capture == []


def test_waitlist_without_a_photon_slot_still_saves_the_row(db, client, photon_on, monkeypatch):
    import photon
    from models import User

    def _cap(phone, name=None):
        raise photon.PhotonError("402 user cap")
    monkeypatch.setattr(photon, "add_user", _cap)
    code, data = _waitlist(client, "5105550306")
    assert code == 200 and data["status"] == "ok" and data["imessage_link"] is None
    u = db.query(User).filter(User.phone == "+15105550306").one()
    assert u.photon_user_id is None and u.waitlist_status == "pending"


# ─── §2.2 the gate: a pending user who texts the line is held, not coached ────

def test_pending_first_imessage_gets_one_holding_line_and_never_the_coach(
        db, client, imessage_on, sidecar_ok, model_forbidden, sms_capture, caplog):
    from models import User
    from tests._sync import PENDING_TIMERS
    user = _pending(db, phone="+15105550311", preferred_channel="imessage", photon_user_id="ph-11")

    with caplog.at_level(logging.INFO):
        assert _post_imessage(client, user.phone, "hey cued", "wl-optin-1").status_code == 200
    assert PENDING_TIMERS.get(user.phone) is None, "never buffered — the model path is closed"

    db.expire_all()
    u = db.get(User, user.id)
    assert u.waitlist_status == "pending" and (u.onboarding_step or 0) == 0, "still on the list, no hook"
    assert u.imessage_opted_in_at is not None, "their line is open now"
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type, m.delivery_status) for m in rows] == [("imessage", "waitlist_hold", "sent")]
    assert "list" in rows[0].body.lower() and "Nate" in rows[0].body
    assert len(sidecar_ok) == 1 and sms_capture == [], "held blue, nothing green"
    assert any("WAITLIST_INBOUND_HELD" in r.getMessage() for r in caplog.records)

    # A second text is logged and left silent — one holding line per waitlister.
    assert _post_imessage(client, user.phone, "when do i start?", "wl-optin-2").status_code == 200
    assert len(_outbound(db, u.id)) == 1 and len(sidecar_ok) == 1
    assert PENDING_TIMERS.get(user.phone) is None


def test_pending_sms_inbound_is_held_too(db, client, model_forbidden, sms_capture):
    from models import User
    from tests._sync import PENDING_TIMERS
    user = _pending(db, phone="+15105550312")  # never provisioned; texted the Twilio number
    r = _post_sms(client, user.phone, "yo is this cued", "SMwl1")
    assert r.status_code == 200
    assert PENDING_TIMERS.get(user.phone) is None
    db.expire_all()
    u = db.get(User, user.id)
    assert u.waitlist_status == "pending" and (u.onboarding_step or 0) == 0
    assert u.imessage_opted_in_at is None, "an SMS proves nothing about iMessage"
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type) for m in rows] == [("sms", "waitlist_hold")]
    assert len(sms_capture) == 1 and "list" in sms_capture[0][1].lower()


def test_opt_in_stamp_is_written_for_any_first_imessage(db, client, imessage_on, sidecar_ok, anthropic_stub):
    """imessage_opted_in_at is a general fact, not a waitlist one: an active user's
    first inbound iMessage stamps it too, and it is never re-stamped."""
    from models import User
    from tests._sync import PENDING_TIMERS
    anthropic_stub.reply_with(lambda kw: "ok")
    user = make_user(db, phone="+15105550313", preferred_channel="imessage", photon_user_id="ph-13")
    assert _post_imessage(client, user.phone, "hey", "act-1").status_code == 200
    PENDING_TIMERS.pop(user.phone, None)
    db.expire_all()
    first = db.get(User, user.id).imessage_opted_in_at
    assert first is not None
    assert _post_imessage(client, user.phone, "hey again", "act-2").status_code == 200
    PENDING_TIMERS.pop(user.phone, None)
    db.expire_all()
    assert db.get(User, user.id).imessage_opted_in_at == first


# ─── §2.4 belt and braces: the hook can only reach an activated user ──────────

def test_fallback_sweep_skips_pending_waitlist(db, imessage_on, sidecar_gated, monkeypatch, sms_capture):
    import config
    from models import User, get_session
    from onboarding_agent import send_fallback_hooks
    monkeypatch.setattr(config, "ONBOARDING_HOOK_FALLBACK_MINUTES", 10)
    pending = _pending(db, phone="+15105550321", preferred_channel="imessage", photon_user_id="ph-21")
    s = get_session()
    try:
        s.get(User, pending.id).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
        s.commit()
    finally:
        s.close()
    assert send_fallback_hooks() == 0
    db.expire_all()
    assert db.get(User, pending.id).onboarding_step == 0 and _outbound(db, pending.id) == [] and sms_capture == []


def test_awaiting_channel_choice_is_false_for_a_pending_user(db):
    from onboarding_agent import awaiting_channel_choice
    pending = _pending(db, phone="+15105550322", preferred_channel="imessage", photon_user_id="ph-22")
    assert awaiting_channel_choice(pending) is False


# ─── §2.5 "I don't have an iPhone" on the waitlist success screen ─────────────

def test_no_iphone_on_the_waitlist_flips_the_channel_and_sends_nothing(db, client, imessage_on, sms_capture):
    from models import User
    user = _pending(db, phone="+15105550331", preferred_channel="imessage", photon_user_id="ph-31")
    r = client.post("/signup/channel", data=json.dumps({"phone": "5105550331", "channel": "sms"}),
                    content_type="application/json")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok" and data["hook_sent"] is False and data["waitlist"] is True
    db.expire_all()
    u = db.get(User, user.id)
    assert u.preferred_channel == "sms" and u.waitlist_status == "pending" and (u.onboarding_step or 0) == 0
    assert _outbound(db, u.id) == [] and sms_capture == []


# ─── §2.6 activation: opted in → blue first try; never tapped → SMS + link ────

def _activate(client, monkeypatch, user_id):
    import app as appmod
    from onboarding_agent import send_onboarding_hook
    # start_onboarding is a real daemon thread; run its body inline for determinism
    monkeypatch.setattr(appmod, "start_onboarding", lambda user: send_onboarding_hook(user.id))
    return client.post(f"/admin/user/{user_id}/activate-waitlist")


def test_activation_after_opt_in_goes_blue_first_try(db, client, imessage_on, sidecar_ok, monkeypatch, sms_capture):
    from models import User
    user = _pending(db, phone="+15105550341", preferred_channel="imessage", photon_user_id="ph-41",
                    imessage_opted_in_at=datetime.now(timezone.utc).replace(tzinfo=None))
    r = _activate(client, monkeypatch, user.id)
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    db.expire_all()
    u = db.get(User, user.id)
    assert u.waitlist_status is None and u.activated_at is not None and u.onboarding_step == 1
    rows = _outbound(db, u.id)
    assert [(m.channel, m.message_type, m.delivery_status) for m in rows] == [("imessage", "onboarding", "sent")]
    assert sms_capture == [] and "redirect" not in rows[0].body, "no link needed — they already texted the line"


def test_activation_without_opt_in_still_falls_over_to_sms_with_the_link(db, client, imessage_on, sidecar_gated,
                                                                          monkeypatch, sms_capture):
    """Guard on today's behavior: a waitlister who never tapped still gets the hook."""
    from models import User
    user = _pending(db, phone="+15105550342", preferred_channel="imessage", photon_user_id="ph-42")
    r = _activate(client, monkeypatch, user.id)
    assert r.status_code == 200
    db.expire_all()
    u = db.get(User, user.id)
    assert u.waitlist_status is None and u.onboarding_step == 1
    rows = _outbound(db, u.id)
    assert [(m.channel, m.delivery_status) for m in rows] == [("imessage", "failed"), ("sms", "sent")]
    assert "/users/ph-42/redirect" in rows[1].body


# ─── §2.7 admin tab shows the profile and the channel state ───────────────────

def test_admin_waitlist_tab_shows_profile_and_channel_state(db, client):
    _pending(db, phone="+15105550351", name="Opted", preferred_channel="imessage", photon_user_id="ph-51",
             imessage_opted_in_at=datetime.now(timezone.utc).replace(tzinfo=None))
    _pending(db, phone="+15105550352", name="Linked", preferred_channel="imessage", photon_user_id="ph-52")
    _pending(db, phone="+15105550353", name="Green", preferred_channel="sms", photon_user_id="ph-53")
    _pending(db, phone="+15105550354", name="Bare")
    r = client.get("/admin")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for s in ("Opted", "Linked", "Green", "Bare", "iMessage ✓", "link sent", "fat_loss, strength", "intermediate", "full_gym"):
        assert s in html, s
