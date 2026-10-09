"""
First-card calibration (workouts/calibrate.py).

Live 2026-09-15 (user 33): a 5'0", 137 lb woman who had never trained got the
generic push card — bench 135 × 5 — and did 35. The first card's loads now come
from, strongest first: a lift they STATED (set_lift_anchors / onboarding capture),
a RELATED lift they've stated or logged (bench scales incline / fly / pushdown), or
strength standards from sex × bodyweight × level. No profile at all → the old
template default. A trained user's first card asks for their numbers first.
"""

from __future__ import annotations

import pytest

from tests.factories import make_user

KRLA = dict(name="Krla", onboarding_step=3, current_split="ppl", gender="female", age=20,
            height_ft=5, height_in=0, weight_lbs=137, experience="none", equipment="full_gym")
ANGEL = dict(name="Angel", onboarding_step=3, current_split="ppl", gender="male", age=20,
             height_ft=5, height_in=11, weight_lbs=180, experience="intermediate", equipment="full_gym")
SARAH = dict(name="sarah", onboarding_step=3, current_split="none", gender="female", age=19,
             height_ft=5, height_in=3, weight_lbs=None, experience="beginner", equipment="full_gym")


def _plan(session_id):
    from models import get_session, SetLog
    s = get_session()
    try:
        out = {}
        for x in s.query(SetLog).filter_by(session_id=session_id).order_by(SetLog.id).all():
            out.setdefault(x.exercise, []).append((x.planned_weight, x.planned_reps))
        return out
    finally:
        s.close()


def _first(plan, slug):
    return plan[slug][0]


# ─── layer 3: strength standards from the profile ─────────────────────────────

def test_never_trained_woman_gets_the_bar_not_135(db):
    """User 33's card: bench 135 → 45 (the empty bar), and every accessory scales
    with her — 10 lb dumbbells, a 10 lb stack, not 40s."""
    from workouts.plan import build_session
    user = make_user(db, **KRLA)
    plan = _plan(build_session(user, "push").id)
    assert _first(plan, "bench_press") == (45.0, 5)
    assert _first(plan, "incline_db_press") == (10.0, 10)
    assert _first(plan, "cable_fly") == (10.0, 12)
    assert _first(plan, "tricep_pushdown") == (10.0, 12)
    legs = _plan(build_session(user, "legs").id)
    assert _first(legs, "squat") == (50.0, 5) and _first(legs, "leg_press") == (80.0, 10)


def test_intermediate_man_gets_his_bodyweight_class_numbers(db):
    """180 lb intermediate male: bench ≈ 1.0×BW e1RM → ~140 for 5 after the
    first-session buffer, squat ~195, deadlift ~245 — near his real numbers, not 135/155/185."""
    from workouts.plan import build_session
    user = make_user(db, **ANGEL)
    push = _plan(build_session(user, "push").id)
    legs = _plan(build_session(user, "legs").id)
    pull = _plan(build_session(user, "pull").id)
    assert _first(push, "bench_press") == (140.0, 5)
    assert _first(legs, "squat") == (195.0, 5)
    assert _first(pull, "deadlift") == (245.0, 5)
    assert _first(pull, "lat_pulldown") == (115.0, 10)


def test_missing_bodyweight_falls_back_to_a_sex_default_not_the_template(db):
    """Sarah (user 44) has no weight on file yet; a 140 lb novice woman is a far
    better guess than the 135 lb bench."""
    from workouts.plan import build_session
    user = make_user(db, **SARAH)
    plan = _plan(build_session(user, "legs").id)
    assert _first(plan, "squat") == (75.0, 5)
    assert _first(plan, "romanian_deadlift") == (70.0, 8)
    assert _first(plan, "leg_press") == (130.0, 10)


def test_every_load_is_plate_loadable(db):
    """Barbell lifts multiples of 5 and never under the bar; stacks/dumbbells multiples of 5."""
    from workouts.plan import build_session
    from workouts.calibrate import BAR
    for prof in (KRLA, ANGEL, SARAH):
        user = make_user(db, **prof)
        for key in ("push", "pull", "legs", "upper", "full_body"):
            for slug, sets in _plan(build_session(user, key).id).items():
                w = sets[0][0]
                assert w % 5 == 0, (prof["name"], slug, w)
                if slug in ("bench_press", "squat", "deadlift", "overhead_press", "barbell_row", "romanian_deadlift"):
                    assert w >= BAR, (prof["name"], slug, w)


