"""
workouts/review.py — the coach's reasoning around a session (founder, 2026-10-18: the
program builder gave the structure, "but it's still missing the knowledge and
responsiveness of a real coach"). Post-session review with bounded NEXT targets that
become the next card's numbers; pre-session plan text with the card. Mid-card
reactions deliberately not built.
"""
from __future__ import annotations

import json
import logging

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import FOUNDER, imessage_on, sidecar_ok, card_ok, sync_threads, _sets  # noqa: F401


def _is_review(kw) -> bool:
    return "SESSION FACTS" in str(kw["messages"][0]["content"])


def _is_plan(kw) -> bool:
    return "SESSION PLAN" in str(kw["messages"][0]["content"])


def _user(db, **over):
    kw = dict(preferred_channel="imessage", **FOUNDER); kw.update(over)
    return make_user(db, **kw)


def _start(user_id, key="push"):
    from workouts.start import start_workout_session
    return start_workout_session(user_id, key)["session_id"]


def _close(user_id, sid, text="135 x5"):
    from workouts.session_ops import apply_text_update, close_session
    apply_text_update(user_id, text)
    close_session(sid, via="text", fill_as_planned=True)


def test_knowledge_doc_is_the_reference_the_prompts_read():
    from workouts.review import training_knowledge
    k = training_knowledge()
    for h in ("## Principles", "Double progression", "## Readiness", "## Exercise library", "Leg press —",
              "## Injury & equipment rules", "Wrist (recent fracture/surgery)", "## Around training (nutrition)"):
        assert h in k, h


