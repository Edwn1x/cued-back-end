"""Water-break GIF (water_gif.py, founder 2026-10-26): the standing water ping is the
character GIF, not a composed line. iMessage → sidecar /send-attachment; SMS → Twilio
MMS; text fallback whenever the file doesn't land; the rotation, the ack window and the
static files."""
from datetime import timedelta

import pytest

from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"


@pytest.fixture
def gif_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "REMINDERS_ENABLED", True)
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", True)
    monkeypatch.setattr(config, "WATER_REMINDER_GATES_ENABLED", False)


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "testsecret")


@pytest.fixture
def attachment_ok(monkeypatch):
    """Capture the sidecar attachment call: (phone, bytes, name, mime)."""
    import sms
    calls: list = []

    def _fake(phone, data, name, mime_type):
        calls.append((phone, data, name, mime_type))
        return f"photon-att-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage_attachment", _fake)
    return calls


@pytest.fixture
def sidecar_text_ok(monkeypatch):
    """Capture plain sidecar text sends (the fallback path for an iMessage user)."""
    import sms
    calls: list = []

    def _fake(phone, body, reply_to=None):
        calls.append((phone, body))
        return f"photon-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
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


def _water_row(db, user, **kw):
    from models import Reminder
    from reminders import create_reminder
    r = create_reminder(user.id, kw.pop("text", "drink water"), None, every_hours=3,
                        source=kw.pop("source", "offer"))
    db.expire_all()
    return db.get(Reminder, r["id"])


def _out(db, user_id):
    from models import Message
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.id).all())


def _no_model(anthropic_stub):
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("the model must not be called for a GIF ping")))


# ─── iMessage: the attachment bubble ─────────────────────────────────────────────

def test_imessage_user_gets_the_gif_as_an_attachment_and_no_line(db, gif_on, imessage_on, attachment_ok,
                                                                 sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import fire_due
    from water_gif import MARKER
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)

    assert fire_due(now=row.fire_at) == 1
    assert len(attachment_ok) == 1 and not sms_capture
    phone, data, name, mime = attachment_ok[0]
    assert phone == u.phone and name.endswith(".gif") and mime == "image/gif" and data[:4] == b"GIF8"

    rows = _out(db, u.id)
    assert len(rows) == 1
    assert (rows[0].message_type, rows[0].body, rows[0].channel, rows[0].delivery_status) == \
        ("water_reminder", MARKER, "imessage", "sent")
    assert rows[0].provider_sid == "photon-att-1"

    db.expire_all()
    row = db.get(Reminder, row.id)
    assert row.active is True and row.sent_count == 1 and row.fire_at > row.last_sent_at


def test_consecutive_pings_never_repeat_a_file(db, gif_on, imessage_on, attachment_ok, anthropic_stub):
    from models import Reminder
    from reminders import fire_due
    from water_gif import gif_files
    assert len(gif_files()) >= 2
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    for _ in range(6):
        db.expire_all()
        row = db.get(Reminder, row.id)
        assert fire_due(now=row.fire_at) == 1
    names = [c[2] for c in attachment_ok]
    assert len(names) == 6 and all(a != b for a, b in zip(names, names[1:]))
    assert set(names) <= set(gif_files()) and len(set(names)) >= 2


def test_the_shuffle_is_per_user_and_covers_every_file():
    from water_gif import _slot
    n = 5
    for uid in (1, 40, 51, 1234):
        seq = [_slot(uid, n, c) for c in range(40)]
        assert all(a != b for a, b in zip(seq, seq[1:]))          # never twice in a row
        assert set(seq) == set(range(n))                           # every file gets a turn
    # two users don't walk the same fixed cycle
    assert [_slot(1, n, c) for c in range(12)] != [_slot(2, n, c) for c in range(12)]
    assert _slot(7, 1, 3) == 0 and _slot(7, 0, 3) == 0