def test_bodyweight_templates_are_untouched(db):
    from workouts.plan import build_session
    user = make_user(db, **dict(KRLA, equipment="bodyweight"))
    plan = _plan(build_session(user, "push").id)
    assert all(w == 0 for sets in plan.values() for w, _r in sets)


# ─── layer 1 + 2: anchors and related lifts ─────────────────────────────────

def test_a_stated_lift_sets_that_lift_and_its_family(db):
    """"i bench 185 for 5" → bench 185, incline / fly / pushdown scaled from it;
    the legs day stays profile-based (no squat stated)."""
    from workouts.plan import build_session
    from workouts.calibrate import set_anchors
    user = make_user(db, **ANGEL)
    r = set_anchors(user.id, [{"exercise": "bench", "weight": 185, "reps": 5}], source="model")
    assert r == {"saved": {"bench_press": "185×5"}, "rejected": []}
    db.expire_all()
    from models import User
    user = db.get(User, user.id)
    push = _plan(build_session(user, "push").id)
    assert _first(push, "bench_press") == (185.0, 5)
    assert _first(push, "incline_db_press") == (60.0, 10)       # 0.28 × 216 e1RM
    assert _first(push, "tricep_pushdown") == (65.0, 12)
    legs = _plan(build_session(user, "legs").id)
    assert _first(legs, "squat") == (195.0, 5)                  # unchanged: profile


def test_an_anchor_under_the_bar_is_kept_not_floored(db):
    """User 33 benched 35 (a fixed bar). Her card must say 35, not the 45 bar."""
    from workouts.plan import build_session
    from workouts.calibrate import set_anchors
    user = make_user(db, **KRLA)
    set_anchors(user.id, [{"exercise": "bench press", "weight": 35, "reps": 5}], source="onboarding")
    db.expire_all()
    from models import User
    user = db.get(User, user.id)
    assert _first(_plan(build_session(user, "push").id), "bench_press") == (35.0, 5)


def test_a_logged_lift_scales_the_related_lifts_next_time(db):
    """Krla logs bench 35×5 on her first card; her next push card's accessories
    follow that, and history beats the stated number when both exist."""
    from workouts.plan import build_session
    from workouts.calibrate import set_anchors
    from models import get_session, SetLog, User
    user = make_user(db, **KRLA)
    set_anchors(user.id, [{"exercise": "bench", "weight": 95}], source="model")   # optimistic guess, reps default 5
    db.expire_all()
    user = db.get(User, user.id)
    first = build_session(user, "push")
    assert _first(_plan(first.id), "bench_press") == (95.0, 5)
    s = get_session()
    try:
        for x in s.query(SetLog).filter_by(session_id=first.id, exercise="bench_press").all():
            x.actual_weight, x.actual_reps, x.done, x.source = 35, 5, True, "card"
        s.commit()
    finally:
        s.close()
    second = _plan(build_session(user, "push").id)
    assert _first(second, "bench_press") == (40.0, 5)            # hit every rep → +5 on what she DID
    assert _first(second, "incline_db_press") == (10.0, 10)      # 0.28 × 41 e1RM → 11 → 10 (history, not the 95)


def test_anchor_rejects_goals_and_unknown_lifts():
    from workouts.calibrate import parse_stated_anchors, anchor_slug
    assert parse_stated_anchors("i bench 135 and squat 185 for 5") == [
        {"exercise": "bench", "weight": 135}, {"exercise": "squat", "weight": 185, "reps": 5}]
    assert parse_stated_anchors("my deadlift is around 225") == [{"exercise": "deadlift", "weight": 225}]
    assert parse_stated_anchors("wanna bench 225 by december") == []
    assert parse_stated_anchors("goal is to squat 315") == []
    assert parse_stated_anchors("gym at 5, class from 12-1") == []
    assert parse_stated_anchors("i weigh 135") == []
    assert anchor_slug("bench") == "bench_press" and anchor_slug("ohp") == "overhead_press"
    assert anchor_slug("burpees") is None


