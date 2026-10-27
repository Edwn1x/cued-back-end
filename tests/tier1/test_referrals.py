"""
Referral codes (referrals.py, founder 2026-10-27 — the frat sign-up challenge).

- Every account has a code (name + two digits), made at signup / on first need, unique.
- The coach hands it out ONLY via get_referral_code (tool on when REFERRALS_ENABLED).
- The site's sign-up chat asks for a friend's code as its last optional field and posts
  it as `referral_code`; /waitlist credits the referrer when it matches, keeps what was
  typed either way. Nothing is asked in the text thread.
- /admin/referrals is the leaderboard.
"""
from __future__ import annotations

import json
import random

import pytest

import config
from tests.factories import make_user


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "REFERRALS_ENABLED", True)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    import app as appmod
    monkeypatch.setattr(appmod, "start_onboarding", lambda user, reason="x": None)


def _signup(client, phone, **over):
    body = {"name": "Nate", "phone": phone, "source": "hero", "timezone": "America/Los_Angeles",
            "age": 21, "gender": "male", "goal": ["fat_loss"], "experience": "intermediate",
            "equipment": "full_gym", "sms_consent": True}
    body.update(over)
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


def _by_phone(db, phone):
    from models import User
    db.expire_all()
    return db.query(User).filter(User.phone == phone).one()


# ── codes ───────────────────────────────────────────────────────────────────

def test_code_is_name_plus_four_digits_and_unique(db):
    import referrals
    a = make_user(db, name="Nau")
    b = make_user(db, name="Nau")
    ca = referrals.ensure_code(a.id, rng=random.Random(1))
    cb = referrals.ensure_code(b.id, rng=random.Random(1))      # same seed → same first pick → must dodge
    assert ca.startswith("NAU") and ca[3:].isdigit() and len(ca) == 7
    assert cb.startswith("NAU") and cb != ca
    assert referrals.ensure_code(a.id) == ca, "stable once set"
    assert referrals.lookup(ca.lower()) == (a.id, "Nau")


def test_code_prefix_falls_back_for_short_or_non_ascii_names(db):
    import referrals
    assert referrals._prefix_from_name("José María") == "JOSE"
    assert referrals._prefix_from_name("A") == "CUED"
    assert referrals._prefix_from_name("") == "CUED"
    assert referrals._prefix_from_name("Christopher Jr") == "CHRISTOP"


def test_normalize_forgives_case_spaces_and_dashes():
    import referrals
    assert referrals.normalize("nau4821") == "NAU4821"
    assert referrals.normalize(" Nau 4821 ") == "NAU4821"
    assert referrals.normalize("NAU-4821") == "NAU4821"
    assert referrals.normalize("nau_4821.") == "NAU4821"
    assert referrals.normalize("nau42") is None, "two digits is the retired shape"
    assert referrals.normalize("no") is None
    assert referrals.normalize("hey there") is None
    assert referrals.normalize("1234") is None
    assert referrals.normalize("") is None and referrals.normalize(None) is None


# ── the sign-up ──────────────────────────────────────────────────────────────

def test_signup_creates_a_code_and_credits_the_friends_code(db, client):
    import referrals
    friend = make_user(db, name="Jayden")
    code = referrals.ensure_code(friend.id)
    status, data = _signup(client, "+15105550199", referral_code=f" {code[:3].lower()}-{code[3:]} ")
    assert status == 200, data
    assert data["referral"] == {"credited": True, "referrer": "Jayden"}
    u = _by_phone(db, "+15105550199")
    assert u.referral_code and u.referral_code.startswith("NATE")
    assert u.referred_by_user_id == friend.id and u.referral_code_entered == f"{code[:3].lower()}-{code[3:]}"
    st = referrals.stats(friend.id)
    assert (st["signups"], st["completed"], st["names"]) == (1, 0, ["Nate"])


def test_signup_without_a_code_or_with_an_unknown_one_is_not_credited(db, client):
    status, data = _signup(client, "+15105550198")
    assert status == 200 and data["referral"] is None
    u = _by_phone(db, "+15105550198")
    assert u.referred_by_user_id is None and u.referral_code_entered is None

    status, data = _signup(client, "+15105550197", referral_code="ZZZ9999")
    assert status == 200 and data["referral"] == {"credited": False, "referrer": None}
    u = _by_phone(db, "+15105550197")
    assert u.referred_by_user_id is None and u.referral_code_entered == "ZZZ9999", "kept for the 'but i typed it' check"


