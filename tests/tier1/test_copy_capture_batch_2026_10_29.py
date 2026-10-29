"""
Four copy / capture fixes from the 2026-10-28 thread watch.

1. Sign-up name field got the referral question. User 85 typed "Where do I put the referral
   code" as the name → users.name "Where", own code WHERE9060, the coach opened "hey Where".
   A question-shaped name is refused with a message that says where the code goes.

2. Program brief lists a day type twice. User 86's five-day push/pull/legs/push/pull cycle
   rendered "push = …, pull = …, legs = …, push = …, pull = …". Each distinct day once.

3. The gcal setup step says "around ur classes and catch due dates" to a desk worker (user
   80, 26, not a student). A known non-student gets "around ur week and catch what's coming".

4. Photo readback said "1 cup of chimichurri"; the row said 'chimichurri ~1 cup used (~2
   tbsp)' and carried 150 cal (user 51, 02:31). One amount per item — the amount EATEN —
   and the tool result flags a description that names two so the readback picks one.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_live_signup import live_mode, inline_hook, _signup, _user  # noqa: F401


def test_knob_defaults():
    assert config.SIGNUP_NAME_SANITY_ENABLED is True
    assert config.GCAL_OFFER_NONSTUDENT_COPY_ENABLED is True
    assert config.LOG_MEAL_TWO_AMOUNTS_HINT_ENABLED is True


# ─── 1. the sign-up name ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "Where do I put the referral code",
    "where does the referral code go?",
    "What is this",
    "how do i sign up",
    "Nate?",
    "referral code NAU4821",
])
def test_a_question_shaped_name_is_refused_with_the_code_hint(db, client, inline_hook, sms_capture, name):
    code, data = _signup(client, "5105550901", name=name, full_name=name)
    assert code == 400, (name, data)
    assert "first name" in data["message"] and "referral code" in data["message"], data
    assert sms_capture == []


@pytest.mark.parametrize("name", ["Nate", "Mary Jane", "Jean-Luc", "Where", "Dakota Is", "Hope"])
def test_real_names_still_pass(db, client, inline_hook, sms_capture, name):
    code, data = _signup(client, f"51055509{hash(name) % 90 + 10:02d}", name=name, full_name=name)
    assert code == 200 and data["status"] == "ok", (name, data)


def test_name_sanity_flag_off_is_the_old_accept(db, client, inline_hook, sms_capture, monkeypatch):
    monkeypatch.setattr(config, "SIGNUP_NAME_SANITY_ENABLED", False)
    code, data = _signup(client, "5105550902", name="Where do I put the referral code")
    assert code == 200 and _user(db, "+15105550902").name == "Where"


# ─── 2. the program brief ─────────────────────────────────────────────────────

def test_a_cycle_with_repeats_lists_each_day_once():
    from workouts.program import build_program, program_brief
    from tests.tier1.test_program_builder import _u
    u = _u(workout_days="5", experience="intermediate", current_split="ppl",
           split_days=["push", "pull", "legs", "push", "pull"], equipment="full_gym", injuries=None,
           user_profile_memory=None, goal="muscle_building")
    p = build_program(u)
    assert p["cycle"] == ["push", "pull", "legs", "push", "pull"]      # their own five-day order stands
    b = program_brief(u, p)
    assert b[1].count("push =") == 1 and b[1].count("pull =") == 1 and b[1].count("legs =") == 1, b[1]
    assert b[1].index("push =") < b[1].index("pull =") < b[1].index("legs =")


# ─── 3. the gcal step for a non-student ───────────────────────────────────────

def test_a_known_non_student_is_not_promised_classes():
    from connect_offers import _directive_text, STEP_GCAL_LINK, STEP_GCAL_ASK
    desk = NS(occupation="desk job", year=None)
    out = _directive_text("offer_link", "gcal", None, "first thing:", user=desk)
    assert out.startswith("first thing: connect ur google calendar") and "classes" not in out and "due dates" not in out, out
    assert "ur week" in out and "tap this" in out
    ask = _directive_text("offer_ask", "gcal", None, "next:", user=desk)
    assert "classes" not in ask and "which google account" in ask, ask


def test_a_student_or_an_unknown_occupation_keeps_the_classes_copy():
    from connect_offers import _directive_text, STEP_GCAL_LINK
    for u in (NS(occupation="physics student", year=None), NS(occupation=None, year=None), None):
        out = _directive_text("offer_link", "gcal", None, "first thing:", user=u)
        assert out == f"first thing: {STEP_GCAL_LINK}", (u, out)


def test_nonstudent_copy_flag_off(monkeypatch):
    from connect_offers import _directive_text, STEP_GCAL_LINK
    monkeypatch.setattr(config, "GCAL_OFFER_NONSTUDENT_COPY_ENABLED", False)
    out = _directive_text("offer_link", "gcal", None, "first thing:", user=NS(occupation="desk job", year=None))
    assert out == f"first thing: {STEP_GCAL_LINK}"


def test_the_live_send_path_passes_the_user(db, monkeypatch, sms_capture):
    """send_next_offer → _directive_text gets the user, so a desk worker's first step has no 'classes'."""
    import connect_offers
    from datetime import datetime, timezone
    seen = {}
    real = connect_offers._directive_text
    def spy(kind, provider, text, lead, user=None):
        seen["user"] = user
        return real(kind, provider, text, lead, user=user)
    monkeypatch.setattr(connect_offers, "_directive_text", spy)
    monkeypatch.setattr(connect_offers, "_action_for", lambda session, u, now, min_gap=None: ("offer_link", "gcal", None))
    monkeypatch.setattr(connect_offers, "_eligible", lambda u: True)
    monkeypatch.setattr(connect_offers, "_send_action", lambda *a, **k: None)
    u = make_user(db, occupation="desk job")
    connect_offers.offer_now(u.id, lead="first thing:")
    assert seen.get("user") is not None and seen["user"].id == u.id


