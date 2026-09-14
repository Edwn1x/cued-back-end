"""
Workout card Phases 3–5: the coach tool sends the session (card on iMessage, per-
exercise messages on SMS / refusal), texted deviations and closes are handled in
code with one line back, 👍 on an exercise message marks its sets, log_workout
routes into an open session, closed sessions mirror to the legacy table + split
pointer, and stale sessions are abandoned silently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

SECRET = "test-internal-secret"
FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", split_pointer_day="pull",
               split_pointer_source="confirmed", height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

    def _fake(phone, body, reply_to=None):
        calls.append(body)
        return f"photon-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
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


def _sets(db, session_id):
    from models import get_session, SetLog
    s = get_session()
    try:
        return [(x.exercise, x.set_index, x.done, x.actual_weight, x.actual_reps, x.source, x.provider_message_ref)
                for x in s.query(SetLog).filter_by(session_id=session_id).order_by(SetLog.id).all()]
    finally:
        s.close()


# ─── parser ──────────────────────────────────────────────────────────────────

EX = [("bench_press", "bench press"), ("incline_db_press", "incline db press"), ("cable_fly", "cable fly")]


@pytest.mark.parametrize("text,expect", [
    ("190 x4", ("set", None, None, 190.0, 4)),
    ("190x4", ("set", None, None, 190.0, 4)),
    ("190 for 4", ("set", None, None, 190.0, 4)),
    ("bench 190 x4", ("set", "bench_press", None, 190.0, 4)),
    ("set 3 190x4", ("set", None, 2, 190.0, 4)),
    ("did 4 at 190", ("set", None, None, 190.0, 4)),
    ("only got 3", ("set", None, None, None, 3)),
    ("skipped incline", ("skip", "incline_db_press", None, None, None)),
    ("done", ("close", None, None, None, None)),
    ("that's it", ("close", None, None, None, None)),
    ("finished", ("close", None, None, None, None)),
    ("Incline 65 × 9", ("set", "incline_db_press", None, 65.0, 9)),
])
def test_parse_set_text_forms(text, expect):
    from workouts.parse import parse_set_text
    u = parse_set_text(text, EX)
    assert u is not None, text
    assert (u.kind, u.exercise, u.set_index, u.weight, u.reps) == expect


@pytest.mark.parametrize("text", ["what should i eat", "190", "bench felt heavy", "i'm at rsf", "ok bet",
                                  "how many sets of bench", "yo", "skipped my lecture lol"])
def test_parse_set_text_ignores_non_sets(text):
    from workouts.parse import parse_set_text
    assert parse_set_text(text, EX) is None


# ─── Phase 3: the coach tool ─────────────────────────────────────────────────

def test_start_session_on_imessage_sends_intro_then_card_and_infers_the_day(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session, infer_template
    from models import get_session, WorkoutSession, Message
    user = make_user(db, preferred_channel="imessage", **FOUNDER)   # pointer pull → next is legs
    assert infer_template(user) == "legs"
    r = start_workout_session(user.id)
    assert r["template_key"] == "legs" and r["surface"] == "card" and r["sets"] == 16
    assert sidecar_ok == ["legs day. 4 sets squat, then the usual. tap as you go — text me if a set goes different."]
    assert card_ok and card_ok[0]["caption"].startswith("legs · ")
    s = get_session()
    try:
        ws = s.get(WorkoutSession, r["session_id"])
        assert ws.status == "active" and ws.started_at and ws.card_message_id == "photon-card-1"
        types = [m.message_type for m in s.query(Message).filter_by(user_id=user.id, direction="out").order_by(Message.id)]
        assert types == ["workout_intro", "workout_card"]
    finally:
        s.close()


def test_start_session_named_day_and_no_pointer_and_open_session_guard(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session, infer_template
    user = make_user(db, preferred_channel="imessage", **dict(FOUNDER, split_pointer_day=None))
    assert infer_template(user) == "push"                       # no pointer → first day of the cycle
    r = start_workout_session(user.id, "upper")                  # they named it
    assert r["template_key"] == "upper"
    with pytest.raises(ValueError, match="already open"):
        start_workout_session(user.id)
    nosplit = make_user(db, preferred_channel="imessage", **dict(FOUNDER, current_split=None, split_pointer_day=None))
    assert infer_template(nosplit) == "full_body"


def test_start_session_on_sms_sends_one_message_per_exercise_with_refs(db, sms_capture):
    from workouts.start import start_workout_session
    user = make_user(db, preferred_channel="sms", **FOUNDER)
    r = start_workout_session(user.id, "push")
    assert r["surface"] == "messages"
    bodies = [b for _, b in sms_capture]
    assert bodies[0].startswith("push day. 4 sets bench press, then the usual.") and "👍 an exercise" in bodies[0]
    assert bodies[1:] == ["bench press · 135 × 5 × 4", "incline db press · 40 × 10 × 3", "cable fly · 20 × 12 × 3", "tricep pushdown · 40 × 12 × 3"]
    refs = {ex: ref for ex, _i, _d, _w, _r, _s, ref in _sets(db, r["session_id"])}
    assert refs["bench_press"] == "SMfake0000000000000000000000000002" and len(set(refs.values())) == 4


def test_start_session_card_refused_falls_to_exercise_messages(db, imessage_on, sidecar_ok, monkeypatch):
    import photon_cards
    from workouts.start import start_workout_session

    def _refuse(*a, **k):
        raise photon_cards.CardError("sidecar /send-card 502: nope")
    monkeypatch.setattr(photon_cards, "send_card", _refuse)
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    r = start_workout_session(user.id, "push")
    assert r["surface"] == "messages"
    assert sidecar_ok[0].startswith("push day.") and sidecar_ok[1] == "bench press · 135 × 5 × 4"
    refs = [ref for *_, ref in _sets(db, r["session_id"])]
    assert all(refs) and refs[0] == "photon-2"


def test_start_tool_result_tells_the_model_to_stay_silent(db, imessage_on, sidecar_ok, card_ok):
    from agent_tools import dispatch_tool
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    assert dispatch_tool("start_workout_session", {"template_key": "arms"}, user.id).startswith("error: unknown template")
    out = dispatch_tool("start_workout_session", {}, user.id)
    assert out.startswith("ok: legs session #") and "sent as a card (16 sets)" in out and out.endswith("Reply with exactly [silent].")
    assert dispatch_tool("start_workout_session", {}, user.id).startswith("error: a session is already open")


# ─── Phase 4: texted deviations + close ──────────────────────────────────────

def _open_push(db, user, **kw):
    from workouts.start import start_workout_session
    return start_workout_session(user.id, "push")["session_id"]


def test_text_deviation_targets_the_next_undone_set_and_replies_one_line(db, imessage_on, sidecar_ok, card_ok, driver, anthropic_stub):
    from workouts.session_ops import apply_text_update
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_push(db, user)
    assert apply_text_update(user.id, "190 x4") == "swapped it in."
    rows = _sets(db, sid)
    assert rows[0][2] and rows[0][3] == 190 and rows[0][4] == 4 and rows[0][5] == "text"
    assert apply_text_update(user.id, "only got 3") == "swapped it in."     # next undone bench set, planned weight kept
    assert rows[1][0] == "bench_press" and _sets(db, sid)[1][3] == 135 and _sets(db, sid)[1][4] == 3
    assert apply_text_update(user.id, "incline 45 x 8") == "swapped it in."
    inc = [r for r in _sets(db, sid) if r[0] == "incline_db_press"]
    assert inc[0][2] and inc[0][3] == 45
    assert apply_text_update(user.id, "skipped cable fly") == "skipped it."
    assert not any(r[0] == "cable_fly" for r in _sets(db, sid))
    assert apply_text_update(user.id, "what should i eat after") is None    # normal turn


def test_text_pr_replies_the_pr_line(db, imessage_on, sidecar_ok, card_ok):
    from workouts.session_ops import apply_text_update
    from workouts.plan import build_session
    from models import get_session, SetLog, WorkoutSession
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    prior = build_session(user, "push", now=_now() - timedelta(days=7))
    s = get_session()
    try:
        for x in s.query(SetLog).filter_by(session_id=prior.id, exercise="bench_press").all():
            x.actual_weight, x.actual_reps, x.done, x.source = 185, 3, True, "card"
        s.get(WorkoutSession, prior.id).status = "done"; s.commit()
    finally:
        s.close()
    _open_push(db, user)
    assert apply_text_update(user.id, "190 x4") == "190 × 4 is a PR 🎉 last time was 185 × 3."


def test_inbound_text_with_open_session_skips_the_model_and_reacts_on_a_pr(db, imessage_on, sidecar_ok, card_ok, client, anthropic_stub, monkeypatch):
    """The pipeline: 'done'/'190 x4' never reaches the model; a PR gets ‼️ on their text."""
    import sms
    from workouts.plan import build_session
    from models import get_session, SetLog, WorkoutSession, Message
    from tests._sync import PENDING_TIMERS
    reacted = []
    monkeypatch.setattr(sms, "react_to_message", lambda uid, sid, emoji: reacted.append((sid, emoji)) or True)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("model must not run for a set text")))
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    prior = build_session(user, "push", now=_now() - timedelta(days=7))
    s = get_session()
    try:
        for x in s.query(SetLog).filter_by(session_id=prior.id, exercise="bench_press").all():
            x.actual_weight, x.actual_reps, x.done, x.source = 185, 3, True, "card"
        s.get(WorkoutSession, prior.id).status = "done"; s.commit()
    finally:
        s.close()
    _open_push(db, user)
    payload = {"phone": user.phone, "text": "190 x4", "provider_message_id": "spc-in-pr", "chat_guid": f"any;-;{user.phone}",
               "service": "iMessage", "line_phone": "+1628", "timestamp": "2026-09-14T12:00:00.000Z", "attachments": []}
    client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    t = PENDING_TIMERS.pop(user.phone, None); assert t is not None; t.fire()
    assert sidecar_ok[-1] == "190 × 4 is a PR 🎉 last time was 185 × 3."
    assert reacted == [("spc-in-pr", "emphasize")]


def test_text_close_finishes_sends_summary_and_mirrors_to_legacy(db, imessage_on, sidecar_ok, card_ok, sync_threads):
    from workouts.session_ops import apply_text_update
    from models import get_session, WorkoutSession, Workout
    from split_pointer import get_split_pointer
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_push(db, user)
    apply_text_update(user.id, "bench 135 x5")
    apply_text_update(user.id, "135x5")
    assert apply_text_update(user.id, "that's it") == ""
    s = get_session()
    try:
        ws = s.get(WorkoutSession, sid)
        assert ws.status == "done" and ws.total_volume_lb == 1350
        legacy = s.query(Workout).filter_by(user_id=user.id).one()
        assert legacy.workout_type == "push" and legacy.completed and legacy.exercises[0]["name"] == "bench press"
        assert legacy.exercises[0]["sets"] == 2 and legacy.user_notes == f"card session #{sid}"
    finally:
        s.close()
    summary = sidecar_ok[-1]
    assert summary.splitlines()[0].startswith("push · ") and "bench press — 135×5 · 135×5" in summary
    assert "1,350 lb total · 0 PRs" in summary and "two texts. that's the whole log" in summary
    p = get_split_pointer(user.id)
    assert p["day"] == "push" and p["source"] == "confirmed"      # the pointer moved with the session


# ─── Phase 5: tapbacks ───────────────────────────────────────────────────────

def test_thumbs_up_on_an_exercise_message_marks_its_sets_done(db, sms_capture, client, imessage_on):
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_tapback
    user = make_user(db, preferred_channel="sms", **FOUNDER)
    sid = start_workout_session(user.id, "push")["session_id"]
    bench_ref = next(ref for ex, *_, ref in _sets(db, sid) if ex == "bench_press")
    assert apply_tapback(user.id, bench_ref, "👍") is True
    bench = [r for r in _sets(db, sid) if r[0] == "bench_press"]
    assert all(r[2] and r[3] == 135 and r[4] == 5 and r[5] == "tapback" for r in bench)
    assert apply_tapback(user.id, "spc-unrelated", "👍") is False
    assert apply_tapback(user.id, bench_ref, "👎") is False

    # through /internal/inbound as the sidecar now forwards it
    inc_ref = next(ref for ex, *_, ref in _sets(db, sid) if ex == "incline_db_press")
    payload = {"phone": user.phone, "text": "", "provider_message_id": "spc-react-1", "chat_guid": "x", "service": "iMessage",
               "line_phone": "+1628", "timestamp": "2026-09-14T12:00:00.000Z", "attachments": [],
               "reaction": {"emoji": "👍", "target_id": inc_ref}}
    r = client.post("/internal/inbound", data=json.dumps(payload), headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True, "reaction": True, "workout_hit": True}
    assert all(r[2] for r in _sets(db, sid) if r[0] == "incline_db_press")


def test_sms_done_fills_untouched_sets_as_planned_without_counting_them_as_taps(db, sms_capture, sync_threads, monkeypatch):
    import photon_cards
    monkeypatch.setattr(photon_cards, "update_card", lambda *a, **k: None)
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update, close_session
    from models import get_session, WorkoutSession
    user = make_user(db, preferred_channel="sms", **FOUNDER)
    sid = start_workout_session(user.id, "push")["session_id"]
    apply_text_update(user.id, "190 x4")
    close_session(sid, via="text", fill_as_planned=True)
    rows = _sets(db, sid)
    assert all(r[2] for r in rows) and rows[0][5] == "text" and rows[1][5] == "coach"
    summary = sms_capture[-1][1]
    assert "one text. that's the whole log" in summary


# ─── log_workout routes into an open session; abandon sweep ─────────────────

def test_log_workout_lands_in_the_open_session(db, imessage_on, sidecar_ok, card_ok):
    from agent_tools import handle_log_workout
    from models import get_session, Workout
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_push(db, user)
    out = handle_log_workout(user.id, {"split_day": "push", "exercises": [{"name": "bench press", "sets": 2, "reps": 5, "weight": 140},
                                                                            {"name": "overhead press", "sets": 1, "reps": 8, "weight": 75}]})
    assert out.startswith("ok: logged 3 sets into today's open session")
    rows = _sets(db, sid)
    bench = [r for r in rows if r[0] == "bench_press"]
    assert bench[0][2] and bench[0][3] == 140 and bench[1][2] and not bench[2][2]
    assert any(r[0] == "overhead_press" and r[2] and r[3] == 75 for r in rows)
    s = get_session()
    try:
        assert s.query(Workout).filter_by(user_id=user.id).count() == 0   # no legacy row while the session is open
    finally:
        s.close()


def test_abandon_sweep_closes_stale_sessions_silently(db, imessage_on, sidecar_ok, card_ok):
    from workouts.session_ops import abandon_stale
    from models import get_session, WorkoutSession
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open_push(db, user)
    n_before = len(sidecar_ok)
    assert abandon_stale(now=_now() + timedelta(hours=5)) == 0
    assert abandon_stale(now=_now() + timedelta(hours=7)) == 1
    s = get_session()
    try:
        assert s.get(WorkoutSession, sid).status == "abandoned"
    finally:
        s.close()
    assert len(sidecar_ok) == n_before          # no message, ever


def test_start_tool_is_offered_and_claimed_by_the_registry(all_on=None):
    from capabilities import CAPABILITIES
    assert any("start_workout_session" in c.tools for c in CAPABILITIES)
