"""
Workout-session lifecycle (the gym-deadlock + session-type-confabulation fix,
2026-09-27). The founder said "heading to rsf for pull day", got a pull card
(active session), then a fresh card dead-locked on the open-session guard, the
coach had no on-demand close, and it confabulated the pull session as "a push
session from earlier".

This covers: reset_workout_session abandons an EMPTY active session and FINALIZES
(never abandons) one with logged sets; start_workout_session REPLACES an empty
active session instead of erroring but REFUSES to drop logged sets; the ACTIVE
WORKOUT SESSION context block carries the session's REAL template_key; everything
inert with the flags off.
"""

from __future__ import annotations

import pytest

from tests.factories import make_user, TEMPLATE_ANCHORS

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", split_pointer_day="legs",
               split_pointer_source="confirmed", height_ft=5, height_in=6, weight_lbs=139, age=20,
               gender="male", lift_anchors=TEMPLATE_ANCHORS)

SECRET = "test-internal-secret"


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


@pytest.fixture
def sidecar_ok(monkeypatch):
    import sms
    calls: list = []
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body, reply_to=None: (calls.append(body), f"photon-{len(calls)}")[1])
    return calls


@pytest.fixture
def card_ok(monkeypatch):
    import photon_cards
    sent: list = []
    monkeypatch.setattr(photon_cards, "send_card", lambda phone, url, live=True, layout=None:
                        sent.append(layout) or {"provider_message_id": f"photon-card-{len(sent)}", "card_session": {"id": f"photon-card-{len(sent)}"}})
    monkeypatch.setattr(photon_cards, "update_card", lambda *a, **k: None)
    return sent


@pytest.fixture
def sync_threads(monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), kwargs=None, daemon=None:
                        type("T", (), {"start": lambda self: target(*args, **(kwargs or {}))})())


def _status(sid):
    from models import get_session, WorkoutSession
    s = get_session()
    try:
        return s.get(WorkoutSession, sid).status
    finally:
        s.close()


def _done_count(sid):
    from models import get_session, SetLog
    s = get_session()
    try:
        return s.query(SetLog).filter(SetLog.session_id == sid, SetLog.done.is_(True)).count()
    finally:
        s.close()


# ─── reset_workout_session ───────────────────────────────────────────────────