def test_set_anchors_validates_and_merges(db):
    from workouts.calibrate import set_anchors, anchors_of
    from models import User
    user = make_user(db, **ANGEL)
    r = set_anchors(user.id, [{"exercise": "bench", "weight": 185}, {"exercise": "burpee", "weight": 10},
                              {"exercise": "squat", "weight": 0}], source="model")
    assert r["saved"] == {"bench_press": "185×5"} and set(r["rejected"]) == {"burpee", "squat"}
    set_anchors(user.id, [{"exercise": "squat", "weight": 225, "reps": 3}], source="model")
    db.expire_all()
    a = anchors_of(db.get(User, user.id))
    assert a["bench_press"]["weight"] == 185 and a["squat"] == {**a["squat"], "weight": 225, "reps": 3}


# ─── the first-card ask ──────────────────────────────────────────────────────

def test_trained_users_first_card_asks_for_their_numbers_first(db, monkeypatch):
    """Intermediate, nothing on file → the tool answers with an ask (both branches
    offered: set_lift_anchors, or no_anchors=true). After anchors → the card."""
    from agent_tools import handle_start_workout_session, handle_set_lift_anchors
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    user = make_user(db, **ANGEL)
    out = handle_start_workout_session(user.id, {"template_key": "push"})
    assert out.startswith("error: first card") and "set_lift_anchors" in out and "no_anchors=true" in out
    assert sent == []
    # The answer sends the parked day's card IN CODE (live: the model saved anchors and
    # said "got it" without calling the start tool again).
    out = handle_set_lift_anchors(user.id, {"lifts": [{"exercise": "bench", "weight": 185, "reps": 5}]})
    # ANGEL has no saved push routine → the result flags the generic day instead of [silent]
    # (the same sentence the start tool uses), so the coach says "those are defaults".
    assert out.startswith("ok: saved bench press 185×5") and "push session #" in out
    assert "STARTING DEFAULT exercises" in out and "[silent]" not in out.split("Don't")[0]
    assert sent[0].startswith("push day. starting u at 185 on bench press — first card, weights are off what u told me")
    assert "bench press · 185 × 5 × 4" in sent
    # a second start replaces the empty session rather than re-asking or dead-ending
    # (gym-deadlock fix, 2026-09-27): anchors are saved, so it just sends the card again.
    assert handle_start_workout_session(user.id, {"template_key": "push"}).startswith("ok: ")


def test_pending_card_expires_and_a_plain_anchor_sends_nothing(db, monkeypatch):
    from agent_tools import handle_start_workout_session, handle_set_lift_anchors
    from workouts.calibrate import pop_pending_card, set_pending_card, anchors_of, describe_anchors
    from models import User
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    user = make_user(db, **ANGEL)
    # a stated lift with NO ask pending → saved, nothing sent
    out = handle_set_lift_anchors(user.id, {"lifts": [{"exercise": "squat", "weight": 225}]})
    assert out.startswith("ok: saved squat 225×5") and sent == []
    # the pending marker never shows up as an anchor
    set_pending_card(user.id, "push")
    db.expire_all()
    u = db.get(User, user.id)
    assert set(anchors_of(u)) == {"squat"} and "pending" not in (describe_anchors(u) or "")
    assert pop_pending_card(user.id) == "push" and pop_pending_card(user.id) is None
    # stale ask → dropped
    import workouts.calibrate as cal
    set_pending_card(user.id, "push")
    monkeypatch.setattr(cal, "PENDING_CARD_TTL_S", -1)
    assert pop_pending_card(user.id) is None


def test_no_anchors_skips_the_ask_and_calibrates_from_stats(db, monkeypatch):
    from agent_tools import handle_start_workout_session
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    user = make_user(db, **ANGEL)
    out = handle_start_workout_session(user.id, {"template_key": "push", "no_anchors": True})
    assert out.startswith("ok: push session #")
    assert "bench press · 140 × 5 × 4" in sent


def test_never_trained_users_get_a_beginner_ask_then_the_light_card(db, monkeypatch):
    """A beginner is asked too (founder 2026-09-23) — for ANY number, even the empty bar —
    with 'no clue' as an easy out. 'no clue' → no_anchors → the calibrated card, whose
    intro says the numbers are a guess they can edit."""
    from agent_tools import handle_start_workout_session, handle_set_lift_anchors
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    user = make_user(db, **KRLA)
    out = handle_start_workout_session(user.id, {"template_key": "push"})
    assert out.startswith("error: first card") and "empty bar" in out and "no_anchors=true" in out and sent == []
    out = handle_start_workout_session(user.id, {"template_key": "push", "no_anchors": True})
    assert out.startswith("ok: push session #")
    assert sent[0].startswith("push day. starting u at 45 on bench press — first card, so the weights are my best guess")
    assert "tap a set and change the number" in sent[0]
    # …and a beginner who DOES have a number: "just the bar" → bench 45, card from it
    sent.clear()
    user2 = make_user(db, **dict(KRLA, phone="+15550009999"))
    assert handle_start_workout_session(user2.id, {"template_key": "push"}).startswith("error: first card")
    out = handle_set_lift_anchors(user2.id, {"lifts": [{"exercise": "bench", "weight": 45}]})
    assert "push session #" in out and sent[0].startswith("push day. starting u at 45 on bench press — first card, weights are off what u told me")


