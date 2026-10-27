"""
Referral codes (referrals.py, founder 2026-10-27 — the frat sign-up challenge).

- Every account has a code (name + two digits), made at signup / on first need, unique.
- The coach hands it out ONLY via get_referral_code (tool on when REFERRALS_ENABLED).
- The last onboarding question is code's ("did a friend send u here?"): the final-answer
  reaction carries it, the next inbound settles it — a matching code credits the friend,
  anything else is no credit — then the summary + completion. Asked once.
- /admin/referrals is the leaderboard.
"""
from __future__ import annotations

import json
import random

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _row, _coach_said, _flags, reactions  # noqa: F401 — fixtures


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "REFERRALS_ENABLED", True)
    monkeypatch.setattr(config, "REFERRAL_ASK_ENABLED", True)
    monkeypatch.setattr(config, "PROGRAM_BRIEF_ENABLED", False)


def _complete_profile(db, **over):
    kw = dict(onboarding_step=2, height_ft=5, height_in=6, weight_lbs=142, occupation="student",
              activity_level="lightly active", avg_steps=10000, workout_days="3", workout_time="18:00",
              current_split="ppl", cooking_situation="mix", diet="omnivore", injuries="none",
              wake_time="09:00", sleep_time="01:00")
    kw.update(over)
    return _new_signup(db, **kw)


def _no_extract(reply="yeah fitbit's solid"):
    return lambda kw: json.dumps({"existing_tools": "fitbit"}) if _is_extract(kw) else reply


# ── codes ───────────────────────────────────────────────────────────────────

def test_code_is_name_plus_two_digits_and_unique(db):
    import referrals
    a = make_user(db, name="Nau")
    b = make_user(db, name="Nau")
    ca = referrals.ensure_code(a.id, rng=random.Random(1))
    cb = referrals.ensure_code(b.id, rng=random.Random(1))      # same seed → same first pick → must dodge
    assert ca.startswith("NAU") and ca[3:].isdigit() and len(ca) == 5
    assert cb.startswith("NAU") and cb != ca
    assert referrals.ensure_code(a.id) == ca, "stable once set"
    assert referrals.lookup(ca.lower()) == (a.id, "Nau")


def test_code_prefix_falls_back_for_short_or_non_ascii_names(db):
    import referrals
    assert referrals._prefix_from_name("José María") == "JOSE"
    assert referrals._prefix_from_name("A") == "CUED"
    assert referrals._prefix_from_name("") == "CUED"
    assert referrals._prefix_from_name("Christopher Jr") == "CHRISTOP"


def test_signup_creates_the_code(db, client, monkeypatch):
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    import app as appmod
    monkeypatch.setattr(appmod, "start_onboarding", lambda user, reason="x": None)
    body = {"name": "Nate", "phone": "+15105550199", "source": "hero", "timezone": "America/Los_Angeles",
            "age": 21, "gender": "male", "goal": ["fat_loss"], "experience": "intermediate",
            "equipment": "full_gym", "sms_consent": True}
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    assert r.status_code in (200, 201), r.get_json()
    from models import User
    u = db.query(User).filter(User.phone == "+15105550199").one()
    assert u.referral_code and u.referral_code.startswith("NATE")


def test_find_code_token_shapes():
    import referrals
    assert referrals.find_code_token("nau42") == "NAU42"
    assert referrals.find_code_token("Nau-42!") == "NAU42"
    assert referrals.find_code_token("no") is None
    assert referrals.find_code_token("its NAU 42 i think") is None, "spaced → only a candidate"
    assert referrals.candidate_tokens("its NAU 42 i think") == ["NAU42"]
    assert referrals.find_code_token("i bench 135 for 5") is None
    assert referrals.candidate_tokens("i bench 135 for 5") == ["BENCH135"]
    assert referrals.candidate_tokens("nah my friend nau told me") == []


