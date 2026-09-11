"""
Photon migration — Phase 4: the Flask seam.

A. sms.py channel router above `_send_single`. `send_sms()` stays the single
   chokepoint; callers are untouched. imessage → sidecar /send; on ANY sidecar
   failure write a `failed` row FIRST (the keystone reads it), trip the user's
   circuit breaker (`channel_failed_over`), then fall through to Twilio so the
   same message still lands.
B. `POST /internal/inbound` — the sidecar's door into the SAME inbound pipeline
   the Twilio webhook uses. Unknown phone → WARNING (last 4 only) + 200 + a
   durable `unknown_inbounds` row (the Business-tier trigger is a number).
C. photon.py — provision a Photon user at signup, BEFORE the first outbound.
   Failure never blocks onboarding: the user stays on SMS.

Everything is flag-gated (IMESSAGE_CHANNEL_ENABLED, PHOTON_PROVISIONING_ENABLED)
and defaults OFF, so this ships dark.
"""

from __future__ import annotations

import base64
import io
import json
import logging

import pytest

from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


@pytest.fixture
def sidecar_ok(monkeypatch):
    """Patch the sidecar call to succeed; records (phone, body)."""
    import sms
    calls: list = []

    def _fake(phone, body):
        calls.append((phone, body))
        return f"photon-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
    return calls


def _outbound_rows(db, user):
    from models import Message
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")
            .order_by(Message.id).all())


def _inbound_rows(db, user):
    from models import Message
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user.id, Message.direction == "in")
            .order_by(Message.id).all())


# ═══════════════════════════════════════════════════════════════════════════
# A. channel router
# ═══════════════════════════════════════════════════════════════════════════

def test_imessage_user_routes_to_sidecar_and_stamps_row(db, imessage_on, sidecar_ok, sms_capture):
    from sms import send_sms
    user = make_user(db, preferred_channel="imessage")

    sid = send_sms(user.phone, "how'd the lift go?", user_id=user.id, message_type="heartbeat")

    assert sidecar_ok == [(user.phone, "how'd the lift go?")]
    assert sms_capture == [], "Twilio must not be touched on a successful iMessage send"
    rows = _outbound_rows(db, user)
    assert len(rows) == 1
    assert (rows[0].channel, rows[0].provider_sid, rows[0].delivery_status) == ("imessage", sid, "sent")
    assert rows[0].message_type == "heartbeat"


def test_sidecar_failure_writes_failed_row_first_then_falls_over_to_sms(db, imessage_on, monkeypatch, sms_capture):
    """The seam's contract with the keystone: the `failed` row exists BEFORE the
    Twilio attempt, the breaker trips, and the SAME message arrives via SMS."""
    import sms
    from models import User

    def _boom(phone, body):
        raise RuntimeError("sidecar /send 502: Target not allowed for this project")
    monkeypatch.setattr(sms, "_send_imessage", _boom)

    user = make_user(db, preferred_channel="imessage")
    sms_sid = sms.send_sms(user.phone, "morning — water first, then coffee", user_id=user.id,
                           message_type="heartbeat")

    # SMS fallback actually went out, with the same body
    assert sms_capture == [(user.phone, "morning - water first, then coffee")]  # GSM-7 normalized dash
    assert sms_sid.startswith("SMfake")

    rows = _outbound_rows(db, user)
    assert [(r.channel, r.delivery_status) for r in rows] == [("imessage", "failed"), ("sms", "sent")], \
        "failed imessage row must be written before the sms row"
    assert rows[0].provider_sid is None
    assert rows[1].provider_sid == sms_sid

    db.expire_all()
    u = db.get(User, user.id)
    assert u.channel_failed_over is True
    assert u.channel_failover_at is not None


def test_failed_over_user_goes_straight_to_sms(db, imessage_on, sidecar_ok, sms_capture):
    from sms import send_sms
    user = make_user(db, preferred_channel="imessage", channel_failed_over=True)

    send_sms(user.phone, "hey", user_id=user.id)

    assert sidecar_ok == [], "breaker is open — sidecar must not be called"
    assert len(sms_capture) == 1
    assert _outbound_rows(db, user)[0].channel == "sms"


def test_flag_off_keeps_everyone_on_sms(db, monkeypatch, sidecar_ok, sms_capture):
    import config
    from sms import send_sms
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", False)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    user = make_user(db, preferred_channel="imessage")

    send_sms(user.phone, "hey", user_id=user.id)

    assert sidecar_ok == []
    assert len(sms_capture) == 1


def test_no_user_id_is_plain_sms(db, imessage_on, sidecar_ok, sms_capture):
    """Callers without a user_id (rare) can't be routed — SMS, no DB lookup."""
    from sms import send_sms
    send_sms("+15550001234", "hey")
    assert sidecar_ok == [] and len(sms_capture) == 1


