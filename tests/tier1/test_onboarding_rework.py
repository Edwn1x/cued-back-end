"""
Onboarding rework v2 (founder, 2026-09-30 / 10-01): the conversation stays the
onboarding; the objective facts come from the form (signup_stats.py, PR #155); the
fixes from user 47 land here.

  1. The hook says what's happening ("ur in … gonna get to know u a bit over the next
     few texts, then ur first workout"); activation says "ur spot's open".
  2. Stats are parsed in code, metric or imperial, before the model sees the message;
     the extractor knows cm/kg and code converts.
  3. The model can never send a card and never claims one is coming / loading / sent.
  4. Nothing unknown → ONE code summary + completion in the same message. They ask for
     a workout with the card-critical basics in → the same, early (the one early exit).
     They ask without them → one honest ask for exactly those.
  5. The friend / big-ask / bundle intake is unchanged (see test_berkeley_friend_onboarding).
"""

from __future__ import annotations

import json
import re

import pytest

from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE, _is_extract, _new_signup


@pytest.fixture
def quiet_completion(monkeypatch):
    import config
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", False)
    monkeypatch.setattr(config, "WATER_OFFER_ENABLED", False)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)


def _bodies(sms_capture):
    return [b for _p, b in sms_capture]


def _row(db, uid):
    from models import User
    db.expire_all()
    return db.get(User, uid)


# ── 1. the hook ───────────────────────────────────────────────────────────────

def test_hook_says_what_happens_next(db, sms_capture, quiet_completion):
    import onboarding_agent as oa
    u = make_user(db, name="Celestia", onboarding_step=0, goal="muscle_building", experience="none", **INTAKE)
    assert oa.send_onboarding_hook(u.id, reason="signup") is True
    assert _bodies(sms_capture) == ["hey Celestia, it's cued. ur in. quick setup before i can actually help: gonna get to "
                                    "know u a bit over the next few texts, then run u through everything i can do. "
                                    "how's ur day going"]
    row = _row(db, u.id)
    assert row.onboarding_step == 1 and row.onboarding_hook_template == "hook_setup"
    assert oa.send_onboarding_hook(u.id, reason="signup") is False and len(sms_capture) == 1   # idempotent
    assert len(oa.HOOK_TEMPLATES) == 1


def test_activation_hook_says_the_spot_opened(db, sms_capture, quiet_completion):
    import onboarding_agent as oa
    u = make_user(db, name="Sarah", onboarding_step=0, goal="fat_loss", **INTAKE)
    oa.send_onboarding_hook(u.id, reason="waitlist_activate")
    assert _bodies(sms_capture)[0].startswith("hey Sarah, it's cued. ur spot's open. quick setup before i can actually help")


# ── 2. stats in code ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg, want", [
    ("I weight 50 kg and 168 cm for height", {"height_ft": 5, "height_in": 6, "weight_lbs": 110.0}),
    ("168cm and 50kg", {"height_ft": 5, "height_in": 6, "weight_lbs": 110.0}),
    ("1.68m 50kg", {"height_ft": 5, "height_in": 6, "weight_lbs": 110.0}),
    ("5'6 and 120", {"height_ft": 5, "height_in": 6, "weight_lbs": 120.0}),
    ("im 5 ft 10, 180 pounds", {"height_ft": 5, "height_in": 10, "weight_lbs": 180.0}),
    ("6'2\" 200 lbs, mon wed fri, no injuries", {"height_ft": 6, "height_in": 2, "weight_lbs": 200.0,
                                                 "workout_days": "mon,wed,fri", "injuries": "none"}),
    ("Morning on Monday, Tuesday and Thursday", {"workout_days": "mon,tue,thu"}),
    ("3 days a week", {"workout_days": "3"}),
    ("4x a week, nothing hurts", {"workout_days": "4", "injuries": "none"}),
    ("my back hurts sometimes", {}),                       # an injury is the model's to read
    ("I wanna work out for my back, what's the plan?", {}),
    ("168 and 50", {}),                                    # no units → the extractor, with the coach's message as context
    ("i can do like 5", {}),                               # a bare number is never a height
    ("have a 70 quiz at 4", {}),
])
def test_parse_stats(msg, want):
    from onboarding_agent import parse_stats
    assert parse_stats(msg) == want


