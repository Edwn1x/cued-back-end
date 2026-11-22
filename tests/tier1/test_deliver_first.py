"""
Deliverable before the questionnaire (DELIVER_FIRST_ENABLED, 2026-11-22): the first card goes out
the moment the card-critical basics are in — mid-setup — and the rest of setup continues after.
Both setup surfaces: the intake agent (handle_onboarding_reply) and intake-in-the-loop.

Live 2026-11-20: Jaskirat (u96) gave split, days, height, weight and injuries by 12:03:48, was
promised the program, got a nine-question block instead, said "This is so slow", then "End".
Maddy (u100) had a full form and a correct diagnosis at 11239 and never got a card. Sarah (u56)
asked for legs twice during setup and got a full-body card after completion.
"""

from __future__ import annotations

import logging

import pytest

import config
from tests.factories import make_user

SECRET = "s3cret-internal"
# Card-critical (height_weight, workout_days, injuries, split_days) all in; sleep / cooking / steps still unknown.
READY = dict(preferred_channel="imessage", onboarding_step=2, name="Jas", age=20, gender="male",
             height_ft=6, height_in=0, weight_lbs=160, experience="beginner", equipment="full_gym",
             goal="strength", workout_days="4", workout_time="18:00", current_split="ppl", injuries="none",
             calorie_target=None, protein_target=None, wake_time=None, sleep_time=None, cooking_situation=None,
             activity_level=None, avg_steps=None, reported_intake=None, lift_anchors=None, occupation="student")


@pytest.fixture
def setup_on(monkeypatch):
    for f in ("DELIVER_FIRST_ENABLED", "ANCHOR_ASK_BESIDE_CARD_ENABLED", "CARD_SETUP_ENABLED",
              "START_WORKOUT_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED", "IMESSAGE_CHANNEL_ENABLED"):
        monkeypatch.setattr(config, f, True, raising=False)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", False)
    monkeypatch.setattr(config, "WATER_OFFER_ENABLED", False)
    monkeypatch.setattr(config, "PROGRAM_BRIEF_ENABLED", False)
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_ENABLED", False)
    monkeypatch.setattr(config, "ONBOARDING_MEALS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_FOOD_FIRST_ENABLED", False, raising=False)


@pytest.fixture
def sidecar_ok(monkeypatch):
    import sms
    calls: list = []

    def _fake(phone, body, reply_to=None):
        calls.append(body)
        return f"photon-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
    return calls


@pytest.fixture
def card_ok(monkeypatch):
    import photon_cards
    sent: list = []
    updated: list = []
    monkeypatch.setattr(photon_cards, "send_card", lambda phone, url, live=True, layout=None:
                        sent.append(layout) or {"provider_message_id": f"photon-card-{len(sent)}",
                                                 "card_session": {"id": f"photon-card-{len(sent)}"}})
    monkeypatch.setattr(photon_cards, "update_card", lambda phone, cs, url, live=None, layout=None:
                        updated.append((cs, url, layout)))
    return {"sent": sent, "updated": updated}


