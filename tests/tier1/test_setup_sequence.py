"""
Setup sequence (setup_sequence.py) — onboarding restructure PR 2 (founder, 2026-10-09).

  summary (2 bubbles + profile link) → rundown           at completion
  → connect offers, one per step (connect_offers)        on their next text / the sweep
  → the first card: pitch, ask or card, tour             LAST
A workout ask at any point sends the card right then (the early exit). Water waits for
day two. The coach knows a planned setup card is not a session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_card_setup import sidecar_ok, card_ok, sync_threads, _u, _sessions, ANCHORS  # noqa: F401


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def seq_on(monkeypatch):
    for f in ("CARD_SETUP_ENABLED", "START_WORKOUT_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED",
              "IMESSAGE_CHANNEL_ENABLED", "SETUP_SEQUENCE_ENABLED", "CONNECT_OFFER_ENABLED",
              "GCAL_ENABLED", "BCOURSES_ENABLED", "WATER_OFFER_ENABLED", "WATER_REMINDERS_ENABLED",
              "REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", False)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s3cret-internal")
    monkeypatch.setattr(config, "SETUP_STEP_QUIET_MINUTES", 20)
    monkeypatch.setattr(config, "SETUP_WINDOW_HOURS", 48)
    monkeypatch.setattr(config, "WATER_OFFER_MIN_HOURS_ONBOARDED", 0)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", False)


def _user(db, **kw):
    base = dict(preferred_channel="imessage", onboarding_step=2, calorie_target=None, protein_target=None,
                gender="male", weight_lbs=170, height_ft=5, height_in=10, age=21, goal="fat_loss,muscle_building,strength",
                workout_days=4, workout_time="18:00", current_split="ppl", occupation="cs student at berkeley",
                wake_time="00:00", sleep_time="23:59", lift_anchors=dict(ANCHORS))
    base.update(kw)
    return make_user(db, **base)


def _inbound(uid, text="ok", ago_min=0):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=uid, direction="in", body=text, channel="imessage",
                      created_at=_now() - timedelta(minutes=ago_min)))
        s.commit()
    finally:
        s.close()


def _backdate(uid, minutes):
    """Everything about this user happened `minutes` ago (completion + every message)."""
    from models import get_session, Message, User
    s = get_session()
    try:
        u = s.get(User, uid)
        u.onboarding_completed_at = _now() - timedelta(minutes=minutes)
        for m in s.query(Message).filter(Message.user_id == uid).all():
            m.created_at = _now() - timedelta(minutes=minutes)
        s.commit()
    finally:
        s.close()


# ── completion: summary + rundown, the card deferred ─────────────────────────

def test_completion_sends_the_two_bubble_summary_and_defers_the_card(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                                      anthropic_stub, caplog):
    import onboarding_agent as oa
    from profile_page import profile_url
    caplog.set_level(logging.INFO)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    b = sidecar_ok
    assert b[0] == ("ok so far this is what i have: ur 5'10 170, training 4 days a week in the evenings, "
                    "up at 12 down by 11:59, tryna lose fat while building muscle and getting stronger"), b[0]
    assert b[1].startswith("im thinking ") and " cal and " in b[1] and oa.SUMMARY_CLOSER in b[1], b[1]
    assert b[1].endswith("\n" + profile_url(_u(u.id))), b[1]
    assert len(b) == 2 and card_ok["sent"] == []
    row = _u(u.id)
    assert row.onboarding_completed_at and row.card_setup_at is None
    assert "CARD_SETUP_DEFERRED" in caplog.text


def test_a_workout_ask_still_gets_the_card_right_then(db, seq_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    import onboarding_agent as oa
    from workouts.card_setup import EXTENSION_INTRO, BREAKDOWN_SETUP
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "send me the workout", early=True)
    assert sidecar_ok[2:4] == list(EXTENSION_INTRO) and sidecar_ok[-3:-1] == list(BREAKDOWN_SETUP)
    assert sidecar_ok[-1].startswith("one thing — those are starter push day exercises")   # the defaults note
    assert len(card_ok["sent"]) == 1 and _u(u.id).card_setup_at


# ── the sequence while engaged: one step per text ─────────────────────────────

def test_on_inbound_walks_offers_then_the_card_one_step_per_text(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                                  anthropic_stub):
    import onboarding_agent as oa
    import setup_sequence as ss
    from connect_offers import OFFER_GCAL_ASK, OFFER_BCOURSES
    from water_offer import OFFER_TEXT
    from workouts.card_setup import EXTENSION_INTRO, BREAKDOWN_SETUP
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db, existing_tools="fitbit")
    oa._complete_onboarding(_u(u.id), "yes")
    sidecar_ok.clear()

    assert ss.on_inbound(u.id) is None, "the summary just went — wait for them (or 20 quiet minutes)"
    _inbound(u.id, "cool")
    assert ss.on_inbound(u.id) == "connect:gcal" and sidecar_ok == [OFFER_GCAL_ASK]
    assert ss.on_inbound(u.id) is None, "the offer is unanswered: no second step"
    _inbound(u.id, "edwin@berkeley.edu")
    assert ss.on_inbound(u.id) == "connect:bcourses" and sidecar_ok[-1] == OFFER_BCOURSES
    assert ss.on_inbound(u.id) is None
    _inbound(u.id, "ok will do")
    assert ss.on_inbound(u.id) == "water:sent" and sidecar_ok[-1] == OFFER_TEXT, "the quick yes/no, before the card"
    assert _u(u.id).water_offer_status == "offered"
    assert ss.on_inbound(u.id) is None
    _inbound(u.id, "yes")
    r = ss.on_inbound(u.id)
    assert r == "card:sent", r
    assert sidecar_ok[3:5] == list(EXTENSION_INTRO) and sidecar_ok[-3:-1] == list(BREAKDOWN_SETUP)
    assert len(card_ok["sent"]) == 1 and _u(u.id).card_setup_at
    _inbound(u.id, "nice")
    assert ss.on_inbound(u.id) is None, "setup is done — nothing repeats"
    assert sorted(_u(u.id).connect_offers) == ["bcourses", "gcal"], "no wearable offer until Google approves the API"


def test_a_quiet_twenty_minutes_also_settles_a_step(db, seq_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    import onboarding_agent as oa
    import setup_sequence as ss
    from connect_offers import OFFER_GCAL_ASK
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    sidecar_ok.clear()
    _backdate(u.id, 25)
    _inbound(u.id, "hey")              # they text after a quiet half hour
    assert ss.on_inbound(u.id) == "connect:gcal" and sidecar_ok == [OFFER_GCAL_ASK]


# ── the sequence when the conversation died down: the sweep ───────────────────

def test_sweep_sends_the_card_only_when_the_offers_are_done_and_it_is_quiet(db, seq_on, sidecar_ok, card_ok,
                                                                             sync_threads, anthropic_stub, monkeypatch):
    import onboarding_agent as oa
    import setup_sequence as ss
    from workouts.card_setup import EXTENSION_INTRO
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    sidecar_ok.clear()
    assert ss.sweep() == 0, "offers still owed — the card waits"
    monkeypatch.setattr(config, "CONNECT_OFFER_ENABLED", False)       # nothing to offer → water is next
    assert ss.sweep() == 0, "but the summary just went out — not quiet yet"
    _backdate(u.id, 25)
    from water_offer import OFFER_TEXT
    assert ss.sweep() == 1 and sidecar_ok == [OFFER_TEXT], "water before the card"
    assert ss.sweep() == 0, "the water ask is unanswered: the card waits (the heartbeat's anti-stack gate)"
    _inbound(u.id, "yes")                # answered — and then the conversation goes quiet again
    _backdate(u.id, 35)
    assert ss.sweep() == 1
    assert sidecar_ok[1:3] == list(EXTENSION_INTRO) and len(card_ok["sent"]) == 1
    assert ss.sweep() == 0, "once"


def test_sweep_respects_the_heartbeat_guardrails(db, seq_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, monkeypatch):
    import onboarding_agent as oa
    import setup_sequence as ss
    monkeypatch.setattr(config, "CONNECT_OFFER_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    _backdate(u.id, 25)
    _inbound(u.id, "hey", ago_min=5)                                   # active conversation
    assert ss.sweep() == 0 and card_ok["sent"] == []


def test_sequence_off_keeps_the_kickoff_card(db, seq_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    assert len(card_ok["sent"]) == 1


# ── connect offers: the setup window ──────────────────────────────────────────

def test_setup_window_waives_the_day_wait_but_not_for_old_users(db, seq_on, sms_capture):
    from connect_offers import in_setup_window, sweep, OFFER_GCAL_ASK
    fresh = make_user(db, occupation="student", activated_at=_now() - timedelta(hours=1),
                      created_at=_now() - timedelta(hours=1), onboarding_completed_at=_now() - timedelta(minutes=45),
                      wake_time="00:00", sleep_time="23:59")
    legacy = make_user(db, occupation="student", activated_at=_now() - timedelta(hours=3),
                       created_at=_now() - timedelta(hours=3), onboarding_completed_at=None,
                       wake_time="00:00", sleep_time="23:59")
    assert in_setup_window(fresh) is True and in_setup_window(legacy) is False
    from setup_sequence import sweep as setup_sweep
    assert sweep(_now()) == 0, "inside the window the setup sequence is the sender (one step per tick, in order)"
    assert setup_sweep(_now()) == 1 and [b for _p, b in sms_capture] == [OFFER_GCAL_ASK]
    assert "gcal" in (_u(fresh.id).connect_offers or {}) and not (_u(legacy.id).connect_offers or {})
    old = make_user(db, onboarding_completed_at=_now() - timedelta(hours=72))
    assert in_setup_window(old) is False


# ── water waits for day two ───────────────────────────────────────────────────

def test_water_is_a_setup_step_not_a_day_two_thing(db, seq_on, monkeypatch):
    from water_offer import eligible
    today = make_user(db, onboarding_completed_at=_now() - timedelta(hours=2))
    assert eligible(today) is True
    monkeypatch.setattr(config, "WATER_OFFER_MIN_HOURS_ONBOARDED", 20)     # the knob still works for existing users
    assert eligible(today) is False and eligible(make_user(db, onboarding_completed_at=None)) is True


def test_reactive_fitbit_link_and_block_say_not_live_yet(db, seq_on, monkeypatch):
    from agent_tools import handle_send_connect_link
    from connect_offers import integrations_block
    u = make_user(db)
    r = handle_send_connect_link(u.id, {"provider": "google_health"})
    assert r.startswith("error:") and "isn't live yet" in r and "screenshot" in r
    block = integrations_block(u, None, None)
    assert "google_health: NOT live yet" in block and "screenshot" in block
    monkeypatch.setattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", True)
    assert "NOT live yet" not in integrations_block(u, None, None)


def test_no_wearable_offer_until_google_approves(db, seq_on, monkeypatch):
    from connect_offers import first_offer_candidates
    from models import get_session, User
    u = make_user(db, occupation="student", existing_tools="pixel watch", onboarding_completed_at=_now(),
                  connect_offers={"gcal": _now().isoformat(), "bcourses": _now().isoformat()})
    s = get_session()
    try:
        assert first_offer_candidates(s, s.get(User, u.id)) == []
        monkeypatch.setattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", True)
        assert [c[1] for c in first_offer_candidates(s, s.get(User, u.id))] == ["google_health"]
    finally:
        s.close()


# ── the coach knows a planned setup card is not a session ─────────────────────

def test_context_line_says_the_setup_card_is_not_a_session(db, seq_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    import onboarding_agent as oa
    from workouts.card_setup import context_line
    from workouts.start import start_workout_session
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes", early=True)
    ctx = context_line(_u(u.id)) or ""
    assert "SETUP card" in ctx and "Never tell them to start it now" in ctx and "'so what now'" in ctx
    from workouts.card_setup import TOUR
    assert "HOW THE CARD WORKS" in ctx and "ONLY if they ask" in ctx and TOUR[0] in ctx, "the tour lives in context, on ask"
    start_workout_session(u.id)                       # heading in: the planned card becomes the live one
    assert "SETUP card" not in (context_line(_u(u.id)) or "")


# ── copy ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("goal, want", [
    ("muscle_building", "tryna build muscle"),
    ("fat_loss,muscle_building", "tryna lose fat while building muscle"),
    ("fat_loss,muscle_building,strength", "tryna lose fat while building muscle and getting stronger"),
    ("general_fitness", "tryna get healthier"),
    (None, "tryna get healthier"),
])
def test_goal_clause(goal, want):
    from onboarding_agent import _goal_clause
    assert _goal_clause(goal) == want


def test_hook_promises_the_setup_and_the_walkthrough_not_the_card():
    from onboarding_agent import HOOK_TEMPLATES, HOOK_ACTIVATED_TEXT
    for t in (HOOK_TEMPLATES[0]["text"], HOOK_ACTIVATED_TEXT):
        assert "quick setup before i can actually help" in t and "run u through everything i can do" in t
        assert "first workout" not in t


def test_the_tour_ends_by_saying_what_happens_next():
    from workouts.card_setup import BREAKDOWN, BREAKDOWN_SETUP, TOUR, TOUR_OFFER, EXTENSION_INTRO
    assert BREAKDOWN == (TOUR_OFFER,) and BREAKDOWN_SETUP[0] == TOUR_OFFER, "the tour is hidden: one offer line"
    assert TOUR_OFFER.startswith("lmk if the layout's confusing") and BREAKDOWN_SETUP[-1].startswith("nothing to do rn")
    assert len(TOUR) == 3 and "slide the bar" in TOUR[-1]
    assert "gamepigeon" in EXTENSION_INTRO[1] and EXTENSION_INTRO[1].endswith("tap the card below to add it")


def test_columns_and_scheduler_job():
    import migrate
    sql = "\n".join(migrate.MIGRATIONS) if hasattr(migrate, "MIGRATIONS") else open("migrate.py").read()
    assert "onboarding_completed_at" in sql and "sleep_estimated" in sql
    src = open("scheduler.py").read()
    assert "setup_sequence_sweep" in src


@pytest.fixture(autouse=True)
def _legacy_completion_shape(monkeypatch):
    """These tests pin the 2026-10-09 shape (two-bubble summary + tiered rundown), still
    supported behind the flags; the 2026-10-10 one-bubble shape is tests/tier1/test_short_completion.py."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "ONBOARDING_SUMMARY_ONE_BUBBLE", False)
    monkeypatch.setattr(_cfg, "ONBOARDING_RUNDOWN_STYLE", "tiered")
