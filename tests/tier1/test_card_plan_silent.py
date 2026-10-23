"""
A card went out with the coach's own plan text → the model adds nothing; and a weight the
card doesn't show never reaches the thread.

Live 2026-10-22 14:59 PT (user 40, a beginner on defaults): pre_session_plan sent "leg press
up to 175 today, you cleared 165 easy" + the card (leg press 175×10). The start tool's
beginner line then told the model to "say WHY … (the weights)", and it added "leg press
start at 165 today, u hit all reps last time so it goes up". Two numbers in the thread, the
model's one wrong. Pins: (1) the tool result says [silent] when plan text went out, even on
the defaults/beginner path, and carries the card's numbers; (2) the beginner line without
plan text carries the card's numbers; (3) the loop guard drops a sentence whose load figure
is not on the card, keeps the rest, and is inert without a start call / flag off / numbers
that match; (4) start.card_numbers formats from card state; (5) wiring.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

from tests.factories import make_user


def _result(**over):
    base = {"session_id": 44, "template_key": "full_body", "surface": "card", "sets": 15,
            "first": False, "estimated": False, "setup": False, "used_default": True,
            "one_off": False, "has_routine": False, "novice": True, "plan_text": True,
            "card": ["leg press 175×10", "chest press 65×10", "seated cable row 70×10",
                     "shoulder press machine 60×10", "leg extension 45×12"]}
    base.update(over)
    return base


def _patch_start(monkeypatch, result):
    import workouts.start as start_mod
    monkeypatch.setattr(start_mod, "start_workout_session", lambda *a, **k: result)


# ── 1/2. the tool result ─────────────────────────────────────────────────────────

def test_plan_text_makes_the_tool_result_silent_even_for_a_beginner_on_defaults(db, monkeypatch):
    import config
    from agent_tools import handle_start_workout_session
    monkeypatch.setattr(config, "CARD_PLAN_TEXT_SILENT_ENABLED", True, raising=False)
    u = make_user(db)
    _patch_start(monkeypatch, _result())
    r = handle_start_workout_session(u.id, {"template_key": "full_body"})
    assert "Reply with exactly [silent]" in r and "Add NOTHING about exercises or weights" in r
    assert "leg press 175×10" in r
    assert "BEGINNER" not in r and "WHY it's built this way" not in r


def test_without_plan_text_the_beginner_line_carries_the_cards_numbers(db, monkeypatch):
    from agent_tools import handle_start_workout_session
    u = make_user(db)
    _patch_start(monkeypatch, _result(plan_text=False))
    r = handle_start_workout_session(u.id, {"template_key": "full_body"})
    assert "BEGINNER" in r and "Don't reply [silent]" in r
    assert "The card shows: leg press 175×10" in r and "never another number" in r


def test_flag_off_restores_the_old_contract(db, monkeypatch):
    import config
    from agent_tools import handle_start_workout_session
    monkeypatch.setattr(config, "CARD_PLAN_TEXT_SILENT_ENABLED", False, raising=False)
    u = make_user(db)
    _patch_start(monkeypatch, _result())
    r = handle_start_workout_session(u.id, {"template_key": "full_body"})
    assert "BEGINNER" in r and "Add NOTHING" not in r


def test_a_routine_user_with_plan_text_is_also_silent(db, monkeypatch):
    from agent_tools import handle_start_workout_session
    u = make_user(db)
    _patch_start(monkeypatch, _result(used_default=False, novice=False))
    r = handle_start_workout_session(u.id, {"template_key": "push"})
    assert "Reply with exactly [silent]" in r and "Add NOTHING" in r


# ── 3. the loop guard ────────────────────────────────────────────────────────────

def _open_card(db, user_id):
    from models import WorkoutSession, SetLog
    ws = WorkoutSession(user_id=user_id, template_key="full_body", status="active",
                        started_at=datetime.now(timezone.utc).replace(tzinfo=None))
    db.add(ws)
    db.commit()
    plan = [("leg_press", 175.0, 10), ("chest_press_machine", 65.0, 10), ("seated_cable_row", 70.0, 10),
            ("shoulder_press_machine", 60.0, 10), ("leg_extension", 45.0, 12)]
    for slug, w, r in plan:
        for i in range(3):
            db.add(SetLog(session_id=ws.id, exercise=slug, exercise_label=slug.replace("_", " "),
                          set_index=i, planned_weight=w, planned_reps=r))
    db.commit()
    return ws


def _mark_start_called(user_id):
    import agent_tools
    agent_tools._TURN_STATE[user_id] = {"reacted": False, "reply_to": None, "called": {"start_workout_session"}}


def test_guard_drops_the_sentence_with_a_weight_the_card_does_not_show(db, monkeypatch):
    import config
    from agent_loop import _card_number_guard
    monkeypatch.setattr(config, "CARD_NUMBER_CONFLICT_GUARD_ENABLED", True, raising=False)
    u = make_user(db)
    _open_card(db, u.id)
    _mark_start_called(u.id)
    live = "full body again, all machines like usual\nleg press start at 165 today, u hit all reps last time so it goes up"
    assert _card_number_guard(u, live) == "full body again, all machines like usual"
    # the whole reply is numbers the card doesn't carry → nothing survives, the card speaks
    assert _card_number_guard(u, "leg press at 165. chest press machine at 55 today") == ""
    # a weight in pounds with no exercise named is still a load claim; equipment limits and
    # times are not ("db up to 50", "30 min")
    assert _card_number_guard(u, "go 185 lbs on the first one") == ""
    free = "cable machine + db up to 50, that's a full day. 30 min tops"
    assert _card_number_guard(u, free) == free
    # a figure the card DOES show passes untouched; small counts are below the floor
    ok = "leg press 175 today, 3 sets of 10 — chest press 65"
    assert _card_number_guard(u, ok) == ok


def test_guard_is_inert_without_a_start_call_or_with_the_flag_off(db, monkeypatch):
    import config
    import agent_tools
    from agent_loop import _card_number_guard
    u = make_user(db)
    _open_card(db, u.id)
    agent_tools._TURN_STATE.pop(u.id, None)
    txt = "leg press at 165 today"
    assert _card_number_guard(u, txt) == txt, "no start_workout_session this turn → untouched"
    _mark_start_called(u.id)
    monkeypatch.setattr(config, "CARD_NUMBER_CONFLICT_GUARD_ENABLED", False, raising=False)
    assert _card_number_guard(u, txt) == txt
    monkeypatch.setattr(config, "CARD_NUMBER_CONFLICT_GUARD_ENABLED", True, raising=False)
    assert _card_number_guard(u, "") == ""


# ── 4. card_numbers ──────────────────────────────────────────────────────────────

def test_card_numbers_formats_the_card_state():
    from workouts.start import card_numbers
    state = {"exercises": [
        {"slug": "leg_press", "label": "Leg Press", "sets": [{"planned_weight": 175.0, "planned_reps": 10}]},
        {"slug": "plank", "label": "Plank", "sets": [{"planned_weight": 0, "planned_reps": 40}]},
        {"slug": "empty", "label": "x", "sets": []},
    ]}
    assert card_numbers(state) == ["leg press 175×10", "plank ×40"]


# ── 5. wiring + flags ────────────────────────────────────────────────────────────

def test_guard_is_wired_before_the_reply_leaves_the_loop():
    from agent_loop import run_agent_loop
    src = inspect.getsource(run_agent_loop)
    assert "_card_number_guard(user, text)" in src
    assert src.index("_card_number_guard(user, text)") < src.index("_apply_restatement_guard(user, _dead_end_fallback(text, user.id)")


def test_start_result_carries_plan_text_and_card_keys():
    from workouts.start import start_workout_session
    src = inspect.getsource(start_workout_session)
    assert '"plan_text": bool(intro)' in src and '"card": card_numbers(state)' in src


def test_flags_default_on():
    import config
    assert config.CARD_PLAN_TEXT_SILENT_ENABLED is True and config.CARD_NUMBER_CONFLICT_GUARD_ENABLED is True
