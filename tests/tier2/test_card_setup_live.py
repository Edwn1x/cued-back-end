"""
Tier-2 (live) anchors for the card setup step (2026-09-24): after code sent the
extension framing + first card + tour, the MODEL has to hold the line when asked.
Binary, x3 each (founder rule).

  1. "wait what is this card thing, do i need to download an app" → says extension,
     never calls it our app, sends no second card.
  2. "it won't open on my phone lol" → the extension line or the text-me-your-sets
     fallback; no second card, no claim it's fixed.
  3. "what do the numbers on it mean" → weight and reps, in words.
  4. "nah i don't wanna install anything, just send it as a link" → set_card_delivery
     mode=link (PR #113): prefers_card_link flips and the card URL goes out as text.
Run: pytest tests/tier2/test_card_setup_live.py --run-tier2 -s
"""

from __future__ import annotations

import re

import pytest

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok, card_ok  # noqa: F401 — fixtures

pytestmark = pytest.mark.tier2

ANGEL = dict(name="Angel", onboarding_step=3, preferred_channel="imessage", equipment="full_gym",
             current_split="ppl", confirmed_training_split="ppl", workout_days="5,6",
             workout_time="18:00", wake_time="08:00", sleep_time="00:00", height_ft=5, height_in=11,
             weight_lbs=180, age=20, gender="male", goal="fat_loss", experience="intermediate",
             calorie_target=2080, protein_target=180, targets_source="computed",
             lift_anchors={"bench": {"weight": 185, "reps": 5, "source": "stated"},
                           "squat": {"weight": 225, "reps": 5, "source": "stated"}})

NO_LINK_RE = re.compile(r"\blink\b|browser|safari")
OUR_APP_RE = re.compile(r"\b(our|the|cued|an|my) app\b|download (the|an|our) app|it'?s an app", re.IGNORECASE)


def _on(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "START_WORKOUT_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED",
              "CARD_SETUP_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _setup(db, monkeypatch):
    _on(monkeypatch)
    from workouts.start import start_workout_session
    u = make_user(db, **ANGEL)
    r = start_workout_session(u.id, setup=True)
    assert r["surface"] == "card"
    return u


def _run(user_id, text):
    from agent_loop import run_agent_loop
    from models import get_session, User
    s = get_session()
    try:
        return run_agent_loop(s.get(User, user_id), text, "freeform") or ""
    finally:
        s.close()


def _n_sessions(user_id):
    from models import get_session, WorkoutSession
    s = get_session()
    try:
        return s.query(WorkoutSession).filter_by(user_id=user_id).count()
    finally:
        s.close()


@pytest.mark.parametrize("i", range(3))
def test_what_is_this_do_i_need_an_app(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    u = _setup(db, monkeypatch)
    reply = _run(u.id, "wait what is this card thing? do i need to download an app")
    print(f"\n[{i}] {reply!r}")
    low = reply.lower()
    # The framing, not the keyword (plain voice says "imessage thing like gamepigeon"):
    # it's not an app, and it's the GamePigeon / one-tap kind of thing.
    assert re.search(r"\bno app\b|not an app|\bextension\b", low), reply
    assert "gamepigeon" in low or "one tap" in low, reply
    assert not OUR_APP_RE.search(reply), reply
    assert not NO_LINK_RE.search(low), reply          # the link is for pushback only
    assert _n_sessions(u.id) == 1 and len(card_ok) == 1, "a second card went out"


@pytest.mark.parametrize("i", range(3))
def test_it_wont_open(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    u = _setup(db, monkeypatch)
    reply = _run(u.id, "it won't open on my phone lol")
    print(f"\n[{i}] {reply!r}")
    low = reply.lower()
    # The property: it's the add step (extension / gamepigeon / add), not a glitch to debug.
    assert re.search(r"extension|gamepigeon|\badd(ed|s)?\b|(text|tell|send) me", low), reply
    assert not re.search(r"\b(fixed|resent|sent (it|another|a new)|try again now)\b", low), reply
    assert not NO_LINK_RE.search(low), reply          # confusion ≠ pushback: explain the add, no link
    assert _n_sessions(u.id) == 1 and len(card_ok) == 1, "a second card went out"


@pytest.mark.parametrize("i", range(3))
def test_what_do_the_numbers_mean(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    u = _setup(db, monkeypatch)
    reply = _run(u.id, "what do the numbers on it mean")
    print(f"\n[{i}] {reply!r}")
    low = reply.lower()
    assert re.search(r"\b(weight|lbs?|pounds?)\b", low) and re.search(r"\breps?\b", low), reply
    assert _n_sessions(u.id) == 1 and len(card_ok) == 1, "a second card went out"


@pytest.mark.parametrize("i", range(3))
def test_dont_want_to_install_send_a_link(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    import config
    monkeypatch.setattr(config, "CARD_LINK_FALLBACK_ENABLED", True)
    u = _setup(db, monkeypatch)
    n_before = len(sidecar_ok)
    reply = _run(u.id, "nah i don't wanna install anything, can u just send it as a link")
    print(f"\n[{i}] {reply!r}")
    from models import get_session, User
    s = get_session()
    try:
        assert s.get(User, u.id).prefers_card_link is True, reply
    finally:
        s.close()
    assert any("/card" in b for b in sidecar_ok[n_before:]), (reply, sidecar_ok[n_before:])
    assert len(card_ok) == 1, "a second extension bubble went out"


@pytest.mark.parametrize("i", range(3))
def test_soft_pushback_without_asking_for_a_link_gets_the_link(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    """Pushback that never says 'link': the model has to recognize it and switch them."""
    import config
    monkeypatch.setattr(config, "CARD_LINK_FALLBACK_ENABLED", True)
    u = _setup(db, monkeypatch)
    n_before = len(sidecar_ok)
    reply = _run(u.id, "ehh i really don't wanna install anything on my phone")
    print(f"\n[{i}] {reply!r}")
    from models import get_session, User
    s = get_session()
    try:
        pref = s.get(User, u.id).prefers_card_link
    finally:
        s.close()
    low = reply.lower()
    assert pref is True or NO_LINK_RE.search(low), reply     # switched them, or at least offered it
    assert len(card_ok) == 1, "a second extension bubble went out"
