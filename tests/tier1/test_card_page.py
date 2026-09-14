"""
Workout card Phase 2 — the page in the bubble. Signed 24h token, render, tap /
edit / PR badge / finish, polling endpoint, expiry + tamper → 401.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", preferred_channel="sms",
               height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male", goal="fat_loss,muscle_building")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _auth(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _bench_ids(state):
    ex = next(e for e in state["exercises"] if e["slug"] == "bench_press")
    return [s["id"] for s in ex["sets"]]


# ─── token ───────────────────────────────────────────────────────────────────

def test_card_token_roundtrip_expiry_and_tamper():
    from card_page import card_token, verify_card_token
    tok = card_token(31, 7)
    assert verify_card_token(tok) == (31, 7)
    u, s, e, mac = tok.split(".")
    assert verify_card_token(f"{u}.{s}.{e}.{mac[:-1]}x") is None        # tampered mac
    assert verify_card_token(f"{u}.8.{e}.{mac}") is None                 # session swapped
    assert verify_card_token(f"32.{s}.{e}.{mac}") is None                # user swapped
    assert verify_card_token(tok, now=time.time() + 25 * 3600) is None   # expired
    old = card_token(31, 7, exp=int(time.time()) - 1)
    assert verify_card_token(old) is None
    assert verify_card_token("31.7") is None and verify_card_token(None) is None


def test_card_url_shape_and_version():
    from card_page import card_url, verify_card_token
    url = card_url(31, 7)
    assert url.startswith("https://") and "/card/workout/" in url and "?" not in url
    assert verify_card_token(url.rsplit("/", 1)[1]) == (31, 7)
    assert card_url(31, 7, version=1699).endswith("?v=1699")


# ─── page + api ──────────────────────────────────────────────────────────────

@pytest.fixture
def planned(db):
    from workouts.plan import build_session
    from card_page import card_token
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push", now=datetime(2026, 9, 16, 22, 0))
    return user, ws, card_token(user.id, ws.id)


def test_page_renders_session_state_and_is_uncached(client, planned):
    user, ws, tok = planned
    r = client.get(f"/card/workout/{tok}")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    assert '"template_key": "push"' in html and '"weekday": "wed"' in html
    assert '"label": "bench press"' in html and html.count('"planned_weight": 135.0') == 4
    assert "prefers-color-scheme" in html and "width: 300px" in html and "padding-left: 40px" in html
    assert "min-height: 44px" in html
    assert 'type="checkbox"' not in html   # circles are drawn from state (GATE 0 finding)


def test_bad_or_expired_token_is_401_and_wrong_session_404(client, planned, monkeypatch):
    from card_page import card_token
    user, ws, tok = planned
    assert client.get("/card/workout/nope").status_code == 401
    assert client.get(f"/card/workout/{card_token(user.id, ws.id, exp=int(time.time()) - 5)}").status_code == 401
    assert client.get(f"/card/workout/{card_token(user.id, ws.id + 999)}").status_code == 404
    assert client.get("/card/api/session").status_code == 401
    assert client.post(f"/card/api/set/1", headers=_auth("x.y.z.w"), data="{}").status_code == 401


def test_tap_toggles_done_at_plan_and_footer_volume_grows(client, planned):
    from models import get_session, SetLog, WorkoutSession
    user, ws, tok = planned
    state = client.get("/card/api/session", headers=_auth(tok)).get_json()
    assert state["ok"] and state["done_count"] == 0 and state["set_count"] == 13
    b0 = _bench_ids(state)[0]
    r = client.post(f"/card/api/set/{b0}", headers=_auth(tok), data=json.dumps({"done": True}))
    d = r.get_json()
    assert d["ok"] and d["pr"] is None and d["done_count"] == 1 and d["volume_lb"] == 675
    s = get_session()
    try:
        row = s.get(SetLog, b0)
        assert row.done and row.actual_weight == 135 and row.actual_reps == 5 and row.source == "card"
        w = s.get(WorkoutSession, ws.id)
        assert w.status == "active" and w.started_at is not None
    finally:
        s.close()
    # toggle off → back to nothing
    d = client.post(f"/card/api/set/{b0}", headers=_auth(tok), data=json.dumps({"done": False})).get_json()
    assert d["done_count"] == 0 and d["volume_lb"] == 0
    bench = next(e for e in d["exercises"] if e["slug"] == "bench_press")["sets"][0]
    assert bench["done"] is False and bench["actual_weight"] is None


def test_edit_marks_actual_values_and_edited_flag(client, planned):
    user, ws, tok = planned
    state = client.get("/card/api/session", headers=_auth(tok)).get_json()
    b2 = _bench_ids(state)[2]
    d = client.post(f"/card/api/set/{b2}", headers=_auth(tok),
                    data=json.dumps({"done": True, "actual_weight": "140", "actual_reps": "4"})).get_json()
    st = next(e for e in d["exercises"] if e["slug"] == "bench_press")["sets"][2]
    assert st["done"] and st["actual_weight"] == 140 and st["actual_reps"] == 4 and st["edited"] is True
    assert d["volume_lb"] == 560
    r = client.post(f"/card/api/set/{b2}", headers=_auth(tok), data=json.dumps({"done": True, "actual_weight": "heavy"}))
    assert r.status_code == 400


def test_pr_badge_on_190x4_over_last_times_185x3(client, db):
    from workouts.plan import build_session
    from card_page import card_token
    from models import get_session, SetLog, WorkoutSession
    user = make_user(db, **FOUNDER)
    prior = build_session(user, "push", now=_now() - timedelta(days=7))
    s = get_session()
    try:
        for x in s.query(SetLog).filter_by(session_id=prior.id, exercise="bench_press").all():
            x.actual_weight, x.actual_reps, x.done, x.source = 185, 3, True, "card"
        s.get(WorkoutSession, prior.id).status = "done"
        s.commit()
    finally:
        s.close()
    ws = build_session(user, "push")
    tok = card_token(user.id, ws.id)
    state = client.get("/card/api/session", headers=_auth(tok)).get_json()
    ids = _bench_ids(state)
    client.post(f"/card/api/set/{ids[0]}", headers=_auth(tok), data=json.dumps({"done": True, "actual_weight": 185, "actual_reps": 5}))
    d = client.post(f"/card/api/set/{ids[2]}", headers=_auth(tok),
                    data=json.dumps({"done": True, "actual_weight": 190, "actual_reps": 4})).get_json()
    assert d["pr"] == "190 × 4 is a PR 🎉 last time was 185 × 3."
    st = next(e for e in d["exercises"] if e["slug"] == "bench_press")["sets"][2]
    assert st["pr"] == "190 × 4 is a PR 🎉 last time was 185 × 3."   # persists in state → survives reload


def test_finish_closes_the_session_and_sends_the_summary_text(client, planned, sms_capture, monkeypatch):
    import threading
    from models import get_session, WorkoutSession
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None: type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())
    user, ws, tok = planned
    state = client.get("/card/api/session", headers=_auth(tok)).get_json()
    ids = _bench_ids(state)
    for i, sid in enumerate(ids):
        body = {"done": True} if i != 2 else {"done": True, "actual_weight": 140, "actual_reps": 4}
        client.post(f"/card/api/set/{sid}", headers=_auth(tok), data=json.dumps(body))
    r = client.post("/card/api/finish", headers=_auth(tok), data="{}")
    d = r.get_json()
    assert d["ok"] and d["session"]["status"] == "done"
    assert d["summary"]["volume_lb"] == 135 * 5 * 3 + 140 * 4 and d["summary"]["sets_done"] == 4
    s = get_session()
    try:
        w = s.get(WorkoutSession, ws.id)
        assert w.status == "done" and w.finished_at is not None and w.total_volume_lb == 2585
    finally:
        s.close()
    assert len(sms_capture) == 1
    text = sms_capture[0][1]
    assert text.splitlines()[0].startswith("push · wed")
    assert "bench press - 135x5 · 135x5 · 140x4 · 135x5" in text.replace("×", "x").replace("—", "-")
    assert "2,585 lb total · 0 PRs" in text
    assert "four taps. that's the whole log - no app opened." in text.replace("—", "-")
    # a closed session refuses further taps; finishing again is idempotent
    assert client.post(f"/card/api/set/{ids[0]}", headers=_auth(tok), data=json.dumps({"done": False})).status_code == 409
    assert client.post("/card/api/finish", headers=_auth(tok), data="{}").get_json()["ok"]
    assert len(sms_capture) == 1


def test_finish_with_nothing_done_sends_no_summary(client, planned, sms_capture, monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None: type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())
    user, ws, tok = planned
    d = client.post("/card/api/finish", headers=_auth(tok), data="{}").get_json()
    assert d["ok"] and d["summary"]["sets_done"] == 0
    assert sms_capture == []