def test_twilio_failure_writes_failed_row_and_reraises(db, monkeypatch):
    import sms
    user = make_user(db)  # preferred sms

    def _boom(phone, body):
        raise RuntimeError("Twilio 30019")
    monkeypatch.setattr(sms, "_send_single", _boom)

    with pytest.raises(RuntimeError, match="30019"):
        sms.send_sms(user.phone, "hey", user_id=user.id, message_type="evening")

    rows = _outbound_rows(db, user)
    assert [(r.channel, r.delivery_status, r.provider_sid) for r in rows] == [("sms", "failed", None)]


def test_imessage_gets_the_full_body_in_one_call_parts_joined(db, imessage_on, sidecar_ok):
    """SMS splits on `---` into separate texts; iMessage sends ONE bubble with the
    coach's part boundaries kept as blank lines, never a literal `---`. No GSM-7
    normalization either — iMessage is unicode."""
    from sms import send_sms
    user = make_user(db, preferred_channel="imessage")

    send_sms(user.phone, "main point — with an em dash --- context here --- so, lift today?", user_id=user.id)

    assert len(sidecar_ok) == 1
    body = sidecar_ok[0][1]
    assert body == "main point — with an em dash\n\ncontext here\n\nso, lift today?"
    assert "---" not in body


def test__send_imessage_http_shape_and_error_handling(monkeypatch, imessage_on):
    import sms

    class _Resp:
        def __init__(self, status, payload):
            self.status_code, self._p = status, payload
            self.text = json.dumps(payload)
        def json(self):
            return self._p

    seen = {}
    def _post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp(200, {"ok": True, "provider_message_id": "photon-abc"})
    monkeypatch.setattr(sms.requests, "post", _post)

    assert sms._send_imessage("+12094205037", "hi") == "photon-abc"
    assert seen["url"] == f"{SIDECAR}/send"
    assert seen["json"] == {"phone": "+12094205037", "text": "hi"}
    assert seen["headers"]["X-Internal-Secret"] == SECRET
    assert 0 < seen["timeout"] <= 15

    monkeypatch.setattr(sms.requests, "post",
                        lambda *a, **k: _Resp(502, {"ok": False, "error": "Target not allowed"}))
    with pytest.raises(Exception, match="Target not allowed"):
        sms._send_imessage("+12094205037", "hi")

    monkeypatch.setattr(sms.requests, "post", lambda *a, **k: _Resp(200, {"ok": False, "error": "weird"}))
    with pytest.raises(Exception, match="weird"):
        sms._send_imessage("+12094205037", "hi")


# ═══════════════════════════════════════════════════════════════════════════
# B. POST /internal/inbound
# ═══════════════════════════════════════════════════════════════════════════

def _post_inbound(client, payload, *, secret=SECRET, files=None):
    headers = {"X-Internal-Secret": secret} if secret is not None else {}
    if files:
        data = {"payload": json.dumps(payload)}
        for i, (name, mime, content) in enumerate(files):
            data[f"attachment_{i}"] = (io.BytesIO(content), name, mime)
        return client.post("/internal/inbound", data=data, headers=headers,
                           content_type="multipart/form-data")
    return client.post("/internal/inbound", data=json.dumps(payload), headers=headers,
                       content_type="application/json")


def _payload(phone, text, msg_id="photon-in-1", **extra):
    return {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
            "service": "iMessage", "line_phone": "+15102646604",
            "timestamp": "2026-09-10T12:00:00.000Z", "attachments": [], **extra}


def test_internal_inbound_401_without_or_with_wrong_secret(db, client, imessage_on):
    user = make_user(db)
    for secret in (None, "wrong"):
        r = _post_inbound(client, _payload(user.phone, "hey"), secret=secret)
        assert r.status_code == 401, secret
    assert _inbound_rows(db, user) == []


def test_internal_inbound_401_when_secret_unconfigured(db, client, monkeypatch):
    """No configured secret must mean CLOSED, not open."""
    import config
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "")
    user = make_user(db)
    r = _post_inbound(client, _payload(user.phone, "hey"), secret="")
    assert r.status_code == 401


def test_internal_inbound_known_user_runs_the_same_pipeline(db, client, imessage_on, driver, sms_capture):
    """Proof it's the SAME pipeline: the inbound row is logged with channel=imessage,
    the buffer arms, flushing it produces a coach reply (LLM stubbed)."""
    user = make_user(db)  # preferred sms — reply comes back via Twilio capture
    r = _post_inbound(client, _payload(user.phone, "what should i eat for lunch?"))
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "known": True}

    rows = _inbound_rows(db, user)
    assert len(rows) == 1
    assert (rows[0].channel, rows[0].provider_sid) == ("imessage", "photon-in-1")
    assert rows[0].body == "what should i eat for lunch?"

    driver.flush(user.phone)  # fire the buffer exactly as the Twilio driver does
    assert len(sms_capture) >= 1, "no coach reply produced — pipeline not reached"