def test_code_stats_are_stored_before_the_extractor_runs(db, anthropic_stub, sms_capture):
    """Her second message, replayed: code stores 5'6 / 110 and the extractor is asked
    only about what's still unknown; the friend reply follows as before."""
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2)
    prompts = []

    def _handler(kw):
        if _is_extract(kw):
            prompts.append(kw["messages"][0]["content"])
            return "{}"
        return "ok so pretty lean, eating's gonna matter as much as the lifting. u cooking or dining hall"
    anthropic_stub.reply_with(_handler)
    assert oa.handle_onboarding_reply(user, "I weight 50 kg and 168 cm for height") is False
    row = _row(db, user.id)
    assert (row.height_ft, row.height_in, row.weight_lbs) == (5, 6, 110.0)
    assert prompts and "height_weight" not in prompts[0].split("Fields we still need:")[1].split("\n")[0]
    assert len(sms_capture) == 1 and sms_capture[0][1].startswith("ok so pretty lean")


def test_extractor_metric_fields_are_converted_in_code(db, anthropic_stub):
    import onboarding_agent as oa
    u = _new_signup(db, onboarding_step=2)
    oa._store_extracted_data(u.id, {"height_cm": 168, "weight_kg": 50})
    row = _row(db, u.id)
    assert (row.height_ft, row.height_in, row.weight_lbs) == (5, 6, 110.0)
    prompts = []
    anthropic_stub.reply_with(lambda kw: (prompts.append(kw["messages"][0]["content"]) or "{}") if _is_extract(kw) else "ok")
    oa._extract_data_from_message("168 and 50", _new_signup(db, onboarding_step=2, phone="+15550000077"),
                                  last_coach_message="how tall are u and what u weigh rn?")
    assert prompts and '"height_cm"' in prompts[0] and "never convert to feet yourself" in prompts[0]


# ── 3 + 4. her actual messages, replayed ─────────────────────────────────────

def test_celestias_messages_now_end_in_a_workout(db, anthropic_stub, sms_capture, quiet_completion):
    """Live: 27 coach turns, no workout. Same three messages + 'send me the workout card' now:
    friend, friend, friend, then reaction + summary + done."""
    import onboarding_agent as oa
    user = make_user(db, name="Celestia", onboarding_step=1, age=23, gender="female", goal="muscle_building",
                     experience="none", equipment="full_gym", **INTAKE)
    gen = []

    def _handler(kw):
        content = str(kw["messages"][0]["content"])
        if _is_extract(kw):
            if "work out for my back" in content:
                return json.dumps({"goal": "muscle_building", "injuries": "back"})
            return "{}"
        gen.append(content)
        # genuinely distinct bodies — the outbound near-dedup drops anything ~85% similar
        return ["back's a good reason to start, we'll build around it", "ok so pretty lean, eating's gonna matter",
                "mornings work, rsf is dead at 8", "ur already there, love that"][len(gen) - 1]
    anthropic_stub.reply_with(_handler)

    # 1 — "what's the plan?" is a workout ask with the basics missing → the honest card ask
    assert oa.handle_onboarding_reply(user, "I wanna work out for my back, what's the plan?") is False
    assert "You can't send it yet" in gen[0] and "height and weight" in gen[0]
    # 2, 3 — the friend intake, unchanged; code reads the metric stats and the days
    assert oa.handle_onboarding_reply(_row(db, user.id), "I weight 50 kg and 168 cm for height") is False
    assert oa.handle_onboarding_reply(_row(db, user.id), "Morning on Monday, Tuesday and Thursday") is False
    row = _row(db, user.id)
    assert (row.height_ft, row.height_in, row.weight_lbs, row.workout_days, row.injuries) == (5, 6, 110.0, "mon,tue,thu", "back")
    assert _bodies(sms_capture) == ["back's a good reason to start, we'll build around it",
                                    "ok so pretty lean, eating's gonna matter", "mornings work, rsf is dead at 8"]
    assert all("ONE question" in g for g in gen[1:]), "the friend intake is unchanged"

    sms_capture.clear()
    assert oa.handle_onboarding_reply(_row(db, user.id), "Send me the workout card") is True
    b = _bodies(sms_capture)
    assert b[0] == "ur already there, love that" and "they asked for it" in gen[-1]
    assert b[1] == "ok so far this is what i have: ur 5'6 110, training mon/tue/thu, tryna build muscle", b[1]
    assert re.search(r"^im thinking \d{4} cal and \d{2,3}g protein a day\. ", b[2]) and oa.SUMMARY_CLOSER in b[2], b[2]
    row = _row(db, user.id)
    assert row.onboarding_step == 3 and row.coaching_branch == "training_nutrition" and row.calorie_target


