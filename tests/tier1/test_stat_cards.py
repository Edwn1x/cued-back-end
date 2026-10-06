"""Stat cards: the small fixed-height live bubbles ("rsf right now", "today",
"this week"). State builders read the same sources the coach uses; the page is
server-rendered and token-gated; send refuses when there's nothing true to show."""

import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
SECRET = "test-internal-secret"


def _naive(aware):
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def rsf_open(monkeypatch):
    import integrations.rsf
    monkeypatch.setattr(integrations.rsf, "is_open", lambda now=None: True)


def _reading(db, pct, wait=None, age_min=2):
    from models import GymOccupancy
    from integrations.rsf import FACILITY
    db.add(GymOccupancy(facility=FACILITY, ts=_utcnow() - timedelta(minutes=age_min), pct=pct, est_wait_min=wait))
    db.commit()


# ─── token ───────────────────────────────────────────────────────────────────

def test_token_round_trips_and_is_bound_to_kind_and_expiry():
    from stat_cards import stat_token, verify_stat_token
    t = stat_token("macros", 42)
    assert verify_stat_token("macros", t) == 42
    assert verify_stat_token("week", t) is None                     # a macros link can't open the week card
    assert verify_stat_token("macros", t[:-2] + "xx") is None       # tampered
    assert verify_stat_token("macros", stat_token("macros", 42, exp=int(time.time()) - 1)) is None
    assert verify_stat_token("nope", t) is None and verify_stat_token("macros", None) is None


def test_page_rejects_a_bad_token_without_leaking_anything(client):
    r = client.get("/card/stat/rsf?t=1.2.bad")
    assert r.status_code == 401
    assert r.headers["Cache-Control"] == "no-store"
    assert "expired" in r.get_data(as_text=True)


# ─── rsf ─────────────────────────────────────────────────────────────────────

def test_rsf_card_quotes_the_meter(db, rsf_open):
    from stat_cards import rsf_state
    _reading(db, 18)
    s = rsf_state()
    assert s["available"] and s["headline"] == "18% full" and s["subline"] == "basically empty. go."
    assert s["bars"][0]["frac"] == pytest.approx(0.18) and s["bars"][0]["tone"] == "blue"
    assert s["caption"] == "rsf · 18% full"


def test_rsf_card_line_on_shows_the_wait(db, rsf_open):
    from stat_cards import rsf_state
    _reading(db, 97, wait=25)
    s = rsf_state()
    assert s["headline"] == "97% full" and s["subline"] == "line's on · ~25 min." and s["bars"][0]["tone"] == "red"


def test_rsf_card_never_invents_a_number(db, monkeypatch, rsf_open):
    """Stale reading (> 20 min) or closed → unavailable, and send refuses."""
    import integrations.rsf
    from stat_cards import rsf_state, send_stat_card, StatCardUnavailable
    _reading(db, 18, age_min=45)
    assert rsf_state()["available"] is False
    u = make_user(db)
    with pytest.raises(StatCardUnavailable):
        send_stat_card(u.id, "rsf")
    monkeypatch.setattr(integrations.rsf, "is_open", lambda now=None: False)
    assert rsf_state()["headline"] == "closed"


def test_rsf_page_renders_fixed_height_no_js(client, db, rsf_open):
    from stat_cards import stat_token
    _reading(db, 18)
    u = make_user(db)
    r = client.get(f"/card/stat/rsf?t={stat_token('rsf', u.id)}&v=1")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    assert "18% full" in html and "basically empty. go." in html and "width: 18.0%" in html
    assert "<script" not in html and "overflow: hidden" in html
    # fills the extension's frame: fluid width, full height, never a fixed px width
    assert "width: 100%; height: 100%" in html and "width: 300px" not in html


# ─── macros ──────────────────────────────────────────────────────────────────

def _meal(db, user_id, cal, pro):
    from models import Meal
    db.add(Meal(user_id=user_id, description="x", calories=cal, protein_g=pro, eaten_at=_utcnow()))
    db.commit()


def test_macros_card_flags_protein_low_when_it_trails_calories(db):
    from stat_cards import macros_state
    u = make_user(db, calorie_target=2400, protein_target=160)
    _meal(db, u.id, 900, 10)     # boba + fries: 38% of calories, 6% of protein
    _meal(db, u.id, 500, 20)
    s = macros_state(u.id)
    cal, pro = s["bars"]
    assert cal["frac"] == pytest.approx(1400 / 2400) and cal["badge"] is None and cal["value"] == "1,400 / 2,400"
    assert (cal["big"], cal["of"], pro["big"], pro["of"]) == ("1,400", "/ 2,400 cal", "30g", "/ 160g")
    assert pro["badge"] == "low" and pro["tone"] == "amber" and pro["frac"] == pytest.approx(30 / 160)
    assert s["subcaption"] == "1,400 cal · 30g protein · protein's low"