def test_reset_abandons_an_empty_active_session(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from workouts.session_ops import reset_active_session, active_session_id
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = start_workout_session(user.id, "pull")["session_id"]
    assert _done_count(sid) == 0
    r = reset_active_session(user.id)
    assert r == {"status": "abandoned", "session_id": sid, "template_key": "pull", "sets_logged": 0}
    assert _status(sid) == "abandoned"
    assert active_session_id(user.id) is None          # cleared → a fresh card can go out


def test_reset_finalizes_a_session_with_logged_sets_no_data_loss(db, imessage_on, sidecar_ok, card_ok, sync_threads):
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update, reset_active_session
    from models import get_session, Workout
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = start_workout_session(user.id, "pull")["session_id"]
    apply_text_update(user.id, "135 x5")               # a real logged set — must NOT be discarded
    assert _done_count(sid) == 1
    r = reset_active_session(user.id)
    assert r["status"] == "finalized" and r["session_id"] == sid and r["template_key"] == "pull"
    assert r["sets_logged"] == 1
    assert _status(sid) == "done"                       # finalized, not abandoned
    assert _done_count(sid) == 1                        # the logged set survived
    # finalize ran the normal close path: legacy mirror + a summary text went out.
    s = get_session()
    try:
        assert s.query(Workout).filter_by(user_id=user.id).count() == 1
    finally:
        s.close()
    assert any("pull" in (b or "") and "lb total" in (b or "") for b in sidecar_ok)


def test_reset_with_no_active_session_is_a_clean_noop(db):
    from workouts.session_ops import reset_active_session
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    assert reset_active_session(user.id)["status"] == "none"


def test_reset_tool_handler_relays_each_outcome(db, imessage_on, sidecar_ok, card_ok, sync_threads):
    from agent_tools import dispatch_tool, _HANDLERS, handle_reset_workout_session
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update
    assert _HANDLERS.get("reset_workout_session") is handle_reset_workout_session
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    assert "no active session" in dispatch_tool("reset_workout_session", {}, user.id)
    sid = start_workout_session(user.id, "pull")["session_id"]
    out = dispatch_tool("reset_workout_session", {}, user.id)
    assert out.startswith("ok: cleared the empty pull session") and "nothing lost" in out
    sid2 = start_workout_session(user.id, "pull")["session_id"]
    apply_text_update(user.id, "135 x5")
    out2 = dispatch_tool("reset_workout_session", {}, user.id)
    assert out2.startswith("ok: finalized their pull session") and "1 logged set" in out2 and "nothing lost" in out2


# ─── start_workout_session: replace-on-active ────────────────────────────────

def test_start_replaces_an_empty_active_session_instead_of_erroring(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from workouts.session_ops import active_session_id
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    first = start_workout_session(user.id, "pull")["session_id"]
    # No dead-end: asking for a fresh card replaces the untouched active session.
    r = start_workout_session(user.id, "push")
    assert r["template_key"] == "push" and r["session_id"] != first
    assert _status(first) == "abandoned"               # the empty one retired
    assert active_session_id(user.id) == r["session_id"]


def test_start_refuses_to_drop_a_session_with_logged_sets(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = start_workout_session(user.id, "pull")["session_id"]
    apply_text_update(user.id, "135 x5")
    with pytest.raises(ValueError, match="logged set"):
        start_workout_session(user.id, "push")
    assert _status(sid) == "active" and _done_count(sid) == 1   # untouched — nothing dropped


def test_start_tool_handler_surfaces_the_logged_sets_refusal(db, imessage_on, sidecar_ok, card_ok):
    from agent_tools import dispatch_tool
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    start_workout_session(user.id, "pull")
    apply_text_update(user.id, "135 x5")
    out = dispatch_tool("start_workout_session", {"template_key": "push"}, user.id)
    assert out.startswith("error:") and "reset_workout_session" in out


# ─── active-session visibility in context ────────────────────────────────────

def test_active_session_context_block_shows_the_real_template_key(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update, active_session_brief
    from agent_loop import build_loop_context
    from models import get_session, User
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = start_workout_session(user.id, "pull")["session_id"]      # a PULL session
    brief = active_session_brief(user)
    assert brief and "## ACTIVE WORKOUT SESSION" in brief
    assert f"pull day (session #{sid})" in brief and "never a different day" in brief
    apply_text_update(user.id, "135 x5")
    assert "1 set logged" in active_session_brief(user)
    # and it reaches the real loop context, so the coach can't call pull "push".
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## ACTIVE WORKOUT SESSION" in ctx and f"pull day (session #{sid})" in ctx


def test_no_active_session_block_when_nothing_open(db):
    from workouts.session_ops import active_session_brief
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    assert active_session_brief(user) is None


# ─── everything inert with the flags off ─────────────────────────────────────

def test_reset_tool_offered_iff_flag_on(db, monkeypatch, anthropic_stub):
    import config
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "START_WORKOUT_TOOL_ENABLED", True)
    captured = {}

    def handler(kw):
        captured["tools"] = [t.get("name") for t in kw.get("tools", [])]
        return "sounds good"
    anthropic_stub.reply_with(handler)
    user = make_user(db, preferred_channel="imessage", **FOUNDER)

    monkeypatch.setattr(config, "RESET_SESSION_TOOL_ENABLED", True)
    run_agent_loop(user, "yo", "freeform")
    assert "reset_workout_session" in captured["tools"]
    monkeypatch.setattr(config, "RESET_SESSION_TOOL_ENABLED", False)
    run_agent_loop(user, "yo", "freeform")
    assert "reset_workout_session" not in captured["tools"]


def test_start_reverts_to_the_legacy_guard_when_flag_off(db, imessage_on, sidecar_ok, card_ok, monkeypatch):
    import config
    from workouts.start import start_workout_session
    monkeypatch.setattr(config, "RESET_SESSION_TOOL_ENABLED", False)
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    start_workout_session(user.id, "pull")
    with pytest.raises(ValueError, match="already open"):        # legacy dead-end preserved
        start_workout_session(user.id, "push")


def test_context_block_absent_when_flag_off(db, imessage_on, sidecar_ok, card_ok, monkeypatch):
    import config
    from workouts.start import start_workout_session
    from agent_loop import build_loop_context
    from models import get_session, User
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    start_workout_session(user.id, "pull")
    monkeypatch.setattr(config, "ACTIVE_SESSION_CONTEXT_ENABLED", False)
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## ACTIVE WORKOUT SESSION" not in ctx


def test_reset_tool_is_claimed_by_the_registry():
    from capabilities import CAPABILITIES
    assert any("reset_workout_session" in c.tools for c in CAPABILITIES)