def test_attachment_failure_falls_back_to_the_composed_line(db, gif_on, imessage_on, monkeypatch,
                                                            sidecar_text_ok, anthropic_stub, caplog):
    import sms
    from reminders import fire_due
    from water_gif import MARKER
    monkeypatch.setattr(sms, "_send_imessage_attachment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sidecar /send-attachment 502: nope")))
    anthropic_stub.reply_with(lambda kw: "water break, go")
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)

    assert fire_due(now=row.fire_at) == 1
    assert sidecar_text_ok == [(u.phone, "water break, go")]
    rows = _out(db, u.id)
    assert [(r.message_type, r.body) for r in rows] == [("water_reminder", "water break, go")]
    assert MARKER not in [r.body for r in rows]
    assert "WATER_GIF_FAILED" in caplog.text


def test_photon_outage_skips_the_upload_and_uses_the_text_path(db, gif_on, imessage_on, monkeypatch,
                                                               attachment_ok, sidecar_text_ok, anthropic_stub):
    import sms
    from reminders import fire_due
    monkeypatch.setattr(sms, "outage_active", lambda: True)
    anthropic_stub.reply_with(lambda kw: "hydrate")
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    assert fire_due(now=row.fire_at) == 1
    assert not attachment_ok and sidecar_text_ok == [(u.phone, "hydrate")]


# ─── SMS: the MMS twin ───────────────────────────────────────────────────────────

def test_sms_user_gets_an_mms_pointing_at_the_static_gif(db, gif_on, mms_ok, sms_capture, anthropic_stub):
    import config
    from reminders import fire_due
    from water_gif import MARKER, gif_files
    _no_model(anthropic_stub)
    u = make_user(db)
    row = _water_row(db, u)
    assert fire_due(now=row.fire_at) == 1
    assert not sms_capture and len(mms_ok) == 1
    phone, url = mms_ok[0]
    assert phone == u.phone
    base = f"{config.INTEGRATIONS_BASE_URL.rstrip('/')}/static/water/"
    assert url.startswith(base) and url[len(base):] in gif_files()
    rows = _out(db, u.id)
    assert [(r.message_type, r.body, r.channel) for r in rows] == [("water_reminder", MARKER, "sms")]


def test_mms_base_url_override(monkeypatch):
    import config
    from water_gif import gif_url
    monkeypatch.setattr(config, "WATER_GIF_BASE_URL", "https://app.cued.fit/")
    assert gif_url("water-01-sip.gif") == "https://app.cued.fit/static/water/water-01-sip.gif"


# ─── when it stays a line ────────────────────────────────────────────────────────

