"""The polished water stickers (water_gif.py, static/water/polished/*.png — founder 2026-11-05:
60% size, fully transparent, "i like them all"). Animated PNG, iMessage only, behind
config.WATER_STICKER_SET: "sketch" (default, nothing changes) | "polished" | "both"."""
import os
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user
from tests.tier1.test_water_gif import (  # noqa: F401 — fixtures
    _no_model, _out, _water_row, attachment_ok, gif_on, imessage_on, mms_ok, sidecar_text_ok,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PTZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def polished(monkeypatch):
    import config
    monkeypatch.setattr(config, "WATER_STICKER_SET", "polished")


def _row(count, uid=7):
    return NS(sent_count=count, user_id=uid, source="offer", text="drink water", every_hours=3)


# ─── the setting ─────────────────────────────────────────────────────────────────

def test_the_default_set_is_the_sketch_loops_so_a_deploy_changes_nothing():
    import config
    from water_gif import gif_files, pick_gif
    assert config.WATER_STICKER_SET == "sketch"
    picks = {pick_gif(_row(c), 7, channel="imessage") for c in range(60)}
    assert picks and picks <= set(gif_files())


def test_polished_set_is_the_whole_pool_for_an_imessage_user(polished):
    from water_gif import pick_gif, polished_files
    picks = [pick_gif(_row(c), 51, channel="imessage") for c in range(200)]
    assert set(picks) == set(polished_files()), "every polished file rotates in, nothing else"
    assert all(a != b for a, b in zip(picks, picks[1:])), "never the same file twice in a row"


def test_an_sms_user_gets_the_same_seven_as_gifs(polished):
    from water_gif import pick_gif, polished_files, polished_gif_files
    assert [f[:-4] for f in polished_gif_files()] == [f[:-4] for f in polished_files()], "one GIF per PNG, same names"
    for channel in ("sms", None):                       # an unreadable channel gets the format every pipe animates
        picks = [pick_gif(_row(c), 51, channel=channel) for c in range(200)]
        assert set(picks) == set(polished_gif_files())
        assert all(a != b for a, b in zip(picks, picks[1:]))


def test_sketch_mode_is_the_hand_drawn_loops_for_every_channel(monkeypatch):
    import config
    from water_gif import gif_files, pick_gif
    monkeypatch.setattr(config, "WATER_STICKER_SET", "sketch")
    for channel in ("imessage", "sms", None):
        picks = {pick_gif(_row(c), 51, channel=channel) for c in range(60)}
        assert picks and picks <= set(gif_files())


def test_both_shuffles_the_two_sets_together_and_the_bed_loop_still_keeps_to_the_evening(db, monkeypatch):
    import config
    from water_gif import BED_GIF_HINT, gif_files, pick_gif, polished_files
    monkeypatch.setattr(config, "WATER_STICKER_SET", "both")
    u = make_user(db, sleep_time="23:00")
    afternoon = datetime(2026, 10, 28, 17, 5, tzinfo=PTZ).astimezone(timezone.utc).replace(tzinfo=None)
    picks = {pick_gif(_row(c, u.id), u.id, user=u, now=afternoon, channel="imessage") for c in range(400)}
    assert picks & set(gif_files()) and picks & set(polished_files())
    assert not any(BED_GIF_HINT in p for p in picks)


def test_polished_asked_for_but_missing_falls_back_to_the_gifs(polished, monkeypatch):
    import water_gif
    monkeypatch.setattr(water_gif, "polished_files", lambda: [])
    monkeypatch.setattr(water_gif, "polished_gif_files", lambda: [])
    for channel in ("imessage", "sms"):
        picks = {water_gif.pick_gif(_row(c), 7, channel=channel) for c in range(20)}
        assert picks and picks <= set(water_gif.gif_files())


# ─── a real ping ─────────────────────────────────────────────────────────────────

def test_an_imessage_ping_goes_out_as_an_animated_png_with_its_own_mime(db, gif_on, imessage_on, attachment_ok,
                                                                        sidecar_text_ok, anthropic_stub, polished):
    from models import Reminder
    from reminders import fire_due
    from water_gif import MARKER, polished_files
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    for _ in range(5):
        db.expire_all()
        row = db.get(Reminder, row.id)
        assert fire_due(now=row.fire_at) == 1
    names = [c[2] for c in attachment_ok]
    assert len(names) == 5 and all(a != b for a, b in zip(names, names[1:]))
    assert set(names) <= {os.path.basename(f) for f in polished_files()}, "the bubble's file name carries no folder"
    for _, data, name, mime in attachment_ok:
        assert name.endswith(".png") and mime == "image/png" and data[:8] == PNG_SIG
    rows = [m for m in _out(db, u.id) if m.body == MARKER]
    assert len(rows) == 5 and {(m.message_type, m.channel) for m in rows} == {("water_reminder", "imessage")}


def test_an_sms_ping_is_a_polished_gif_over_mms_and_carries_the_line_once(db, gif_on, mms_ok, sms_capture,
                                                                         anthropic_stub, polished):
    import config
    from water_gif import MARKER, polished_gif_files
    _no_model(anthropic_stub)
    u = make_user(db)
    row = _water_row(db, u)
    _fire(db, row, times=3)
    urls = [url for _, url in mms_ok]
    base = f"{config.INTEGRATIONS_BASE_URL.rstrip('/')}/static/water/"
    assert len(urls) == 3 and all(url.startswith(base) and url[len(base):] in polished_gif_files() for url in urls)
    bodies = [m.body for m in _out(db, u.id)]
    assert bodies.count(MARKER) == 3 and bodies.count(NOTE) == 1 and bodies[:2] == [MARKER, NOTE]


# ─── admin preview ───────────────────────────────────────────────────────────────

def test_admin_preview_sends_the_polished_set_in_order_or_one_by_name(db, client, imessage_on, attachment_ok, monkeypatch):
    import time
    from water_gif import polished_files
    monkeypatch.setattr(time, "sleep", lambda s: None)
    u = make_user(db, preferred_channel="imessage")
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": "polished"})
    assert r.status_code == 200 and r.get_json()["sent"] == polished_files()
    assert [c[3] for c in attachment_ok] == ["image/png"] * len(polished_files())
    one = polished_files()[0]
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": one})
    assert r.status_code == 200 and r.get_json()["sent"] == [one]
    assert {m.message_type for m in _out(db, u.id)} == {"admin"}


def test_admin_preview_all_is_still_only_the_gifs_and_unknown_lists_every_set(db, client, mms_ok, monkeypatch):
    import time
    from water_gif import gif_files, polished_files, polished_gif_files
    monkeypatch.setattr(time, "sleep", lambda s: None)
    u = make_user(db)
    assert client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": "all"}).get_json()["sent"] == gif_files()
    r = client.post("/admin/water-gif/send", data={"user_id": str(u.id), "name": "polished/nope.png"})
    assert r.status_code == 404 and r.get_json()["available"] == gif_files() + polished_files() + polished_gif_files()


def test_admin_preview_polished_follows_the_channel_and_polished_gif_forces_the_gifs(db, client, imessage_on, attachment_ok,
                                                                                    mms_ok, monkeypatch):
    import time
    from water_gif import polished_gif_files
    monkeypatch.setattr(time, "sleep", lambda s: None)
    sms_user = make_user(db)
    r = client.post("/admin/water-gif/send", data={"user_id": str(sms_user.id), "name": "polished"})
    assert r.status_code == 200 and r.get_json()["sent"] == polished_gif_files()
    assert [url.rsplit("/static/water/", 1)[1] for _, url in mms_ok] == polished_gif_files()
    im = make_user(db, preferred_channel="imessage")
    r = client.post("/admin/water-gif/send", data={"user_id": str(im.id), "name": "polished-gif"})
    assert r.status_code == 200 and r.get_json()["sent"] == polished_gif_files()
    assert [c[3] for c in attachment_ok] == ["image/gif"] * len(polished_gif_files())


# ─── the files ───────────────────────────────────────────────────────────────────

def test_the_seven_are_there_animated_transparent_and_sized_to_land_at_sixty_percent(client):
    from PIL import Image
    from water_gif import GIF_DIR, polished_files
    names = polished_files()
    assert [os.path.basename(n) for n in names] == [
        "water-glass.png", "water-logo-drip.png", "water-mascot.png", "water-porthole.png",
        "water-ring.png", "water-ripple.png", "water-type.png"]
    for name in names:
        path = os.path.join(GIF_DIR, name)
        assert os.path.getsize(path) < 1_000_000
        im = Image.open(path)
        assert getattr(im, "is_animated", False) and im.n_frames >= 10, name
        assert im.size == (640, 384), "art 384 wide on a 640 canvas = 60% of the bubble width"
        seen = 0
        for i in range(0, im.n_frames, max(1, im.n_frames // 6)):
            im.seek(i)
            alpha = im.convert("RGBA").getchannel("A")
            assert alpha.getpixel((0, 0)) == 0 and alpha.getpixel((639, 0)) == 0, "no card: the corners are clear"
            box = alpha.getbbox()
            if box:
                seen += 1
                assert box[2] <= 384, f"{name}: art stays in the left 60%"
        assert seen, f"{name}: something is drawn"
        r = client.get(f"/static/water/{name}")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:8] == PNG_SIG


# ─── "new sticker drop!" — once, under the first polished sticker ───────────────

NOTE = "new sticker drop!"


def _fire(db, row, times=1):
    from models import Reminder
    from reminders import fire_due
    for _ in range(times):
        db.expire_all()
        row = db.get(Reminder, row.id)
        assert fire_due(now=row.fire_at) == 1
    db.expire_all()
    return db.get(Reminder, row.id)


def test_the_first_polished_sticker_comes_with_the_line_and_later_ones_do_not(db, gif_on, imessage_on, attachment_ok,
                                                                              sidecar_text_ok, anthropic_stub, polished):
    import config
    from water_gif import MARKER
    assert config.WATER_STICKER_NOTE_ENABLED is True and config.WATER_STICKER_NOTE_TEXT == NOTE
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    _fire(db, row)
    assert sidecar_text_ok == [(u.phone, NOTE)]
    rows = _out(db, u.id)
    assert [(m.message_type, m.body) for m in rows] == [("water_reminder", MARKER), ("water_reminder", NOTE)], \
        "the sticker lands first, the line under it, both part of the water ping"
    _fire(db, row, times=3)
    assert sidecar_text_ok == [(u.phone, NOTE)], "said once, never again"
    assert len(attachment_ok) == 4


def test_a_thumbs_up_after_the_line_is_still_a_water_ack(db, gif_on, imessage_on, attachment_ok, sidecar_text_ok,
                                                         anthropic_stub, polished):
    from reminders import is_water_ack
    _no_model(anthropic_stub)
    u = make_user(db, preferred_channel="imessage")
    _fire(db, _water_row(db, u))
    assert _out(db, u.id)[-1].body == NOTE
    assert is_water_ack(u.id, "👍") is True and is_water_ack(u.id, "drank") is True


def test_no_line_with_the_sketch_set_or_at_the_admin_preview(db, client, gif_on, imessage_on, attachment_ok, mms_ok,
                                                            sidecar_text_ok, sms_capture, anthropic_stub, monkeypatch):
    import config
    import time
    _no_model(anthropic_stub)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    u = make_user(db, preferred_channel="imessage")                     # default set: the GIF loops
    _fire(db, _water_row(db, u))
    sms_user = make_user(db)
    _fire(db, _water_row(db, sms_user))
    monkeypatch.setattr(config, "WATER_STICKER_SET", "polished")
    adm = make_user(db, preferred_channel="imessage")                   # a preview is not a ping
    assert client.post("/admin/water-gif/send", data={"user_id": str(adm.id), "name": "polished"}).status_code == 200
    adm_sms = make_user(db)
    assert client.post("/admin/water-gif/send", data={"user_id": str(adm_sms.id), "name": "polished"}).status_code == 200
    assert not sidecar_text_ok
    for x in (u, sms_user, adm, adm_sms):
        assert NOTE not in [m.body for m in _out(db, x.id)]


def test_no_line_when_the_sticker_did_not_land_and_it_is_tried_again_next_time(db, gif_on, imessage_on, monkeypatch,
                                                                               sidecar_text_ok, anthropic_stub, polished):
    import sms
    calls = []

    def _flaky(phone, data, name, mime):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("sidecar /send-attachment 502: nope")
        return f"photon-att-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage_attachment", _flaky)
    anthropic_stub.reply_with(lambda kw: "water break, go")
    u = make_user(db, preferred_channel="imessage")
    row = _water_row(db, u)
    row = _fire(db, row)
    assert sidecar_text_ok == [(u.phone, "water break, go")], "fallback line only — nothing to announce"
    _fire(db, row)
    assert sidecar_text_ok[-1] == (u.phone, NOTE)


def test_the_line_can_be_switched_off_or_reworded_and_a_failure_never_costs_the_ping(db, gif_on, imessage_on,
                                                                                    attachment_ok, sidecar_text_ok,
                                                                                    anthropic_stub, polished, monkeypatch):
    import config
    import sms
    from water_gif import MARKER
    _no_model(anthropic_stub)
    monkeypatch.setattr(config, "WATER_STICKER_NOTE_ENABLED", False)
    a = make_user(db, preferred_channel="imessage")
    _fire(db, _water_row(db, a))
    assert not sidecar_text_ok
    monkeypatch.setattr(config, "WATER_STICKER_NOTE_ENABLED", True)
    monkeypatch.setattr(config, "WATER_STICKER_NOTE_TEXT", "fresh stickers just dropped")
    b = make_user(db, preferred_channel="imessage")
    _fire(db, _water_row(db, b))
    assert sidecar_text_ok == [(b.phone, "fresh stickers just dropped")]
    monkeypatch.setattr(sms, "send_sms", lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    c = make_user(db, preferred_channel="imessage")
    row = _fire(db, _water_row(db, c))
    assert row.sent_count == 1 and [m.body for m in _out(db, c.id)] == [MARKER]


def test_the_gif_versions_are_there_animated_clear_cornered_and_mms_sized(client):
    from PIL import Image
    from water_gif import GIF_DIR, polished_gif_files
    names = polished_gif_files()
    assert len(names) == 7
    for name in names:
        path = os.path.join(GIF_DIR, name)
        assert os.path.getsize(path) < 600_000, f"{name}: stays comfortably MMS-sized"
        im = Image.open(path)
        assert getattr(im, "is_animated", False) and im.n_frames >= 10, name
        assert im.size == (640, 384)
        seen = 0
        for i in range(0, im.n_frames, max(1, im.n_frames // 6)):
            im.seek(i)
            alpha = im.convert("RGBA").getchannel("A")
            assert alpha.getpixel((0, 0)) == 0 and alpha.getpixel((639, 0)) == 0
            assert sum(alpha.histogram()[1:255]) == 0, "one-bit alpha"
            box = alpha.getbbox()
            if box:
                seen += 1
                assert box[2] <= 384, f"{name}: art stays in the left 60%"
        assert seen, f"{name}: something is drawn"
        r = client.get(f"/static/water/{name}")
        assert r.status_code == 200 and r.mimetype == "image/gif" and r.data[:4] == b"GIF8"