@pytest.fixture
def sync_threads(monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None:
                        type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


def _sessions(uid):
    from models import get_session, WorkoutSession
    s = get_session()
    try:
        return [(w.id, w.status) for w in s.query(WorkoutSession).filter(WorkoutSession.user_id == uid).order_by(WorkoutSession.id).all()]
    finally:
        s.close()


def _is_extract(kw) -> bool:
    return "Extract any fitness coaching profile data" in str(kw["messages"][0]["content"])


def _script(reply: str):
    def script(kw):
        if _is_extract(kw):
            return "{}"
        return reply
    return script


# ─── the intake agent ─────────────────────────────────────────────────────────────

def test_card_goes_the_turn_the_basics_land_and_setup_continues(db, setup_on, sidecar_ok, card_ok, sync_threads,
                                                                 anthropic_stub, caplog):
    import onboarding_agent as oa
    from workouts.card_setup import EXTENSION_INTRO, ASK_BESIDE_NEW, SETUP_NEXT_MID, TOUR_OFFER, SETUP_NEXT
    anthropic_stub.reply_with(_script("arnold split at 160, respect. ur first card's right below, we'll finish the rest after"))
    caplog.set_level(logging.INFO)
    u = make_user(db, **READY)
    assert oa.handle_onboarding_reply(_u(u.id), "nothing hurts") is False          # setup NOT closed
    row = _u(u.id)
    assert row.onboarding_step == 2 and row.card_setup_at is not None and row.calorie_target is None
    bodies = sidecar_ok
    assert bodies[0].startswith("arnold split at 160") and "?" not in bodies[0]       # no question this turn
    assert bodies[1:3] == list(EXTENSION_INTRO)
    assert bodies[3].startswith("here's ur first card, push day") and "my best guess from ur stats" in bodies[3]
    assert TOUR_OFFER in bodies and SETUP_NEXT_MID in bodies and SETUP_NEXT not in bodies
    assert ASK_BESIDE_NEW in bodies
    assert len(card_ok["sent"]) == 1 and _sessions(u.id)[-1][1] == "planned"
    assert "ONBOARDING_REPLY mode=card_now" in caplog.text and "ONBOARDING_CARD_NOW" in caplog.text
    # next turn: setup resumes with a question, no second card
    sidecar_ok.clear()
    anthropic_stub.reply_with(_script("bet. u cooking most nights or is it dining hall"))
    assert oa.handle_onboarding_reply(_u(u.id), "sick") is False
    assert sidecar_ok == ["bet. u cooking most nights or is it dining hall"]
    assert len(card_ok["sent"]) == 1
    prompt = oa._build_system_prompt(_u(u.id))
    assert "THEIR FIRST WORKOUT CARD IS ALREADY ON THEIR PHONE" in prompt


def test_completion_later_does_not_send_a_second_card(db, setup_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    import onboarding_agent as oa
    anthropic_stub.reply_with(_script("locked in"))
    u = make_user(db, **READY)
    oa.handle_onboarding_reply(_u(u.id), "nothing hurts")
    assert len(card_ok["sent"]) == 1
    sidecar_ok.clear()
    oa._complete_onboarding(_u(u.id), "yes")
    assert _u(u.id).onboarding_step == 3 and len(card_ok["sent"]) == 1
    assert not any(b.startswith("here's ur first card") for b in sidecar_ok)


def test_sms_user_and_missing_basics_and_flag_off_change_nothing(db, setup_on, sidecar_ok, card_ok, sync_threads,
                                                                 anthropic_stub, sms_capture, monkeypatch):
    import onboarding_agent as oa
    anthropic_stub.reply_with(_script("ok. u cooking most nights or is it dining hall"))
    # SMS: no card mid-setup, setup continues as before
    s = make_user(db, **dict(READY, preferred_channel="sms", phone="+15550007001"))
    assert oa.handle_onboarding_reply(_u(s.id), "nothing hurts") is False
    assert card_ok["sent"] == [] and _u(s.id).card_setup_at is None
    assert [b for _, b in sms_capture][-1].startswith("ok. u cooking")
    # a card-critical basic still missing: the question, not the card
    m = make_user(db, **dict(READY, weight_lbs=None, phone="+15550007002"))
    assert oa.handle_onboarding_reply(_u(m.id), "nothing hurts") is False
    assert card_ok["sent"] == []
    # flag off: the pre-2026-11-22 flow
    monkeypatch.setattr(config, "DELIVER_FIRST_ENABLED", False)
    f = make_user(db, **dict(READY, phone="+15550007003"))
    assert oa.handle_onboarding_reply(_u(f.id), "nothing hurts") is False
    assert card_ok["sent"] == [] and _u(f.id).card_setup_at is None


# ─── intake in the loop ───────────────────────────────────────────────────────────

def test_intake_loop_turn_sends_the_card_after_its_bubble(db, setup_on, sidecar_ok, card_ok, sync_threads,
                                                          anthropic_stub, monkeypatch, caplog):
    import app as appmod
    from workouts.card_setup import EXTENSION_INTRO, ASK_BESIDE_NEW
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_USER_IDS", frozenset())
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    seen = {}

    def script(kw):
        if _is_extract(kw):
            return "{}"
        sysb = kw.get("system") or ""
        text = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in sysb) if isinstance(sysb, list) else str(sysb)
        if "SETUP IN PROGRESS" in text:
            seen["system"] = text
            return "nothing hurting makes this easy. ur first card's right below, rest of setup after"
        return "{}"
    anthropic_stub.reply_with(script)
    caplog.set_level(logging.INFO)
    u = make_user(db, **READY)
    appmod.process_buffered_message(u.id, "nothing hurts", "freeform")
    assert "THIS TURN: the card basics just landed" in seen["system"]
    bodies = sidecar_ok
    assert bodies[0].startswith("nothing hurting makes this easy") and "?" not in bodies[0]
    assert bodies[1:3] == list(EXTENSION_INTRO) and ASK_BESIDE_NEW in bodies
    assert len(card_ok["sent"]) == 1 and _u(u.id).onboarding_step == 2
    assert "ONBOARDING_REPLY mode=card_now" in caplog.text and "path=loop" in caplog.text
    # the next intake turn's block says the card is already there
    seen.clear()

    def script2(kw):
        if _is_extract(kw):
            return "{}"
        sysb = kw.get("system") or ""
        text = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in sysb) if isinstance(sysb, list) else str(sysb)
        if "SETUP IN PROGRESS" in text:
            seen["system"] = text
            return "bet. u cooking most nights or dining hall"
        return "{}"
    anthropic_stub.reply_with(script2)
    appmod.process_buffered_message(u.id, "sick", "freeform")
    assert "THEIR FIRST WORKOUT CARD IS ALREADY ON THEIR PHONE" in seen["system"]
    assert len(card_ok["sent"]) == 1