def test_flag_off_keeps_the_composed_line(db, monkeypatch, mms_ok, sms_capture, anthropic_stub):
    import config
    from reminders import fire_due
    monkeypatch.setattr(config, "REMINDERS_ENABLED", True)
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", False)
    monkeypatch.setattr(config, "WATER_REMINDER_GATES_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "sip something")
    u = make_user(db)
    row = _water_row(db, u)
    assert fire_due(now=row.fire_at) == 1
    assert not mms_ok and sms_capture[-1][1] == "sip something"


def test_non_water_interval_rows_and_one_offs_keep_the_line(db, gif_on, mms_ok, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import create_reminder, fire_due
    # Two different lines: an identical body twice in a row would trip the outbound dedup.
    anthropic_stub.reply_with(lambda kw: "up and stretch" if "stretch" in kw["messages"][0]["content"] else "creatine time")
    u = make_user(db)
    r = create_reminder(u.id, "stand up and stretch", None, every_hours=2, source="model")
    db.expire_all()
    assert fire_due(now=db.get(Reminder, r["id"]).fire_at) == 1
    # Park the interval row's +2h re-arm out of the way: the 07:00 one-off below is more
    # than 2h out for most of the day, so otherwise BOTH fire there and the count is 2
    # (wall-clock fragile — green at 04:34 PT, red at 08:00 PT on 2026-10-26).
    db.expire_all()
    stretch = db.get(Reminder, r["id"])
    stretch.fire_at = stretch.fire_at + timedelta(days=2)
    db.commit()
    one = create_reminder(u.id, "take creatine", "07:00")
    db.expire_all()
    assert fire_due(now=db.get(Reminder, one["id"]).fire_at) == 1
    assert not mms_ok and [b for _, b in sms_capture] == ["up and stretch", "creatine time"]


def test_is_water_row():
    from types import SimpleNamespace as NS
    from water_gif import is_water_row
    assert is_water_row(NS(every_hours=3, source="offer", text="drink water"))
    assert is_water_row(NS(every_hours=2, source="model", text="Drink some WATER"))
    assert not is_water_row(NS(every_hours=2, source="model", text="stand up"))
    assert not is_water_row(NS(every_hours=None, source="offer", text="drink water"))


# ─── downstream readers see what they saw before ─────────────────────────────────

def test_a_gif_ping_still_opens_the_ack_window(db, gif_on, imessage_on, attachment_ok, anthropic_stub):
    from reminders import fire_due, is_water_ack
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    assert fire_due(now=row.fire_at) == 1
    assert is_water_ack(u.id, "👍") and is_water_ack(u.id, "drank")


def test_a_gif_ping_is_not_an_unanswered_proactive(db, gif_on, imessage_on, attachment_ok, anthropic_stub):
    from engagement_tracker import has_unanswered_proactive
    from reminders import fire_due
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    assert fire_due(now=row.fire_at) == 1
    assert has_unanswered_proactive(u.id, 180) is False


# ─── admin preview: POST /admin/water-gif/send ──────────────────────────────────

def test_admin_preview_sends_one_gif_without_the_flag_and_logs_it_as_admin(db, client, imessage_on, attachment_ok,
                                                                           monkeypatch):
    import config
    from water_gif import MARKER, gif_files
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", False)
    u = make_user(db, preferred_channel="imessage")
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": gif_files()[1]})
    assert r.status_code == 200 and r.get_json() == {"status": "ok", "sent": [gif_files()[1]], "failed": []}
    assert [c[2] for c in attachment_ok] == [gif_files()[1]]
    rows = _out(db, u.id)
    assert [(m.message_type, m.body, m.channel) for m in rows] == [("admin", MARKER, "imessage")]


def test_admin_preview_all_sends_every_file_in_order(db, client, mms_ok, monkeypatch):
    import time
    from water_gif import gif_files
    monkeypatch.setattr(time, "sleep", lambda s: None)   # app.py imports time locally → same module object
    u = make_user(db)
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": "all"})
    assert r.status_code == 200 and r.get_json()["sent"] == gif_files()
    assert [url.rsplit("/", 1)[1] for _, url in mms_ok] == gif_files()


def test_admin_preview_refuses_unknown_gif_archived_and_opted_out(db, client, mms_ok):
    from datetime import datetime
    u = make_user(db)
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": "nope.gif"})
    assert r.status_code == 404 and "available" in r.get_json()
    out = make_user(db, opted_out=True)
    assert client.post("/admin/water-gif/send", data={"user_id": str(out.id)}).status_code == 400
    arch = make_user(db)
    arch.archived_at = datetime(2026, 10, 1); db.commit()
    assert client.post("/admin/water-gif/send", data={"user_id": str(arch.id)}).status_code == 400
    assert client.post("/admin/water-gif/send", data={"user_id": "999999"}).status_code == 404
    assert not mms_ok


def test_admin_preview_reports_a_failed_send(db, client, monkeypatch):
    import sms
    monkeypatch.setattr(sms, "_send_single_mms", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("twilio down")))
    u = make_user(db)
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id)})
    assert r.status_code == 502 and r.get_json()["status"] == "error" and r.get_json()["failed"]


# ─── the files ───────────────────────────────────────────────────────────────────

def test_gifs_are_served_as_static_files_and_stay_mms_sized(client):
    from water_gif import GIF_DIR, gif_files
    import os
    names = gif_files()
    assert len(names) >= 2
    for name in names:
        assert os.path.getsize(os.path.join(GIF_DIR, name)) < 1_000_000   # MMS-friendly
        r = client.get(f"/static/water/{name}")
        assert r.status_code == 200 and r.mimetype == "image/gif" and r.data[:4] == b"GIF8"
