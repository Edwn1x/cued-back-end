"""
Burn-in finding 2026-09-15 (founder's live legs session): the terse set parser was
too eager and the coach didn't log working sets it knew. Every live string is pinned
here; see rewrite/findings/workout-log-fidelity.md.
"""

from __future__ import annotations

import json

import pytest

from tests.factories import make_user

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", split_pointer_day="pull")
EX = [("leg_press", "leg press"), ("squat", "squat"), ("bench_press", "bench press"),
      ("incline_db_press", "incline db press")]


# ─── parser: the exact live strings ─────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    # the mis-parse that logged 135×3 instead of 135×7×3 — now structured
    ("Did squats with 135 for 3 sets 7 reps each", ("set", "squat", 135.0, 3, 7)),
    ("3 sets of 7 at 135", ("set", None, 135.0, 3, 7)),
    ("135 for 3 sets of 8", ("set", None, 135.0, 3, 8)),
    ("bench 135x3x8", ("set", "bench_press", 135.0, 3, 8)),
    # miss-framed rep-only → planned weight, one set
    ("only got 3", ("set", None, None, 1, 3)),
    ("just 5", ("set", None, None, 1, 5)),
    # clean terse forms still work
    ("315 for 5", ("set", None, 315.0, 1, 5)),
    ("315x5", ("set", None, 315.0, 1, 5)),
    ("190 x4", ("set", None, 190.0, 1, 4)),
    ("did 4 at 190", ("set", None, 190.0, 1, 4)),
])
def test_parses(text, expect):
    from workouts.parse import parse_set_text
    u = parse_set_text(text, EX)
    assert u is not None, text
    assert (u.kind, u.exercise, u.weight, u.sets, u.reps) == expect


@pytest.mark.parametrize("text", [
    "I got 5",                       # "I " prefix + no weight → model logs with the real weight
    "Got 7 for my second set",       # a "set" word it can't structure → model
    "Got 7 3rd set",
    "got 5",                         # plain got-N: weight was set in conversation, code can't see it
    "5 reps",
    "hit chest today",
])
def test_ambiguous_or_weightless_reports_go_to_the_model(text):
    from workouts.parse import parse_set_text
    assert parse_set_text(text, EX) is None


# ─── apply: a structured multi-set report fills the session correctly ────────

def _open_legs(db, user):
    from workouts.start import start_workout_session
    import config
    return start_workout_session(user.id, "legs")["session_id"]


def _squat_sets(db, sid):
    from models import get_session, SetLog
    s = get_session()
    try:
        return [(x.done, x.actual_weight, x.actual_reps, x.source) for x in
                s.query(SetLog).filter_by(session_id=sid, exercise="squat").order_by(SetLog.id).all()]
    finally:
        s.close()


class _Env:
    sent = []


@pytest.fixture
def workout_env(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s")
    import photon_cards
    monkeypatch.setattr(photon_cards, "send_card", lambda phone, url, live=True, layout=None:
                        {"provider_message_id": "pc-1", "card_session": {"id": "pc-1"}})
    monkeypatch.setattr(photon_cards, "update_card", lambda *a, **k: None)
    import sms
    workout_env.sent = []
    env = _Env(); env.sent = []
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body, reply_to=None: (env.sent.append(body), "photon-x")[1])
    return env


def test_multi_set_text_logs_three_sets_not_one(db, workout_env):
    from workouts.session_ops import apply_text_update
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_legs(db, user)
    assert apply_text_update(user.id, "did squats with 135 for 3 sets 7 reps each") == "logged 3 sets."
    done = [r for r in _squat_sets(db, sid) if r[0]]
    assert len(done) == 3 and all(r[1] == 135 and r[2] == 7 and r[3] == "text" for r in done)


def test_multi_set_supersedes_an_earlier_bad_terse_squat(db, workout_env):
    """The live sequence: a bad '135 for 3' logged 135×3, then the full report corrects it —
    the text set is replaced, not stacked."""
    from workouts.session_ops import apply_text_update
    from models import get_session, WorkoutSession, SetLog
    from card_page import apply_set_update
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_legs(db, user)
    s = get_session()
    try:  # simulate the earlier mis-parse: one squat done 135×3, source text
        ws = s.get(WorkoutSession, sid)
        first = s.query(SetLog).filter_by(session_id=sid, exercise="squat").order_by(SetLog.id).first()
        apply_set_update(s, ws, first, done=True, actual_weight=135, actual_reps=3, source="text")
    finally:
        s.close()
    assert apply_text_update(user.id, "squats 135 for 3 sets 7 reps") == "logged 3 sets."
    done = [r for r in _squat_sets(db, sid) if r[0]]
    assert len(done) == 3 and all(r[2] == 7 for r in done), done   # the 135×3 is gone


def test_reconcile_keeps_card_sets_but_replaces_text_sets(db, workout_env):
    from agent_tools import _log_into_open_session
    from models import get_session, WorkoutSession, SetLog
    from card_page import apply_set_update
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_legs(db, user)
    s = get_session()
    try:
        ws = s.get(WorkoutSession, sid)
        rows = s.query(SetLog).filter_by(session_id=sid, exercise="squat").order_by(SetLog.id).all()
        apply_set_update(s, ws, rows[0], done=True, actual_weight=135, actual_reps=3, source="text")   # bad terse
        apply_set_update(s, ws, rows[1], done=True, actual_weight=155, actual_reps=5, source="card")   # real tap
    finally:
        s.close()
    _log_into_open_session(user.id, [{"name": "squat", "sets": 3, "reps": 7, "weight": 135}], None)
    done = _squat_sets(db, sid)
    done_only = [r for r in done if r[0]]
    assert (True, 155.0, 5, "card") in done_only          # the card tap survived
    assert not any(r[1] == 135 and r[2] == 3 for r in done_only)   # the bad text 135×3 is gone
    assert sum(1 for r in done_only if r[1] == 135 and r[2] == 7) == 3


def test_pipeline_logs_a_clean_multiset_and_skips_the_model(db, workout_env, client, anthropic_stub):
    import json as _j
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    _open_legs(db, user)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("clean multiset must not reach the model")))
    from tests._sync import PENDING_TIMERS
    payload = {"phone": user.phone, "text": "squats 135 for 3 sets 7 reps", "provider_message_id": "spc-ms",
               "chat_guid": "x", "service": "iMessage", "line_phone": "+1628", "timestamp": "2026-09-15T05:00:00.000Z", "attachments": []}
    client.post("/internal/inbound", data=_j.dumps(payload), headers={"X-Internal-Secret": "s"}, content_type="application/json")
    t = PENDING_TIMERS.pop(user.phone, None); assert t is not None; t.fire()
    assert "logged 3 sets." in workout_env.sent


def test_voice_rule_tells_the_coach_to_log_working_sets_it_knows():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "log_workout it" in v and "you have the weight, code doesn't" in v
    assert 'Never say "bank that number"' in v