def test_second_card_uses_the_usual_intro(db, monkeypatch):
    from workouts.start import start_workout_session
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    user = make_user(db, **KRLA)
    r = start_workout_session(user.id, "push", no_anchors=True)
    assert r["first"] is True
    from models import get_session, SetLog, WorkoutSession
    s = get_session()
    try:
        x = s.query(SetLog).filter_by(session_id=r["session_id"], exercise="bench_press").first()
        x.actual_weight, x.actual_reps, x.done, x.source = 45, 5, True, "card"
        s.get(WorkoutSession, r["session_id"]).status = "done"
        s.commit()
    finally:
        s.close()
    r2 = start_workout_session(user.id, "push")
    assert r2["first"] is False and sent[-5].startswith("push day. bench press first, ") and "then the usual" not in sent[-5]


def test_pending_card_reply_is_answered_in_code(db, monkeypatch):
    """The answer to the ask never depends on the model: a number → anchors + the
    card; "no clue" → the light card; anything else → nothing, ask stays pending."""
    from agent_tools import handle_start_workout_session
    from workouts.calibrate import handle_pending_card_reply, peek_pending_card, anchors_of
    from models import User
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    # no ask pending → never fires
    u0 = make_user(db, **KRLA)
    assert handle_pending_card_reply(u0.id, "no clue") is False and sent == []
    # "no clue" → the light card, no anchors invented
    u1 = make_user(db, **dict(KRLA, phone="+15550001111"))
    assert handle_start_workout_session(u1.id, {"template_key": "push"}).startswith("error: first card")
    assert peek_pending_card(u1.id) == "push"
    assert handle_pending_card_reply(u1.id, "lol idk what should i eat before") is False   # not an answer → pending stays
    assert peek_pending_card(u1.id) == "push"
    assert handle_pending_card_reply(u1.id, "no clue, never really lifted") is True
    assert sent[0].startswith("push day. starting u at 45 on bench press — first card, so the weights are my best guess")
    assert peek_pending_card(u1.id) is None
    db.expire_all()
    assert anchors_of(db.get(User, u1.id)) == {}
    # a number → anchors + the card from it
    sent.clear()
    u2 = make_user(db, **dict(ANGEL, phone="+15550002222"))
    assert handle_start_workout_session(u2.id, {"template_key": "push"}).startswith("error: first card")
    assert handle_pending_card_reply(u2.id, "bench 185 squat 225 for 5") is True
    db.expire_all()
    a = anchors_of(db.get(User, u2.id))
    assert a["bench_press"]["weight"] == 185 and a["squat"]["weight"] == 225
    assert sent[0].startswith("push day. starting u at 185 on bench press — first card, weights are off what u told me")
    assert handle_pending_card_reply(u2.id, "no clue") is False                       # nothing pending anymore


def test_onboarding_reply_with_a_stated_lift_is_captured(db):
    from workouts.calibrate import maybe_capture_stated_anchors, anchors_of
    from models import User
    user = make_user(db, **dict(ANGEL, onboarding_step=2))
    r = maybe_capture_stated_anchors(user.id, "im 5'11 180, bench 185 squat 225 for 5, lift 4x a week after class", source="onboarding")
    assert r["saved"] == {"bench_press": "185×5", "squat": "225×5"}
    assert maybe_capture_stated_anchors(user.id, "i wanna bench 225 eventually", source="onboarding") is None
    db.expire_all()
    assert set(anchors_of(db.get(User, user.id))) == {"bench_press", "squat"}


def test_capabilities_registry_knows_the_tool():
    from capabilities import CAPABILITIES
    tools = {t for c in CAPABILITIES for t in c.tools}
    assert "set_lift_anchors" in tools


# ─── the first-card ask's ANSWER, as people actually phrase it (2026-10-05, user 48) ───