# ─── 4. one amount per item ───────────────────────────────────────────────────

def _log(uid, desc, cal=150, guessed=True):
    from agent_tools import handle_log_meal
    return handle_log_meal(uid, {"description": desc, "calories": cal, "protein_g": 2, "carbs_g": 3, "fat_g": 14,
                                 "portion_guessed": guessed})


def test_a_description_with_two_amounts_is_flagged_in_the_result(db, caplog):
    import logging
    user = make_user(db)
    with caplog.at_level(logging.INFO):
        out = _log(user.id, "chimichurri ~1 cup used (~2 tbsp)")
    assert out.startswith("ok"), out
    assert "two amounts" in out and "chimichurri ~1 cup used (~2 tbsp)" in out and "the amount EATEN" in out, out
    assert "LOG_MEAL_TWO_AMOUNTS" in caplog.text


@pytest.mark.parametrize("desc", [
    "chicken breast ~6oz",
    "white rice ~2 cups",
    "2 eggs, 2 toasts w/ butter",            # two foods, one amount each — not two amounts for one thing
    "greek yogurt 3/4 cup",
    "salad w/ ranch (side)",
])
def test_one_amount_descriptions_are_not_flagged(db, desc):
    user = make_user(db)
    out = _log(user.id, desc)
    assert out.startswith("ok") and "two amounts" not in out, out


@pytest.mark.parametrize("desc", [
    "ranch served 1/4 cup, ate about 1 tbsp",
    "peanut butter ~2 tbsp (jar says 1 serving = 2 tbsp, used maybe 3 tbsp)",
])
def test_served_vs_eaten_phrasings_are_flagged(db, desc):
    user = make_user(db)
    assert "two amounts" in _log(user.id, desc)


def test_two_amounts_hint_flag_off(db, monkeypatch):
    monkeypatch.setattr(config, "LOG_MEAL_TWO_AMOUNTS_HINT_ENABLED", False)
    user = make_user(db)
    assert "two amounts" not in _log(user.id, "chimichurri ~1 cup used (~2 tbsp)")


def test_the_estimation_prompt_says_one_amount_the_amount_eaten():
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    est = " ".join(open(os.path.join(root, "prompts", "meal_estimation.md")).read().lower().split())
    assert "one amount per item" in est and "the amount eaten" in est and "chimichurri" in est