def test_internal_inbound_reply_returns_over_imessage_for_imessage_user(db, client, imessage_on, sidecar_ok, driver, sms_capture):
    user = make_user(db, preferred_channel="imessage")
    _post_inbound(client, _payload(user.phone, "how much protein today?"))
    driver.flush(user.phone)

    assert len(sidecar_ok) >= 1, "reply should route back through the sidecar"
    assert sms_capture == []
    out = _outbound_rows(db, user)
    assert out and all(r.channel == "imessage" for r in out)


def test_internal_inbound_unknown_phone_200_warning_last4_and_counted(db, client, imessage_on, caplog):
    from models import get_session, UnknownInbound
    with caplog.at_level(logging.WARNING):
        r = _post_inbound(client, _payload("+12094205037", "yo is this cued?"))
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "known": False}

    warns = [rec.getMessage() for rec in caplog.records
             if rec.levelno == logging.WARNING and "UNKNOWN" in rec.getMessage().upper()]
    assert warns, "unknown-phone inbound must log at WARNING"
    assert any("5037" in w for w in warns)
    assert not any("+12094205037" in w for w in warns), "full phone must not appear in logs"

    s = get_session()
    try:
        rows = s.query(UnknownInbound).all()
    finally:
        s.close()
    assert len(rows) == 1
    assert rows[0].handle == "+12094205037"
    assert rows[0].channel == "imessage"


def test_internal_inbound_email_sender_is_unknown_not_a_crash(db, client, imessage_on):
    r = _post_inbound(client, _payload("someone@icloud.com", "hi"))
    assert r.status_code == 200
    assert r.get_json()["known"] is False


def test_internal_inbound_normalizes_phone_like_the_webhook(db, client, imessage_on):
    user = make_user(db, phone="+15105551234")
    r = _post_inbound(client, _payload("(510) 555-1234", "hey"))
    assert r.status_code == 200 and r.get_json()["known"] is True
    assert len(_inbound_rows(db, user)) == 1


def test_internal_inbound_duplicate_provider_message_id_writes_once(db, client, imessage_on):
    user = make_user(db)
    _post_inbound(client, _payload(user.phone, "just ate a burrito", msg_id="photon-dup"))
    _post_inbound(client, _payload(user.phone, "just ate a burrito", msg_id="photon-dup"))
    assert len(_inbound_rows(db, user)) == 1


def test_internal_inbound_attachment_appends_image_marker(db, client, imessage_on):
    from sms import IMAGE_MARKER
    user = make_user(db)
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    r = _post_inbound(client, _payload(user.phone, "", attachments=[{"name": "IMG_1.png", "mime_type": "image/png", "size": len(png)}]),
                      files=[("IMG_1.png", "image/png", png)])
    assert r.status_code == 200
    rows = _inbound_rows(db, user)
    assert len(rows) == 1
    assert rows[0].body == IMAGE_MARKER  # captionless attachment logs the marker alone, never empty


def test_internal_inbound_bad_payload_400(db, client, imessage_on):
    assert _post_inbound(client, {"text": "no phone"}).status_code == 400
    r = client.post("/internal/inbound", data="not json", headers={"X-Internal-Secret": SECRET},
                    content_type="application/json")
    assert r.status_code == 400


def test_twilio_webhook_still_works_and_stamps_sms_channel(db, driver):
    """Regression guard on the extraction: the Twilio path is byte-for-byte the
    same pipeline, now stamping channel=sms / provider_sid=MessageSid."""
    user = make_user(db)
    replies = driver.send(user, "what should i eat for lunch?", message_sid="SMtwilio1")
    assert len(replies) >= 1
    rows = _inbound_rows(db, user)
    assert len(rows) == 1
    assert (rows[0].channel, rows[0].provider_sid) == ("sms", "SMtwilio1")


# ═══════════════════════════════════════════════════════════════════════════
# C. Photon provisioning
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def photon_creds(monkeypatch):
    import config
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", True)
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_ID", "ce4294aa-0000-4000-8000-000000000001")
    monkeypatch.setattr(config, "SPECTRUM_PROJECT_SECRET", "s3cret")
    monkeypatch.setattr(config, "SPECTRUM_API_URL", "https://spectrum.photon.codes")


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)
    def json(self):
        return self._p


