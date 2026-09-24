"""Workout card web-link fallback: a user who doesn't want the Spectrum iMessage
extension gets the card as a plain browser link (the card_page web app taps + logs
the same) instead of the Photon extension card. set_card_delivery flips the
preference; send_workout_card honors it. (Live: founder 2026-09-24.)"""
from __future__ import annotations

import pytest

import config
from tests.factories import make_user

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", preferred_channel="imessage")


@pytest.fixture
def sidecar_cfg(monkeypatch):
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s")
    monkeypatch.setattr(config, "CARD_LINK_FALLBACK_ENABLED", True)


def _no_extension_card(monkeypatch):
    """send_card must NOT be called in link mode — blow up if it is."""
    import photon_cards
    monkeypatch.setattr(photon_cards, "send_card",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("extension card sent in link mode")))


# ─── send_workout_card honors the preference ─────────────────────────────────

def test_link_mode_sends_a_browser_link_not_the_extension_card(db, sidecar_cfg, sms_capture, monkeypatch):
    from workouts.plan import build_session
    from workouts.card import send_workout_card
    user = make_user(db, prefers_card_link=True, **FOUNDER)
    ws = build_session(user, "push")
    _no_extension_card(monkeypatch)
    out = send_workout_card(ws.id)
    assert "card_link" in out
    # a plain text with the card URL went out (sms_capture holds (phone, body))
    bodies = [b for _p, b in sms_capture]
    assert any("card.html" in b or "/card" in b or "t=" in b for b in bodies), bodies
    assert any("tap sets" in b for b in bodies)


def test_default_still_sends_the_extension_card(db, sidecar_cfg, monkeypatch):
    import photon_cards
    from workouts.plan import build_session
    from workouts.card import send_workout_card
    user = make_user(db, **FOUNDER)   # prefers_card_link defaults False
    ws = build_session(user, "push")
    calls = []
    monkeypatch.setattr(photon_cards, "send_card",
                        lambda phone, url, live=True, layout=None: calls.append(url) or
                        {"provider_message_id": "pc-1", "card_session": {"id": "pc-1"}})
    send_workout_card(ws.id)
    assert len(calls) == 1, "default users must still get the Photon extension card"


def test_flag_off_falls_back_to_extension_even_if_user_prefers_link(db, sidecar_cfg, monkeypatch):
    import photon_cards
    from workouts.plan import build_session
    from workouts.card import send_workout_card
    monkeypatch.setattr(config, "CARD_LINK_FALLBACK_ENABLED", False)
    user = make_user(db, prefers_card_link=True, **FOUNDER)
    ws = build_session(user, "push")
    calls = []
    monkeypatch.setattr(photon_cards, "send_card",
                        lambda phone, url, live=True, layout=None: calls.append(url) or
                        {"provider_message_id": "pc-1", "card_session": {"id": "pc-1"}})
    send_workout_card(ws.id)
    assert len(calls) == 1, "flag off = extension card regardless of preference"


# ─── set_card_delivery tool ──────────────────────────────────────────────────

def test_set_card_delivery_link_sets_pref_and_resends_open_card(db, sidecar_cfg, sms_capture, monkeypatch):
    from workouts.plan import build_session
    from agent_tools import handle_set_card_delivery
    from models import get_session, User
    user = make_user(db, **FOUNDER)
    build_session(user, "push")           # an open session exists
    _no_extension_card(monkeypatch)
    out = handle_set_card_delivery(user.id, {"mode": "link"})
    assert out.startswith("ok:") and "link" in out
    s = get_session()
    try:
        assert s.get(User, user.id).prefers_card_link is True
    finally:
        s.close()
    assert any("tap sets" in b for _p, b in sms_capture), "open card should be re-sent as a link"


def test_set_card_delivery_card_reverts_pref(db, sidecar_cfg, monkeypatch):
    from agent_tools import handle_set_card_delivery
    from models import get_session, User
    user = make_user(db, prefers_card_link=True, **FOUNDER)   # no open session
    out = handle_set_card_delivery(user.id, {"mode": "card"})
    assert out.startswith("ok:")
    s = get_session()
    try:
        assert s.get(User, user.id).prefers_card_link is False
    finally:
        s.close()


def test_set_card_delivery_bad_mode_is_an_error(db):
    from agent_tools import handle_set_card_delivery
    user = make_user(db, **FOUNDER)
    assert handle_set_card_delivery(user.id, {"mode": "email"}).startswith("error")


def test_set_card_delivery_is_wired():
    import agent_tools
    assert agent_tools._HANDLERS.get("set_card_delivery") is agent_tools.handle_set_card_delivery
    assert agent_tools.SET_CARD_DELIVERY_TOOL["input_schema"]["properties"]["mode"]["enum"] == ["link", "card"]
