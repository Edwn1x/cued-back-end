"""
The card in the thread (workouts/card.py): static bubble captions that follow the
session, refreshed in place; tapping opens the page (overlay). Founder, 2026-09-14,
after the live-card test: no live WebView in the bubble.
"""

from __future__ import annotations

import json

import pytest

from tests.factories import make_user

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", preferred_channel="imessage")


@pytest.fixture
def sidecar_cfg(monkeypatch):
    import config
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s")


def _state(done, total, vol, status="active", key="push"):
    return {"session": {"template_key": key, "weekday": "wed", "status": status},
            "exercises": [{"label": "bench press", "sets": [{}] * 4}],
            "done_count": done, "set_count": total, "volume_lb": vol}


def test_card_layout_follows_the_session():
    from workouts.card import card_layout
    assert card_layout(_state(0, 13, 0, "planned")) == {
        "caption": "push · wed", "subcaption": "4 sets bench press, then the usual — tap to start",
        "trailingCaption": "0/13", "summary": "push day"}
    assert card_layout(_state(3, 13, 1935)) == {
        "caption": "push · wed", "subcaption": "in progress — tap to log", "trailingCaption": "3/13 · 1,935 lb", "summary": "push day"}
    assert card_layout(_state(13, 13, 8040, "done"))["subcaption"] == "done — tap for the log"
    assert card_layout(_state(13, 13, 8040, "done"))["trailingCaption"] == "8,040 lb"
    assert card_layout(_state(0, 5, 0, "planned", "full_body"))["caption"] == "full body · wed"


def test_refresh_card_edits_in_place_with_new_captions_and_is_best_effort(db, sidecar_cfg, monkeypatch):
    import photon_cards
    from workouts.plan import build_session
    from workouts.card import send_workout_card, refresh_card
    from card_page import apply_set_update
    from models import get_session, WorkoutSession, SetLog
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push")
    calls = []
    monkeypatch.setattr(photon_cards, "send_card", lambda phone, url, live=True, layout=None:
                        {"provider_message_id": "photon-card-1", "card_session": {"id": "photon-card-1"}})
    monkeypatch.setattr(photon_cards, "update_card", lambda phone, cs, url, live=None, layout=None: calls.append((cs["id"], url, live, layout)))
    send_workout_card(ws.id)
    s = get_session()
    try:
        w = s.get(WorkoutSession, ws.id)
        first = s.query(SetLog).filter_by(session_id=ws.id).order_by(SetLog.id).first()
        apply_set_update(s, w, first, done=True, source="card")
    finally:
        s.close()
    assert refresh_card(ws.id) is True
    cid, url, live, layout = calls[-1]
    assert cid == "photon-card-1" and live is False and "&v=" in url
    assert layout["subcaption"] == "in progress — tap to log" and layout["trailingCaption"] == "1/13 · 675 lb"

    def _boom(*a, **k):
        raise photon_cards.CardError("sidecar /update-card 502: session expired")
    monkeypatch.setattr(photon_cards, "update_card", _boom)
    assert refresh_card(ws.id) is False     # logged, never raises, breaker untouched


def test_refresh_without_a_card_is_a_noop(db):
    from workouts.plan import build_session
    from workouts.card import refresh_card
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push")
    assert refresh_card(ws.id) is False


def test_card_tap_flow_refreshes_the_bubble_on_activation_and_finish(client, db, sidecar_cfg, monkeypatch):
    """First done set → bubble says in progress; finish → bubble says done, then the summary text."""
    import threading, photon_cards
    from workouts.plan import build_session
    from workouts.card import send_workout_card
    from card_page import card_token
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None:
                        type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push")
    layouts = []
    monkeypatch.setattr(photon_cards, "send_card", lambda phone, url, live=True, layout=None:
                        {"provider_message_id": "photon-card-1", "card_session": {"id": "photon-card-1"}})
    monkeypatch.setattr(photon_cards, "update_card", lambda phone, cs, url, live=None, layout=None: layouts.append(layout["subcaption"]))
    send_workout_card(ws.id)
    tok = card_token(user.id, ws.id)
    H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    st = client.get("/card/api/session", headers=H).get_json()
    ids = [x["id"] for x in st["exercises"][0]["sets"]]
    client.post(f"/card/api/set/{ids[0]}", headers=H, data=json.dumps({"done": True}))
    client.post(f"/card/api/set/{ids[1]}", headers=H, data=json.dumps({"done": True}))   # no refresh: not a transition, no PR
    client.post("/card/api/finish", headers=H, data="{}")
    assert layouts == ["in progress — tap to log", "done — tap for the log"]