def test_macros_card_protein_on_pace_has_no_badge_and_hit_is_green(db):
    from stat_cards import macros_state
    u = make_user(db, calorie_target=2000, protein_target=150)
    _meal(db, u.id, 1000, 70)
    assert macros_state(u.id)["bars"][1]["badge"] is None
    _meal(db, u.id, 600, 90)
    pro = macros_state(u.id)["bars"][1]
    assert pro["badge"] == "hit" and pro["badge_tone"] == "green" and pro["frac"] == 1.0


def test_macros_card_ignores_deleted_meals_and_shows_no_bar_without_targets(db, client):
    from models import Meal
    from stat_cards import macros_state, stat_token
    u = make_user(db)
    _meal(db, u.id, 400, 30)
    db.add(Meal(user_id=u.id, description="gone", calories=999, protein_g=99, eaten_at=_utcnow(), deleted_at=_utcnow()))
    db.commit()
    s = macros_state(u.id)
    assert [b["frac"] for b in s["bars"]] == [None, None] and s["no_targets"]
    assert s["bars"][0]["value"] == "400"
    html = client.get(f"/card/stat/macros?t={stat_token('macros', u.id)}").get_data(as_text=True)
    assert "no targets set yet" in html and 'class="track"' not in html


# ─── week ────────────────────────────────────────────────────────────────────

SUN_NIGHT = datetime(2026, 10, 4, 20, 41, tzinfo=PT)   # the mockup: "Sun 8:41 PM"


def _deadline(user_id, title, when_aware, *, source="gcal", all_day=False):
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=title, title=title,
                          occurred_at=_naive(when_aware), all_day=all_day)


def test_week_card_sunday_night_shows_mon_to_fri_with_midterm_and_lift(db):
    from stat_cards import week_state
    u = make_user(db, confirmed_training_days="wed", current_split="ppl", split_pointer_day="pull",
                  split_pointer_at=_naive(SUN_NIGHT - timedelta(days=2)))
    _deadline(u.id, "Ochem midterm", datetime(2026, 10, 6, 10, 0, tzinfo=PT))
    s = week_state(u.id, now_utc=SUN_NIGHT.astimezone(timezone.utc))
    assert s["label"] == "this week"
    assert [d["dow"] for d in s["week"]] == ["mon", "tue", "wed", "thu", "fri"]
    by = {d["dow"]: d["items"] for d in s["week"]}
    assert by["tue"] == [{"text": "ochem", "tone": "exam"}]
    assert by["wed"] == [{"text": "legs", "tone": "lift"}]          # the day after pull
    assert by["mon"] == by["thu"] == by["fri"] == []
    assert s["subcaption"] == "1 due · 1 lift day"


def test_week_card_continues_the_cycle_and_marks_today_done(db):
    """Mid-week: today trained (pointer moved today) → done chip; later training days
    walk the cycle from the next day; all-day bcourses items land on their LOCAL day."""
    from stat_cards import week_state
    wed = datetime(2026, 10, 7, 18, 0, tzinfo=PT)
    u = make_user(db, confirmed_training_days="mon,wed,fri,sat", current_split="ppl",
                  split_pointer_day="push", split_pointer_at=_naive(wed - timedelta(hours=1)))
    _deadline(u.id, "due: Homework 4: RISC-V [CS61C]", datetime(2026, 10, 9, 0, 0, tzinfo=PT),
              source="bcourses", all_day=True)
    s = week_state(u.id, now_utc=wed.astimezone(timezone.utc))
    assert s["label"] == "next 5 days" and [d["dow"] for d in s["week"]] == ["wed", "thu", "fri", "sat", "sun"]
    assert s["week"][0]["today"] is True
    by = {d["dow"]: d["items"] for d in s["week"]}
    assert by["wed"] == [{"text": "push", "tone": "done"}]
    assert by["fri"] == [{"text": "cs61c", "tone": "due"}, {"text": "pull", "tone": "lift"}]
    assert by["sat"] == [{"text": "legs", "tone": "lift"}]


def test_week_card_overflow_collapses_to_plus_n(db):
    from stat_cards import week_state
    u = make_user(db)
    for i in range(4):
        _deadline(u.id, f"Essay {i} due", datetime(2026, 10, 6, 9 + i, 0, tzinfo=PT))
    tue = {d["dow"]: d for d in week_state(u.id, now_utc=SUN_NIGHT.astimezone(timezone.utc))["week"]}["tue"]
    assert len(tue["items"]) == 2 and tue["more"] == 2