FOUNDER_ANSWER = ("My all time bench pr on flat bench was 185lb for 1. But I can do like 135 for 10 lwk\n"
                  "Squat, lwk I’m stuck between 135 and 150")


def test_loose_parse_reads_a_pr_a_can_do_and_a_range():
    """The live answer that read as NOTHING (so the turn fell to the model and the card
    lost its setup mode): a PR clause (1 rep), a bare 'can do' load that belongs to the
    lift just named, and a range that collapses to its midpoint."""
    from workouts.calibrate import parse_stated_anchors
    assert parse_stated_anchors(FOUNDER_ANSWER, loose=True) == [
        {"exercise": "bench", "weight": 185, "reps": 1},
        {"exercise": "bench", "weight": 135, "reps": 10},     # last wins in set_anchors → the working number
        {"exercise": "Squat", "weight": 145},
    ]
    # strict (onboarding capture of any message) stays precision-biased
    assert parse_stated_anchors(FOUNDER_ANSWER) == [{"exercise": "bench", "weight": 185, "reps": 1}]


@pytest.mark.parametrize("text, expect", [
    ("my bench max is 185", [{"exercise": "bench", "weight": 185, "reps": 1}]),     # a max is a 1-rep number
    ("squat 135-150", [{"exercise": "squat", "weight": 145}]),
    ("squat somewhere from 135 to 150", [{"exercise": "squat", "weight": 145}]),
    ("bench was 155 for 8", [{"exercise": "bench", "weight": 155, "reps": 8}]),
    ("squat rack is open 24 hours", []),                                            # not a load
    ("bench on tuesday at 6pm for 45 min", []),
    ("gym at 5, class from 12-1", []),
    ("wanna bench 225 by december", []),
])
def test_loose_parse_ranges_maxes_and_non_loads(text, expect):
    from workouts.calibrate import parse_stated_anchors
    assert parse_stated_anchors(text, loose=True) == expect


def test_the_founders_answer_is_answered_in_code_with_the_working_numbers(db, monkeypatch):
    from agent_tools import handle_start_workout_session
    from workouts.calibrate import handle_pending_card_reply, anchors_of
    from models import User
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    u = make_user(db, **ANGEL)
    assert handle_start_workout_session(u.id, {"template_key": "push"}).startswith("error: first card")
    assert handle_pending_card_reply(u.id, FOUNDER_ANSWER) is True
    db.expire_all()
    a = anchors_of(db.get(User, u.id))
    assert (a["bench_press"]["weight"], a["bench_press"]["reps"]) == (135, 10)
    assert a["squat"]["weight"] == 145
    assert sent[0].startswith("push day. starting u at 155 on bench press — first card, weights are off what u told me")
    # ANGEL has no saved push routine → the generic day is labelled as such, in code
    from workouts.start import defaults_note
    assert sent[-1] == defaults_note("push")


def test_a_saved_routine_gets_no_defaults_note_and_the_tool_stays_silent(db, monkeypatch):
    from agent_tools import handle_start_workout_session, handle_set_lift_anchors
    from workouts.calibrate import handle_pending_card_reply
    from workouts.start import ROUTINE_NOTE_TYPE
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append((body, kw.get("message_type"))) or {"sid": f"m{len(sent)}"})
    rows = [{"slug": "bench_press", "label": "bench press", "sets": 4, "reps": 5, "default_weight": 135, "plate_step": 5},
            {"slug": "cable_fly", "label": "cable fly", "sets": 3, "reps": 12, "default_weight": 25, "plate_step": 5}]
    u = make_user(db, **ANGEL, custom_templates={"push": rows})
    assert handle_start_workout_session(u.id, {"template_key": "push"}).startswith("error: first card")
    out = handle_set_lift_anchors(u.id, {"lifts": [{"exercise": "bench", "weight": 185, "reps": 5}]})
    assert out.endswith("Reply with exactly [silent].") and "STARTING DEFAULT" not in out
    assert all(t != ROUTINE_NOTE_TYPE for _b, t in sent)
    # and through the code path too
    v = make_user(db, **dict(ANGEL, phone="+15550007777"), custom_templates={"push": rows})
    sent.clear()
    assert handle_start_workout_session(v.id, {"template_key": "push"}).startswith("error: first card")
    assert handle_pending_card_reply(v.id, "bench 185 for 5") is True
    assert all(t != ROUTINE_NOTE_TYPE for _b, t in sent)