def test_the_early_exit_needs_every_card_critical_field(db, anthropic_stub, sms_capture):
    """Height/weight in, days in, injuries UNKNOWN + 'send me a workout' → the card ask,
    not completion. A card for a back we don't know about is the one thing worse than
    a slow onboarding."""
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=110, workout_days="3")
    seen = {}
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (seen.setdefault("i", kw["messages"][0]["content"]) and "ask"))
    assert oa.handle_onboarding_reply(user, "can i get a workout") is False
    assert "You can't send it yet" in seen["i"] and "any injuries" in seen["i"] and "height and weight" not in seen["i"]
    assert _row(db, user.id).onboarding_step == 2


def test_bare_last_answer_gets_only_the_summary(db, anthropic_stub, sms_capture, quiet_completion):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=120, workout_days="3",
                       occupation="student", activity_level="active", avg_steps=7000, workout_time="18:00",
                       current_split="none", cooking_situation="cook_myself", diet="omnivore",
                       wake_time="09:30", sleep_time="00:00", existing_tools="none")
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (_ for _ in ()).throw(AssertionError("no bubble")))
    assert oa.handle_onboarding_reply(user, "no injuries") is True
    b = _bodies(sms_capture)
    assert len(b) == 2 and b[0] == ("ok so far this is what i have: ur 5'6 120, training 3 days a week in the evenings, "
                                    "up at 9:30 down by 12, tryna build muscle"), b


def test_completion_reaction_failure_never_blocks_the_summary(db, anthropic_stub, sms_capture, quiet_completion):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=120, workout_days="3",
                       occupation="student", activity_level="active", avg_steps=7000, workout_time="18:00",
                       current_split="none", cooking_situation="cook_myself", diet="omnivore",
                       wake_time="09:30", sleep_time="00:00", existing_tools="none")
    anthropic_stub.reply_with(lambda kw: '{"injuries": "none"}' if _is_extract(kw) else (_ for _ in ()).throw(RuntimeError("api down")))
    assert oa.handle_onboarding_reply(user, "nah nothing hurts, why do u ask?") is True
    assert len(sms_capture) == 2 and sms_capture[0][1].startswith("ok so far")


def test_completion_logs_turns_minutes_and_what_is_learned_later(db, anthropic_stub, sms_capture, quiet_completion, caplog):
    import logging
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=120, workout_days="3", injuries="none")
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "x")
    with caplog.at_level(logging.INFO):
        assert oa.handle_onboarding_reply(user, "just give me the plan") is True
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Onboarding complete for"))
    assert "turns=" in line and "minutes=" in line and "learned_later=['occupation'" in line
    assert any("ONBOARDING_EARLY_EXIT" in r.getMessage() for r in caplog.records)


# ── 6. nobody has to say "card" ──────────────────────────────────────────────

def test_start_workout_tool_answers_plain_asks():
    from agent_tools import START_WORKOUT_SESSION_TOOL
    d = START_WORKOUT_SESSION_TOOL["description"]
    for phrase in ("they never have to say 'card'", "'give me a workout'", "'what should i do today'", "'leg day'",
                   "'can i get a workout for my back'", "'send me my card'"):
        assert phrase in d, phrase


def test_the_friend_intake_is_still_here_and_the_adjust_turn_is_gone():
    import onboarding_agent as oa
    for name in ("_intake_mode", "_build_friend_reply", "_build_big_ask_message", "_bundle_gap_questions",
                 "_big_ask_sent", "_coach_turns", "_NOT_DONE_LINE", "BIG_ASK_AFTER_TURNS"):
        assert hasattr(oa, name), name
    assert not hasattr(oa, "_extract_target_request")
    assert oa.CARD_CRITICAL == {"height_weight", "workout_days", "injuries", "split_days"}


@pytest.fixture(autouse=True)
def _legacy_completion_shape(monkeypatch):
    """These tests pin the 2026-10-09 shape (two-bubble summary + tiered rundown), still
    supported behind the flags; the 2026-10-10 one-bubble shape is tests/tier1/test_short_completion.py."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "ONBOARDING_SUMMARY_ONE_BUBBLE", False)
    monkeypatch.setattr(_cfg, "ONBOARDING_RUNDOWN_STYLE", "tiered")