def test_week_card_empty_grid_says_why(db):
    from stat_cards import week_state
    u = make_user(db)
    s = week_state(u.id, now_utc=SUN_NIGHT.astimezone(timezone.utc))
    assert s["empty_note"] == "nothing due. tell me your lift days and they'll show here."
    u2 = make_user(db, confirmed_training_days="sat")      # training days, none in the window
    assert week_state(u2.id, now_utc=SUN_NIGHT.astimezone(timezone.utc))["empty_note"] == "nothing due."
    u3 = make_user(db, confirmed_training_days="mon")
    assert week_state(u3.id, now_utc=SUN_NIGHT.astimezone(timezone.utc))["empty_note"] is None


def test_week_card_unmapped_split_says_lift_not_a_guess(db):
    from stat_cards import week_state
    u = make_user(db, confirmed_training_days="tue", current_split="custom")
    by = {d["dow"]: d["items"] for d in week_state(u.id, now_utc=SUN_NIGHT.astimezone(timezone.utc))["week"]}
    assert by["tue"] == [{"text": "lift", "tone": "lift"}]


@pytest.mark.parametrize("title,label", [
    ("Ochem midterm", "ochem"),
    ("due: Homework 4: RISC-V [CS61C]", "cs61c"),
    ("CS 70 midterm", "cs70"),
    ("Midterm 2", "midterm"),
    ("Econ problem set due", "econ"),
])
def test_chip_label(title, label):
    from stat_cards import chip_label
    assert chip_label(title) == label


def test_week_page_renders_the_grid(client, db):
    from stat_cards import stat_token
    u = make_user(db, confirmed_training_days="mon,tue,wed,thu,fri", current_split="ppl")
    html = client.get(f"/card/stat/week?t={stat_token('week', u.id)}").get_data(as_text=True)
    assert html.count('class="cell"') == 5 and 'class="chip lift"' in html


# ─── send / update / driver ──────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


@pytest.fixture
def sidecar(monkeypatch):
    import config
    import photon_cards
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "STAT_CARD_BASE_URL", "https://web.test")
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return _Resp(200, {"ok": True, "provider_message_id": "card-1", "card_session": {"id": "card-1"}})
    monkeypatch.setattr(photon_cards.requests, "post", fake_post)
    return calls


def test_send_live_card_posts_url_only_and_logs_the_outbound(db, sidecar):
    from models import Message
    from stat_cards import send_stat_card, verify_stat_token
    u = make_user(db, calorie_target=2400, protein_target=160)
    r = send_stat_card(u.id, "macros", live=True)
    url, payload = sidecar[0]
    assert url.endswith("/send-card") and payload["live"] is True and "layout" not in payload
    assert payload["url"].startswith("https://web.test/card/stat/macros?t=") and "&v=" in payload["url"]
    tok = payload["url"].split("t=")[1].split("&")[0]
    assert verify_stat_token("macros", tok) == u.id
    assert r["card_session"] == {"id": "card-1"}
    db.expire_all()
    m = db.query(Message).filter(Message.user_id == u.id).one()
    assert m.message_type == "stat_card_macros" and m.channel == "imessage" and m.provider_sid == "card-1"


def test_send_static_card_carries_captions(db, sidecar, rsf_open):
    from stat_cards import send_stat_card
    _reading(db, 18)
    u = make_user(db)
    send_stat_card(u.id, "rsf", live=False)
    payload = sidecar[0][1]
    assert payload["live"] is False
    assert payload["layout"] == {"caption": "rsf · 18% full", "subcaption": "basically empty. go.", "summary": "rsf right now"}


def test_link_fallback_user_gets_the_url_as_text(db, sidecar, sms_capture):
    import config
    from stat_cards import send_stat_card
    u = make_user(db, prefers_card_link=True)
    if not config.CARD_LINK_FALLBACK_ENABLED:
        pytest.skip("fallback disabled")
    r = send_stat_card(u.id, "week")
    assert sidecar == [] and r["card_session"] is None
    assert "/card/stat/week?t=" in json.dumps(sms_capture)


def test_update_re_renders_in_place_with_a_new_version(db, sidecar):
    from stat_cards import update_stat_card
    u = make_user(db)
    update_stat_card(u.id, "week", {"id": "card-1"}, live=True)
    url, payload = sidecar[0]
    assert url.endswith("/update-card") and payload["card_session"] == {"id": "card-1"}
    assert "/card/stat/week?t=" in payload["url"] and payload["live"] is True


