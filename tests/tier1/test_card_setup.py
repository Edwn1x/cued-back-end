"""
Card setup (workouts/card_setup.py): the extension framing, the first card at onboarding
completion, the one-time tour — and the start tool reusing an untouched setup card.
"""

from __future__ import annotations

import re

import pytest

from tests.factories import make_user

SECRET = "s3cret-internal"
ANCHORS = {"bench": {"weight": 135, "reps": 5, "source": "stated"},
           "squat": {"weight": 185, "reps": 5, "source": "stated"}}


@pytest.fixture
def setup_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", True)
    monkeypatch.setattr(config, "START_WORKOUT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "LOG_WORKOUT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    for f in ("WATER_OFFER_ENABLED", "WATER_REMINDERS_ENABLED", "REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True)


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
        return [(w.id, w.status, w.card_session) for w in
                s.query(WorkoutSession).filter(WorkoutSession.user_id == uid).order_by(WorkoutSession.id).all()]
    finally:
        s.close()


def _imsg_user(db, **kw):
    base = dict(preferred_channel="imessage", onboarding_step=2, calorie_target=None, protein_target=None,
                gender="male", weight_lbs=170, height_in=70, age=21, goal="build_muscle",
                workout_days=4, workout_time="18:00", current_split="ppl",
                lift_anchors=dict(ANCHORS))
    base.update(kw)
    return make_user(db, **base)


def test_completion_sends_framing_first_card_and_tour_and_defers_water(db, setup_on, sidecar_ok, card_ok,
                                                                        sync_threads, anthropic_stub):
    import onboarding_agent as oa
    from workouts.card_setup import EXTENSION_INTRO, BREAKDOWN
    from water_offer import OFFER_TEXT
    anthropic_stub.reply_with(lambda kw: "locked in. lets go")
    u = _imsg_user(db)
    oa._complete_onboarding(_u(u.id), "yes")

    bodies = sidecar_ok
    assert bodies[0] == "locked in. lets go"
    assert bodies[1:3] == list(EXTENSION_INTRO)
    intro = bodies[3]
    assert intro.startswith("here's ur first card") and "tap it now so ur set for 6pm" in intro, intro
    assert re.search(r"starting u at \d+ on bench press — weights are off what u told me", intro), intro
    assert bodies[4:7] == list(BREAKDOWN)
    assert OFFER_TEXT not in bodies                       # water waits for its sweep
    assert len(card_ok["sent"]) == 1 and card_ok["sent"][0]["caption"].startswith("push")
    row = _u(u.id)
    assert row.card_setup_at and row.card_explained_at and not row.card_opened_at
    assert row.water_offer_status is None
    assert _sessions(u.id) == [(_sessions(u.id)[0][0], "planned", {"id": "photon-card-1"})]


def test_no_lifts_asks_first_and_the_answer_sends_the_setup_card(db, setup_on, sidecar_ok, card_ok,
                                                                  sync_threads, anthropic_stub):
    import onboarding_agent as oa
    from workouts.card_setup import ASK_NEW, EXTENSION_INTRO, BREAKDOWN
    from workouts.calibrate import peek_pending_card, peek_pending_setup, handle_pending_card_reply
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _imsg_user(db, experience="beginner", lift_anchors=None)
    oa._complete_onboarding(_u(u.id), "yes")
    assert sidecar_ok == ["locked in", ASK_NEW]
    assert card_ok["sent"] == []
    assert peek_pending_card(u.id) == "push" and peek_pending_setup(u.id) is True

    assert handle_pending_card_reply(u.id, "bench 65 for 5") is True
    bodies = sidecar_ok[2:]
    assert bodies[:2] == list(EXTENSION_INTRO)
    assert bodies[2].startswith("here's ur first card") and re.search(r"starting u at \d+ on bench press", bodies[2]), bodies[2]
    assert "weights are off what u told me" in bodies[2]
    assert bodies[3:6] == list(BREAKDOWN)
    assert len(card_ok["sent"]) == 1
    assert peek_pending_card(u.id) is None


def test_no_clue_answer_sends_the_estimated_setup_card(db, setup_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    import onboarding_agent as oa
    from workouts.calibrate import handle_pending_card_reply
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _imsg_user(db, experience="none", lift_anchors=None)
    oa._complete_onboarding(_u(u.id), "yes")
    assert handle_pending_card_reply(u.id, "no clue, never really lifted") is True
    intro = [b for b in sidecar_ok if b.startswith("here's ur first card")]
    assert intro and "my best guess from ur stats" in intro[0], sidecar_ok
    assert len(card_ok["sent"]) == 1


def test_gym_time_reuses_the_untouched_setup_card_in_place(db, setup_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    """'starting push' after the setup card: no 'already open' refusal, the old bubble is
    edited to the new session, the one-line extension reminder (card never opened), no tour again."""
    import onboarding_agent as oa
    from workouts.start import start_workout_session
    from workouts.card_setup import EXTENSION_INTRO, EXTENSION_REMINDER, BREAKDOWN
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _imsg_user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    setup_sid = _sessions(u.id)[0][0]
    del sidecar_ok[:]

    r = start_workout_session(u.id)
    assert r["surface"] == "card" and r["setup"] is False and r["session_id"] != setup_sid
    assert sidecar_ok[0] == EXTENSION_REMINDER
    assert sidecar_ok[1].startswith("push day.") and "tap it now" not in sidecar_ok[1]
    assert not any(b in BREAKDOWN or b in EXTENSION_INTRO for b in sidecar_ok)
    assert len(card_ok["sent"]) == 1                      # no second bubble
    assert len(card_ok["updated"]) == 1 and card_ok["updated"][0][0] == {"id": "photon-card-1"}
    rows = _sessions(u.id)
    assert rows[0] == (setup_sid, "abandoned", {"id": "photon-card-1"})
    assert rows[1][1] == "active" and rows[1][2] == {"id": "photon-card-1"}


def test_a_touched_session_is_still_refused(db, setup_on, sidecar_ok, card_ok, sync_threads):
    from workouts.start import start_workout_session
    from card_page import apply_set_update
    from models import get_session, WorkoutSession, SetLog
    u = _imsg_user(db, onboarding_step=3)
    r = start_workout_session(u.id)
    s = get_session()
    try:
        ws = s.get(WorkoutSession, r["session_id"])
        row = s.query(SetLog).filter(SetLog.session_id == ws.id).first()
        apply_set_update(s, ws, row, done=True, source="card")
    finally:
        s.close()
    with pytest.raises(ValueError, match="already open"):
        start_workout_session(u.id)


def test_opening_the_card_stamps_the_user_and_silences_the_extension_line(db, setup_on, sidecar_ok, card_ok,
                                                                          sync_threads, client):
    from workouts.start import start_workout_session
    from workouts.card_setup import EXTENSION_INTRO, EXTENSION_REMINDER
    from card_page import card_token
    u = _imsg_user(db, onboarding_step=3)
    r = start_workout_session(u.id, setup=True)
    assert sidecar_ok[:2] == list(EXTENSION_INTRO)
    tok = card_token(u.id, r["session_id"])
    assert client.get("/card/api/session", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert _u(u.id).card_opened_at is not None
    del sidecar_ok[:]
    start_workout_session(u.id)           # gym time: reuse, and no extension talk at all
    assert not any(b == EXTENSION_REMINDER or b in EXTENSION_INTRO for b in sidecar_ok), sidecar_ok
    assert sidecar_ok[0].startswith("push day.")


def test_sms_user_gets_no_setup_and_water_waits_for_the_sweep(db, setup_on, sms_capture, card_ok, anthropic_stub):
    import onboarding_agent as oa
    from water_offer import OFFER_TEXT
    from workouts.card_setup import EXTENSION_INTRO
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _imsg_user(db, preferred_channel="sms")
    oa._complete_onboarding(_u(u.id), "yes")
    bodies = [b for _p, b in sms_capture]
    assert bodies == ["locked in"], bodies
    assert OFFER_TEXT not in bodies and card_ok["sent"] == []
    assert _u(u.id).card_setup_at is None


def test_flag_off_keeps_the_kickoff_water_offer(db, setup_on, sidecar_ok, card_ok, anthropic_stub, monkeypatch):
    import config
    import onboarding_agent as oa
    from water_offer import OFFER_TEXT
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _imsg_user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    assert sidecar_ok == ["locked in", OFFER_TEXT]
    assert card_ok["sent"] == []


def test_refused_card_in_setup_sends_one_line_not_exercise_texts(db, setup_on, sidecar_ok, sync_threads, monkeypatch):
    import photon_cards
    from workouts.start import start_workout_session
    from workouts.card_setup import REFUSED_LINE, BREAKDOWN

    def _refuse(*a, **k):
        raise photon_cards.CardError("sidecar /send-card 502: nope")
    monkeypatch.setattr(photon_cards, "send_card", _refuse)
    u = _imsg_user(db, onboarding_step=3)
    r = start_workout_session(u.id, setup=True)
    assert r["surface"] == "refused"
    assert sidecar_ok[-1] == REFUSED_LINE
    assert not any(" × " in b for b in sidecar_ok), sidecar_ok     # no per-exercise texts
    assert not any(b in BREAKDOWN for b in sidecar_ok)
    assert _sessions(u.id)[0][1] == "abandoned"


def test_the_sweep_offers_water_once_the_conversation_is_quiet(db, setup_on, sidecar_ok, card_ok, sync_threads,
                                                                anthropic_stub, monkeypatch):
    """With the setup step on, the water offer's sweep is the path: blocked while the
    onboarding conversation is fresh, sent once it's ~30 min quiet."""
    import config
    import onboarding_agent as oa
    from water_offer import sweep, OFFER_TEXT
    from datetime import datetime, timezone, timedelta
    from models import get_session, Message
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _imsg_user(db, wake_time="00:00", sleep_time="23:59")
    oa._complete_onboarding(_u(u.id), "yes")
    s = get_session()
    try:
        s.add(Message(user_id=u.id, direction="in", body="yes", channel="imessage",
                      created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)))
        s.commit()
    finally:
        s.close()
    assert sweep() == 0 and OFFER_TEXT not in sidecar_ok
    s = get_session()
    try:
        for m in s.query(Message).filter(Message.user_id == u.id).all():
            m.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=45)
        s.commit()
    finally:
        s.close()
    assert sweep() == 1 and sidecar_ok[-1] == OFFER_TEXT


def test_columns_capability_and_voice_rule(db):
    from models import User
    for c in ("card_setup_at", "card_opened_at", "card_explained_at"):
        assert hasattr(User, c), c
    import migrate
    src = open(migrate.__file__).read()
    for c in ("card_setup_at", "card_opened_at", "card_explained_at"):
        assert f"ADD COLUMN IF NOT EXISTS {c}" in src, c
    voice = " ".join(open("prompts/voice.md").read().lower().split())
    assert "gamepigeon" in voice and "never call it an app" in voice


def test_a_timed_out_untouched_card_is_reused_too(db, setup_on, sidecar_ok, card_ok, sync_threads):
    """The 6h sweep abandoned the setup card before they ever lifted: the next start still
    edits that bubble instead of stacking a second one."""
    from workouts.start import start_workout_session
    from workouts.session_ops import abandon_stale
    from datetime import datetime, timezone, timedelta
    u = _imsg_user(db, onboarding_step=3)
    r = start_workout_session(u.id, setup=True)
    assert abandon_stale(now=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)) == 1
    assert _sessions(u.id)[0][1] == "abandoned"
    r2 = start_workout_session(u.id)
    assert r2["surface"] == "card" and r2["session_id"] != r["session_id"]
    assert len(card_ok["sent"]) == 1 and len(card_ok["updated"]) == 1
    assert _sessions(u.id)[-1][2] == {"id": "photon-card-1"}