def test_signup_code_is_capped_and_never_blocks(db, client):
    status, data = _signup(client, "+15105550196", referral_code="x" * 500)
    assert status == 200 and data["referral"]["credited"] is False
    assert len(_by_phone(db, "+15105550196").referral_code_entered) == 32


def test_credit_signup_ignores_own_code_and_a_missing_user(db):
    import referrals
    me = make_user(db, name="Nau")
    own = referrals.ensure_code(me.id)
    r = referrals.credit_signup(me.id, own)
    assert r["credited"] is False and r["entered"] == own
    db.expire_all()
    assert db.get(type(me), me.id).referred_by_user_id is None
    assert referrals.credit_signup(999999, "NAU4821")["credited"] is False


def test_a_two_digit_code_is_regenerated_on_touch_and_by_backfill(db):
    import referrals
    from models import User
    a = make_user(db, name="Nau", referral_code="NAU42")
    b = make_user(db, name="Sam", referral_code="SAM07")
    c = make_user(db, name="Kim", referral_code="KIM4821")
    new_a = referrals.ensure_code(a.id)
    assert new_a != "NAU42" and new_a.startswith("NAU") and len(new_a) == 7
    assert referrals.lookup("NAU42") is None, "the old shape no longer credits anyone"
    assert referrals.backfill() == 1, "only Sam was left on the old shape"
    db.expire_all()
    assert len(db.get(User, b.id).referral_code) == 7
    assert db.get(User, c.id).referral_code == "KIM4821", "a current code is left alone"


def test_no_referral_question_in_the_text_thread(db, anthropic_stub, sms_capture):
    """The ask lives on the site. Onboarding's completion turn is unchanged: reaction + summary."""
    import onboarding_agent as oa
    from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
    from tests.tier1.test_onboarding_restructure_1 import _bodies, _coach_said, _row
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=142, occupation="student",
                       activity_level="lightly active", avg_steps=10000, workout_days="3", workout_time="18:00",
                       current_split="ppl", cooking_situation="mix", diet="omnivore", injuries="none",
                       wake_time="09:00", sleep_time="01:00")
    _coach_said(user, "any apps or a watch u use")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: json.dumps({"existing_tools": "fitbit"}) if _is_extract(kw) else "yeah fitbit's solid")
    assert oa.handle_onboarding_reply(user, "i use a fitbit") is True
    bodies = _bodies(sms_capture)
    assert bodies[0] == "yeah fitbit's solid" and bodies[1].startswith("ok so")
    assert not any("referral" in b.lower() for b in bodies)
    assert _row(db, user.id).onboarding_step == 3


def test_onboarding_prompt_carries_their_code_only_for_the_ask(db):
    import onboarding_agent as oa, referrals
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    db.expire_all()
    prompt = oa._build_system_prompt(db.get(type(user), user.id))
    code = referrals.ensure_code(user.id)
    assert f"Their referral code: {code}" in prompt and "ONLY if they ask" in prompt and "sign-up chat at cued.fit" in prompt


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
    assert f"their referral code is {code}" in out and "sign-up chat at cued.fit" in out
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
    from models import User
    top = make_user(db, name="Nau")
    second = make_user(db, name="Sam")
    for n in ("A", "B", "C"):
        make_user(db, name=f"Kid{n}", referred_by_user_id=top.id, onboarding_step=3)
    make_user(db, name="KidD", referred_by_user_id=second.id, onboarding_step=2)
    lone = make_user(db, name="Lone")
    make_user(db, name="Typo", referral_code_entered="NAU9999")
    r = client.get("/admin/referrals")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert html.index(">Nau<") < html.index(">Sam<"), "most sign-ups first"
    assert "KidD</a> (in setup)" in html
    assert "NAU9999 (no match)" in html
    db.expire_all()
    assert db.get(User, lone.id).referral_code, "the page backfills codes for everyone"
    board = referrals.leaderboard(db)
    assert [(b["name"], b["signups"], b["completed"]) for b in board] == [("Nau", 3, 3), ("Sam", 1, 0)]