def test_driver_sends_a_stat_card_to_the_user_on_that_phone(client, db, sidecar, rsf_open):
    _reading(db, 18)
    u = make_user(db)
    h = {"X-Internal-Secret": SECRET}
    assert client.post("/internal/card-test", json={"phone": u.phone, "action": "send_stat", "kind": "rsf"}).status_code == 401
    r = client.post("/internal/card-test", headers=h, json={"phone": u.phone, "action": "send_stat", "kind": "rsf"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["card_session"] == {"id": "card-1"}
    assert body["state"]["headline"] == "18% full"
    assert client.post("/internal/card-test", headers=h,
                       json={"phone": u.phone, "action": "send_stat", "kind": "bogus"}).status_code == 400
    assert client.post("/internal/card-test", headers=h,
                       json={"phone": "+19999999999", "action": "send_stat", "kind": "rsf"}).status_code == 404


# ─── image mode: the card as a 2.5:1 picture (half the live bubble's height) ──

def test_image_route_draws_a_wide_png_and_is_token_gated(client, db, rsf_open):
    import io
    from PIL import Image
    from stat_cards import stat_token
    _reading(db, 81)
    u = make_user(db, calorie_target=2400, protein_target=160, confirmed_training_days="wed", current_split="ppl")
    for kind in ("rsf", "macros", "week"):
        r = client.get(f"/card/stat/{kind}/image.png?t={stat_token(kind, u.id)}&v=1")
        assert r.status_code == 200 and r.headers["Content-Type"] == "image/png", kind
        assert Image.open(io.BytesIO(r.data)).size == (1200, 479)
    assert client.get(f"/card/stat/week/image.png?t={stat_token('rsf', u.id)}").status_code == 401


def test_page_og_tags_point_at_the_image_with_the_same_token(client, db, monkeypatch):
    import config
    from stat_cards import stat_token
    monkeypatch.setattr(config, "STAT_CARD_BASE_URL", "https://web.test")
    u = make_user(db)
    t = stat_token("week", u.id)
    html = client.get(f"/card/stat/week?t={t}&v=7").get_data(as_text=True)
    assert f'<meta property="og:image" content="https://web.test/card/stat/week/image.png?t={t}&amp;v=7">' in html
    assert '<meta property="og:title" content="this week">' in html and "og:description" not in html


def test_image_mode_sends_the_picture_as_the_whole_layout(db, sidecar):
    """No caption (no strip) and a blank imageTitle (the SDK needs one; a real title is
    printed over the picture; Photon strips plain whitespace, so U+2800). JPEG, base64."""
    import base64
    from stat_cards import send_stat_card, update_stat_card
    u = make_user(db)
    r = send_stat_card(u.id, "macros", mode="image")
    payload = sidecar[0][1]
    lay = payload["layout"]
    assert payload["live"] is False and r["mode"] == "image"
    assert set(lay) == {"imageBase64", "imageTitle", "summary"} and lay["imageTitle"] == "\u2800" and lay["summary"] == "today"
    assert base64.b64decode(lay["imageBase64"])[:2] == b"\xff\xd8"          # JPEG
    update_stat_card(u.id, "macros", {"id": "card-1"}, mode="image")
    assert "imageBase64" in sidecar[1][1]["layout"] and sidecar[1][1]["live"] is False


def test_mode_defaults_from_config_and_rejects_junk(db, sidecar, monkeypatch):
    import config
    from stat_cards import send_stat_card
    u = make_user(db)
    monkeypatch.setattr(config, "STAT_CARDS_MODE", "image")
    assert send_stat_card(u.id, "week")["mode"] == "image"
    assert send_stat_card(u.id, "week", live=True)["mode"] == "live"   # legacy flag still wins over the default
    with pytest.raises(ValueError):
        send_stat_card(u.id, "week", mode="huge")


def test_image_renderer_never_overflows_long_text():
    """Everything is drawn through _fit, so long chips/sublines truncate, never spill."""
    from stat_card_image import render_png
    days = [{"dow": d, "today": False, "items": [{"text": "supercalifragilistic", "tone": "exam"}] * 2, "more": 3}
            for d in ("mon", "tue", "wed", "thu", "fri")]
    assert render_png({"kind": "week", "label": "this week", "week": days})[:4] == b"\x89PNG"
    assert render_png({"kind": "rsf", "label": "rsf right now", "headline": "100% full",
                       "subline": "line's on · ~120 min. bring a book and a snack.",
                       "bars": [{"frac": 1.0, "tone": "red"}], "foot": "as of 12:59pm"})[:4] == b"\x89PNG"