def test_spaced_code_credits_when_real_but_a_spaced_near_miss_is_just_a_no(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa, referrals
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    friend = make_user(db, name="Nau")
    code = referrals.ensure_code(friend.id)
    user = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    sms_capture.clear()
    assert oa.handle_onboarding_reply(user, f"ya its {code[:3].lower()} {code[3:]}") is True
    assert _bodies(sms_capture)[0] == "bet, Nau gets the credit"
    assert _row(db, user.id).referred_by_user_id == friend.id

    other = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    sms_capture.clear()
    assert oa.handle_onboarding_reply(other, "nah but i bench 135 for 5") is True
    assert _bodies(sms_capture)[0].startswith("ok so"), "no 'unknown code' ack for a near-miss"
    assert _row(db, other.id).referred_by_user_id is None


# ── the last onboarding question ─────────────────────────────────────────────

def test_final_answer_gets_the_ask_in_the_reaction_bubble_not_the_summary(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa, referrals
    user = _complete_profile(db)                         # only existing_tools missing
    _coach_said(user, "any apps or a watch u use")
    sms_capture.clear()
    anthropic_stub.reply_with(_no_extract())
    assert oa.handle_onboarding_reply(user, "i use a fitbit") is False
    bodies = _bodies(sms_capture)
    assert len(bodies) == 1 and bodies[0] == f"yeah fitbit's solid {referrals.ASK_TEXT}"
    row = _row(db, user.id)
    assert row.referral_asked_at and not row.referral_resolved_at and row.onboarding_step == 2
    assert "Code is about to ask them one last quick question" in anthropic_stub.calls[-1]["messages"][0]["content"]


def test_a_friends_code_credits_them_then_the_summary_completes(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa, referrals
    friend = make_user(db, name="Jayden")
    code = referrals.ensure_code(friend.id)
    user = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    _coach_said(user, referrals.ASK_TEXT)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    assert oa.handle_onboarding_reply(user, code.lower()) is True
    bodies = _bodies(sms_capture)
    assert bodies[0] == "bet, Jayden gets the credit"
    assert bodies[1].startswith("ok so"), "the summary follows the ack"
    row = _row(db, user.id)
    assert row.referred_by_user_id == friend.id and row.referral_resolved_at and row.onboarding_step == 3
    assert referrals.stats(friend.id)["signups"] == 1 and referrals.stats(friend.id)["completed"] == 1


def test_no_means_no_credit_and_straight_to_the_summary(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa, referrals
    user = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    _coach_said(user, referrals.ASK_TEXT)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    assert oa.handle_onboarding_reply(user, "no") is True
    bodies = _bodies(sms_capture)
    assert bodies[0].startswith("ok so"), bodies
    row = _row(db, user.id)
    assert row.referred_by_user_id is None and row.referral_resolved_at and row.onboarding_step == 3


def test_unknown_and_own_codes_are_not_credited(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa, referrals
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    user = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    sms_capture.clear()
    assert oa.handle_onboarding_reply(user, "ZZZ99") is True
    assert _bodies(sms_capture)[0] == referrals.UNKNOWN_CODE_TEXT
    assert _row(db, user.id).referred_by_user_id is None

    me = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    own = referrals.ensure_code(me.id)
    sms_capture.clear()
    assert oa.handle_onboarding_reply(me, own) is True
    assert _bodies(sms_capture)[0] == referrals.OWN_CODE_TEXT
    assert _row(db, me.id).referred_by_user_id is None


def test_a_question_about_the_code_is_answered_once_and_the_ask_stays_open(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa, referrals
    user = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    _coach_said(user, referrals.ASK_TEXT)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    assert oa.handle_onboarding_reply(user, "wait what's a referral code?") is False
    assert _bodies(sms_capture) == [referrals.EXPLAIN_TEXT]
    assert _row(db, user.id).referral_resolved_at is None
    sms_capture.clear()
    assert oa.handle_onboarding_reply(user, "oh ok no then?") is True, "a second question doesn't loop"
    assert _bodies(sms_capture)[0].startswith("ok so")


def test_continuation_without_a_code_keeps_the_ask_open(db, anthropic_stub, sms_capture, reactions):
    import onboarding_agent as oa, referrals
    user = _complete_profile(db, existing_tools="none", referral_asked_at=referrals._naive_utcnow())
    _coach_said(user, referrals.ASK_TEXT)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    assert oa.handle_onboarding_reply(user, "and i mostly lift at night", continuation=True) is False
    assert _bodies(sms_capture) == [] and reactions == [(user.id, "like")]
    row = _row(db, user.id)
    assert row.referral_resolved_at is None and row.onboarding_step == 2


def test_early_exit_asks_too_then_completes_early(db, anthropic_stub, sms_capture, caplog):
    import logging
    import onboarding_agent as oa, referrals
    caplog.set_level(logging.INFO)
    # card-critical basics in, diet/cooking/sleep still unknown, and they ask for the workout
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=10, weight_lbs=170, workout_days="4",
                       injuries="none", current_split="ppl")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "say less, card's coming")
    assert oa.handle_onboarding_reply(user, "can i just get my workout for today") is False
    assert _bodies(sms_capture)[-1].endswith(referrals.ASK_TEXT)
    assert "mode=referral_ask" in caplog.text and "early=True" in caplog.text
    sms_capture.clear()
    assert oa.handle_onboarding_reply(user, "no") is True
    assert "ONBOARDING_EARLY_EXIT" in caplog.text
    assert _row(db, user.id).onboarding_step == 3


def test_ask_flag_off_completes_directly(db, anthropic_stub, sms_capture, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "REFERRAL_ASK_ENABLED", False)
    user = _complete_profile(db)
    _coach_said(user, "any apps or a watch u use")
    sms_capture.clear()
    anthropic_stub.reply_with(_no_extract())
    assert oa.handle_onboarding_reply(user, "i use a fitbit") is True
    bodies = _bodies(sms_capture)
    assert bodies[0] == "yeah fitbit's solid" and bodies[1].startswith("ok so")
    assert _row(db, user.id).referral_asked_at is None


def test_onboarding_prompt_carries_their_code_only_for_the_ask(db):
    import onboarding_agent as oa, referrals
    user = _complete_profile(db)
    prompt = oa._build_system_prompt(_row(db, user.id))
    code = referrals.ensure_code(user.id)
    assert f"Their referral code: {code}" in prompt and "ONLY if they ask" in prompt


# ── the coach tool ───────────────────────────────────────────────────────────

def test_tool_returns_code_and_credited_count(db):
    import referrals
    from agent_tools import handle_get_referral_code
    me = make_user(db, name="Nau")
    make_user(db, name="Jayden", referred_by_user_id=me.id, onboarding_step=3)
    make_user(db, name="Alex", referred_by_user_id=me.id, onboarding_step=2)
    make_user(db, name="Gone", referred_by_user_id=me.id, onboarding_step=3, archived_at=referrals._naive_utcnow())
    out = handle_get_referral_code(me.id, {})
    code = referrals.ensure_code(me.id)
    assert f"their referral code is {code}" in out
    assert "credited sign-ups so far: 2 (1 finished setup): Jayden, Alex" in out


def test_tool_offered_to_the_coach_when_on_and_not_when_off(db, monkeypatch, anthropic_stub):
    from tests.tier1.test_calendar_writeback import _tool_names_offered
    user = make_user(db, onboarding_step=3)
    assert "get_referral_code" in _tool_names_offered(user, monkeypatch, anthropic_stub)
    monkeypatch.setattr(config, "REFERRALS_ENABLED", False)
    assert "get_referral_code" not in _tool_names_offered(user, monkeypatch, anthropic_stub)


def test_voice_prompt_names_the_tool():
    from agent_loop import _voice_prompt
    v = _voice_prompt()
    assert "get_referral_code" in v and "never bring the code up unasked" in v


# ── admin ────────────────────────────────────────────────────────────────────

def test_admin_referrals_page_ranks_referrers_and_backfills_codes(db, client):
    import referrals
    top = make_user(db, name="Nau")
    second = make_user(db, name="Sam")
    for n in ("A", "B", "C"):
        make_user(db, name=f"Kid{n}", referred_by_user_id=top.id, onboarding_step=3)
    make_user(db, name="KidD", referred_by_user_id=second.id, onboarding_step=2)
    lone = make_user(db, name="Lone")
    r = client.get("/admin/referrals")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert html.index(">Nau<") < html.index(">Sam<"), "most sign-ups first"
    assert "KidD</a> (in setup)" in html
    assert _row(db, lone.id).referral_code, "the page backfills codes for everyone"
    board = referrals.leaderboard(db)
    assert [(b["name"], b["signups"], b["completed"]) for b in board] == [("Nau", 3, 3), ("Sam", 1, 0)]