def test_add_user_http_shape_from_openapi(monkeypatch, photon_creds):
    """Pinned to https://spectrum.photon.codes/openapi/json:
    POST /projects/{projectId}/users/  body {type:'shared', phoneNumber, firstName?, lastName?}
    Authorization: Basic base64(projectId:projectSecret). 200 → {succeed, data:{id,...}}."""
    import photon
    seen = {}
    def _post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp(200, {"succeed": True, "data": {"id": "usr_123", "phoneNumber": json["phoneNumber"],
                                                     "assignedPhoneNumber": "+15102646604", "type": "shared"}})
    monkeypatch.setattr(photon.requests, "post", _post)

    uid = photon.add_user("+12094205037", "Nau Ruiz")

    assert uid == "usr_123"
    assert seen["url"] == "https://spectrum.photon.codes/projects/ce4294aa-0000-4000-8000-000000000001/users/"
    assert seen["json"] == {"type": "shared", "phoneNumber": "+12094205037", "firstName": "Nau", "lastName": "Ruiz"}
    expected = "Basic " + base64.b64encode(b"ce4294aa-0000-4000-8000-000000000001:s3cret").decode()
    assert seen["headers"]["Authorization"] == expected
    assert 0 < seen["timeout"] <= 15


def test_add_user_single_name_and_errors(monkeypatch, photon_creds):
    import photon
    seen = {}
    monkeypatch.setattr(photon.requests, "post",
                        lambda url, json=None, headers=None, timeout=None: (seen.update(json=json) or _Resp(200, {"succeed": True, "data": {"id": "u"}})))
    photon.add_user("+12094205037", "Nau")
    assert seen["json"] == {"type": "shared", "phoneNumber": "+12094205037", "firstName": "Nau"}

    # Free-tier cap or any rejection → raise with the status + body visible
    monkeypatch.setattr(photon.requests, "post",
                        lambda *a, **k: _Resp(422, {"succeed": False, "error": "maxSharedUsers reached"}))
    with pytest.raises(photon.PhotonError, match="422"):
        photon.add_user("+12094205037", "Nau")
    # a 200 without succeed/id is also a failure, not a silent None
    monkeypatch.setattr(photon.requests, "post", lambda *a, **k: _Resp(200, {"succeed": False}))
    with pytest.raises(photon.PhotonError):
        photon.add_user("+12094205037", "Nau")


def test_provision_user_success_sets_id_and_preferred_channel(db, monkeypatch, photon_creds):
    import photon
    from models import User
    monkeypatch.setattr(photon, "add_user", lambda phone, name=None: "usr_abc")
    user = make_user(db)

    assert photon.provision_user(user.id) is True

    db.expire_all()
    u = db.get(User, user.id)
    assert u.photon_user_id == "usr_abc"
    assert u.preferred_channel == "imessage"


def test_provision_user_failure_leaves_sms_and_never_raises(db, monkeypatch, photon_creds, caplog):
    import photon
    from models import User
    def _boom(phone, name=None):
        raise photon.PhotonError("photon users API 422: maxSharedUsers reached")
    monkeypatch.setattr(photon, "add_user", _boom)
    user = make_user(db)

    with caplog.at_level(logging.WARNING):
        assert photon.provision_user(user.id) is False

    db.expire_all()
    u = db.get(User, user.id)
    assert u.photon_user_id is None
    assert u.preferred_channel == "sms"
    assert any("PHOTON_PROVISION_FAILED" in r.getMessage() for r in caplog.records)


def test_provision_user_flag_off_makes_no_call(db, monkeypatch):
    import config, photon
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    called = []
    monkeypatch.setattr(photon, "add_user", lambda *a, **k: called.append(a) or "x")
    user = make_user(db)
    assert photon.provision_user(user.id) is False
    assert called == []


def test_provision_user_is_idempotent_when_already_provisioned(db, monkeypatch, photon_creds):
    import photon
    called = []
    monkeypatch.setattr(photon, "add_user", lambda *a, **k: called.append(a) or "x")
    user = make_user(db, photon_user_id="usr_existing")
    assert photon.provision_user(user.id) is True
    assert called == []


def test_start_onboarding_provisions_before_the_first_outbound(db, monkeypatch, photon_creds, sms_capture):
    """4C's placement rule: provisioning runs BEFORE the hook text is sent, on every
    entry point (signup w/ consent, /activate-sms, admin waitlist activation all
    funnel through start_onboarding)."""
    import onboarding_agent, photon, sms
    from tests import _sync
    order: list = []
    monkeypatch.setattr(onboarding_agent, "threading",
                        _sync.make_threading_shim(Thread=_sync.SyncThread))
    monkeypatch.setattr(photon, "provision_user", lambda uid: order.append(("provision", uid)) or True)
    real_single = sms._send_single
    def _spy(phone, body):
        order.append(("send", phone))
        return real_single(phone, body)
    monkeypatch.setattr(sms, "_send_single", _spy)

    user = make_user(db, onboarding_step=0)
    onboarding_agent.start_onboarding(user)

    assert [o[0] for o in order] == ["provision", "send"]
    assert order[0][1] == user.id
