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


def test_card_url_is_the_site_page_with_the_token():
    from card_page import card_url, verify_card_token
    url = card_url(31, 7)
    assert url.startswith("https://cued.fit/card.html?t=")
    assert verify_card_token(url.split("?t=")[1]) == (31, 7)
    assert card_url(31, 7, version=1699).endswith("&v=1699")


# ─── page + api ──────────────────────────────────────────────────────────────

@pytest.fixture
def planned(db):
    from workouts.plan import build_session
    from card_page import card_token
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push", now=datetime(2026, 9, 16, 22, 0))
    return user, ws, card_token(user.id, ws.id)


def test_legacy_page_route_redirects_a_valid_token_to_the_site(client, planned):
    user, ws, tok = planned
    r = client.get(f"/card/workout/{tok}")
    assert r.status_code == 302 and r.headers["Location"] == f"https://cued.fit/card.html?t={tok}"


def test_bad_or_expired_token_is_401_and_wrong_session_404(client, planned, monkeypatch):
    from card_page import card_token
    user, ws, tok = planned
    assert client.get("/card/workout/nope").status_code == 401
    assert client.get(f"/card/workout/{card_token(user.id, ws.id, exp=int(time.time()) - 5)}").status_code == 401
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


# ─── PR once, not on every equal repeat ──────────────────────────────────────

def test_pr_badge_fires_once_per_new_session_best(client, db):
    """Live: 185×5 ×3 then 190×5 over last week's 185×3 badged all four rows and
    refreshed the bubble three times. Rule: badge when a set beats last time AND is
    a new best for today → 185×5 (first), then 190×5. Not the two equal repeats."""
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
    ids = _bench_ids(client.get("/card/api/session", headers=_auth(tok)).get_json())
    prs = []
    for i, sid in enumerate(ids):
        body = {"done": True, "actual_weight": 190 if i == 3 else 185, "actual_reps": 5}
        prs.append(client.post(f"/card/api/set/{sid}", headers=_auth(tok), data=json.dumps(body)).get_json()["pr"])
    assert prs == ["185 × 5 is a PR 🎉 last time was 185 × 3.", None, None, "190 × 5 is a PR 🎉 last time was 185 × 3."]
    state = client.get("/card/api/session", headers=_auth(tok)).get_json()
    badges = [x["pr"] for x in next(e for e in state["exercises"] if e["slug"] == "bench_press")["sets"]]
    assert badges == prs


# ─── add / remove sets and exercises ─────────────────────────────────────────

def test_add_and_remove_sets(client, planned):
    user, ws, tok = planned
    state = client.get("/card/api/session", headers=_auth(tok)).get_json()
    assert state["set_count"] == 13
    d = client.post("/card/api/set", headers=_auth(tok), data=json.dumps({"exercise": "bench_press"})).get_json()
    bench = next(e for e in d["exercises"] if e["slug"] == "bench_press")["sets"]
    assert d["set_count"] == 14 and len(bench) == 5 and bench[-1]["planned_weight"] == 135 and bench[-1]["index"] == 4
    d = client.delete(f"/card/api/set/{bench[-1]['id']}", headers=_auth(tok)).get_json()
    assert d["set_count"] == 13
    assert client.post("/card/api/set", headers=_auth(tok), data=json.dumps({"exercise": "nope"})).status_code == 404
    assert client.delete("/card/api/set/999999", headers=_auth(tok)).status_code == 404


def test_add_and_remove_exercises(client, planned):
    user, ws, tok = planned
    # known name → template slug + label, history/template-planned, 3 sets by default
    d = client.post("/card/api/exercise", headers=_auth(tok), data=json.dumps({"name": "OHP"})).get_json()
    ohp = next(e for e in d["exercises"] if e["slug"] == "overhead_press")
    assert ohp["label"] == "overhead press" and len(ohp["sets"]) == 3 and ohp["sets"][0]["planned_weight"] == 75
    # free-text name needs weight + reps; sets capped at 10
    r = client.post("/card/api/exercise", headers=_auth(tok), data=json.dumps({"name": "landmine press"}))
    assert r.status_code == 400
    d = client.post("/card/api/exercise", headers=_auth(tok),
                    data=json.dumps({"name": "Landmine Press", "weight": 70, "reps": 8, "sets": 2})).get_json()
    lm = next(e for e in d["exercises"] if e["slug"] == "landmine_press")
    assert lm["label"] == "landmine press" and len(lm["sets"]) == 2 and lm["sets"][0]["planned_reps"] == 8
    # duplicate → 409; remove only the undone sets
    assert client.post("/card/api/exercise", headers=_auth(tok), data=json.dumps({"name": "ohp"})).status_code == 409
    client.post(f"/card/api/set/{ohp['sets'][0]['id']}", headers=_auth(tok), data=json.dumps({"done": True}))
    d = client.delete("/card/api/exercise/overhead_press", headers=_auth(tok)).get_json()
    assert d["removed"] == 2
    ohp = next(e for e in d["exercises"] if e["slug"] == "overhead_press")
    assert len(ohp["sets"]) == 1 and ohp["sets"][0]["done"]
    assert client.delete("/card/api/exercise/nothing_here", headers=_auth(tok)).status_code == 404


def test_add_remove_refused_on_a_closed_session(client, planned, monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None: type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())
    user, ws, tok = planned
    client.post("/card/api/finish", headers=_auth(tok), data="{}")
    assert client.post("/card/api/set", headers=_auth(tok), data=json.dumps({"exercise": "bench_press"})).status_code == 409
    assert client.post("/card/api/exercise", headers=_auth(tok), data=json.dumps({"name": "ohp"})).status_code == 409