def test_session_facts_flag_what_a_coach_would_notice(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    from workouts.review import session_facts, facts_text
    from workouts.session_ops import apply_text_update, close_session
    anthropic_stub.reply_with(lambda kw: "not json")
    u = _user(db)
    sid = _start(u.id)
    apply_text_update(u.id, "150 x5")          # bench planned 135 → jumped
    apply_text_update(u.id, "150 x5")
    apply_text_update(u.id, "150 x5")
    apply_text_update(u.id, "150 x5")
    close_session(sid, via="text")              # the rest stays undone → skipped
    f = session_facts(sid)
    bench = next(e for e in f["exercises"] if e["slug"] == "bench_press")
    assert bench["jumped"] and bench["hit_all"] and not bench["missed_reps"] and bench["is_new"] and bench["best"] == (150.0, 5)
    assert any(e["skipped"] for e in f["exercises"]) and f["first"] is True and f["n_done_sessions"] == 1
    txt = facts_text(f)
    assert "FIRST EVER SESSION" in txt and "bench press: plan 135×5×4 → did 150×5 · 150×5 · 150×5 · 150×5 [hit_all, jumped]" in txt
    assert "## PROGRAM" not in txt or "week:" in txt


def test_review_goes_out_after_the_summary_and_its_next_targets_become_the_next_card(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, caplog):
    from workouts.close import FIRST_WORKOUT_LINE
    from models import User
    seen = {}

    def handler(kw):
        if _is_review(kw) and "review" in seen:                     # later reviews: bubbles only, no new targets
            return json.dumps({"bubbles": ["solid, same plan next time"]})
        if _is_review(kw):
            seen["review"] = kw
            return json.dumps({"bubbles": ["bench moved easy at 150, that start was too light", "next time we open at 160 and hold the reps"],
                               "next": {"bench_press": {"weight": 160, "reps": 5, "why": "all reps easy at 150"},
                                        "cable_fly": {"weight": 99, "reps": 12, "why": "nope"},      # 99 → bounded
                                        "nonsense": {"weight": 1, "reps": 1}},
                               "note": "starting weights were light"})
        if _is_plan(kw):
            return json.dumps({"bubbles": ["bench opens at 160 today like we said"]})
        return "ok"
    anthropic_stub.reply_with(handler)
    u = _user(db)
    sid = _start(u.id)
    caplog.set_level(logging.INFO)
    _close(u.id, sid, "150 x5")
    bodies = sidecar_ok
    i_sum = next(i for i, b in enumerate(bodies) if "lb total" in b)
    assert bodies[i_sum + 1] == "bench moved easy at 150, that start was too light" and bodies[i_sum + 2].startswith("next time we open at 160")
    assert FIRST_WORKOUT_LINE not in bodies                       # the review replaced the generic line
    assert "REVIEW_SENT" in caplog.text and "TRAINING_DECISIONS_STORED" in caplog.text
    # the system prompt carried the knowledge doc; the facts carried the flags
    sys_text = " ".join(b["text"] for b in seen["review"]["system"])
    assert "## Exercise library" in sys_text and "[hit_all, jumped]" in str(seen["review"]["messages"][0]["content"])
    db.expire_all(); row = db.get(User, u.id)
    d = row.training_decisions
    assert d["bench_press"]["weight"] == 160.0 and d["bench_press"]["reps"] == 5 and d["bench_press"]["consumed"] is False
    assert d["cable_fly"]["weight"] == 25.0                        # 99 asked; best 20 → one step always allowed, the 20% cap blocks the second
    assert "nonsense" not in d
    # next push card: bench opens at 160 (source review), the plan text rides with it, the decision is consumed
    sidecar_ok.clear()
    sid2 = _start(u.id, "push")
    rows = [r for r in _sets(db, sid2) if r[0] == "bench_press"]
    from models import get_session, SetLog
    s = get_session()
    try:
        pw = s.query(SetLog.planned_weight).filter(SetLog.session_id == sid2, SetLog.exercise == "bench_press").first()[0]
    finally:
        s.close()
    assert pw == 160.0
    assert sidecar_ok[0] == "bench opens at 160 today like we said"
    db.expire_all(); assert db.get(User, u.id).training_decisions["bench_press"]["consumed"] is True
    # a third card: back to the history rule (+1 step on what they actually did)
    _close(u.id, sid2, "160 x5")
    sid3 = _start(u.id, "push")
    s = get_session()
    try:
        pw3 = s.query(SetLog.planned_weight).filter(SetLog.session_id == sid3, SetLog.exercise == "bench_press").first()[0]
    finally:
        s.close()
    assert pw3 == 165.0


def test_bounds_keep_the_models_numbers_honest():
    from workouts.review import _bound_decisions
    f = {"exercises": [
        {"slug": "leg_press", "best": (145.0, 10), "planned_weight": 130.0, "planned_reps": 10},
        {"slug": "cable_fly", "best": (20.0, 12), "planned_weight": 20.0, "planned_reps": 12},
        {"slug": "skipped_one", "best": None, "planned_weight": 50.0, "planned_reps": 10},
    ]}
    out = _bound_decisions(f, {"leg_press": {"weight": 300, "reps": 10, "why": "x"},          # capped: +2 steps (10) → 165
                               "cable_fly": {"weight": 5, "reps": 40},                          # floor 50% → 10; reps cap 20
                               "cable_fly_up": None,
                               "skipped_one": {"weight": 55, "reps": 10},                        # no done set → planned is the base
                               "ghost": {"weight": 100, "reps": 5}, "bad": {"weight": "x"}})
    assert out["leg_press"]["weight"] == 165.0 and out["leg_press"]["reps"] == 10
    assert out["cable_fly"]["weight"] == 10.0 and out["cable_fly"]["reps"] == 20
    up = _bound_decisions(f, {"cable_fly": {"weight": 99, "reps": 12}})
    assert up["cable_fly"]["weight"] == 25.0                        # one step always; the 20% cap blocks the second
    assert out["skipped_one"]["weight"] == 55.0
    assert "ghost" not in out and "bad" not in out


def test_unparseable_or_failed_review_falls_back_to_the_first_workout_line(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, caplog):
    from workouts.close import FIRST_WORKOUT_LINE
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    sid = _start(u.id)
    caplog.set_level(logging.INFO)
    _close(u.id, sid)
    assert sidecar_ok[-1] == FIRST_WORKOUT_LINE and "REVIEW_UNPARSEABLE" in caplog.text


def test_review_flag_off_is_the_old_close(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, monkeypatch):
    from workouts.close import FIRST_WORKOUT_LINE
    monkeypatch.setattr(config, "SESSION_REVIEW_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: json.dumps({"bubbles": ["should not be used"]}) if _is_review(kw) else "nope")
    u = _user(db)
    sid = _start(u.id)
    _close(u.id, sid)
    assert sidecar_ok[-1] == FIRST_WORKOUT_LINE and "should not be used" not in sidecar_ok


def test_plan_text_rides_with_the_card_with_a_demo_for_a_new_movement(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)
    seen = {}

    def handler(kw):
        if _is_plan(kw):
            seen["plan"] = str(kw["messages"][0]["content"])
            return json.dumps({"bubbles": ["legs. leg press is new — feet mid platform, don't lock the knees", "https://youtu.be/CHPHn-OnTqE"]})
        return "ok"
    anthropic_stub.reply_with(handler)
    u = _user(db, equipment="machines")
    sid = _start(u.id, "legs")
    assert sidecar_ok[0].startswith("legs. leg press is new") and sidecar_ok[1] == "https://youtu.be/CHPHn-OnTqE"
    assert "NEW TO THEM" in seen["plan"] and "demo link for a new movement: leg press: https://youtu.be/CHPHn-OnTqE" in seen["plan"]
    assert "this is their FIRST card" in seen["plan"]
    from models import User
    db.expire_all(); assert db.get(User, u.id).seen_exercise_demos.get("leg_press")


def test_plan_falls_back_to_the_generic_intro_and_never_runs_for_the_setup_card(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, monkeypatch):
    anthropic_stub.reply_with(lambda kw: "nope")
    u = _user(db)
    _start(u.id)
    assert sidecar_ok[0].startswith("push day. starting u at 135 on bench press") or sidecar_ok[0].startswith("push day.")
    calls_before = len(anthropic_stub.calls)
    from workouts.start import start_workout_session
    v = _user(db, phone="+15550009991")
    start_workout_session(v.id, "push", setup=True)
    assert len(anthropic_stub.calls) == calls_before                 # the onboarding setup card keeps the founder's intro copy
    monkeypatch.setattr(config, "PRE_SESSION_PLAN_ENABLED", False)
    w = _user(db, phone="+15550009992")
    sidecar_ok.clear(); _start(w.id)
    assert len(anthropic_stub.calls) == calls_before and sidecar_ok[0].startswith("push day.")
