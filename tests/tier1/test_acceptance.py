"""
Roadmap-failure acceptance cases — the RATCHET.

Genuinely-broken-today cases are @pytest.mark.xfail(strict=True): the suite stays
green, but the day a phase fixes the behavior the test XPASSes and breaks the
build, forcing the marker's removal into that phase's diff. Cases whose fix has
no system to test against yet are skip-until (they can't fail for the right
reason, so xfail would be dishonest).

All assertions are on deterministic state / render / gate output — never on the
mocked model's words — so an XPASS means the behavior actually changed.
"""

from __future__ import annotations

import pytest


# ─── Failure 1: facts lost across the multi-agent split ──────────────────────

def test_cross_domain_fact_reaches_the_seeding_agent(db):
    """Control: a 'schedule' fact renders for training (schedule IS in its map).
    Proves the seed + render work, so the xfail below fails for the right reason."""
    from tests.factories import make_user
    from memory import apply_facts, build_memory_block

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "has an organic chem exam every friday morning",
        "replaces_text": None, "safety_critical": False,
    }])
    user.user_profile_memory = profile
    db.commit()
    assert "organic chem exam" in build_memory_block(user, "training")


@pytest.mark.xfail(strict=True, reason="failure 1 — per-agent slice map drops "
                   "cross-domain facts; fixed in Phase 2 (unified render)")
def test_cross_domain_fact_reaches_a_different_agent(db):
    """A schedule fact the user mentioned should be usable when a later message
    routes to nutrition. Today the per-agent slice map ('schedule' not in the
    nutrition map, and it isn't safety) drops it."""
    from tests.factories import make_user
    from memory import apply_facts, build_memory_block

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "has an organic chem exam every friday morning",
        "replaces_text": None, "safety_critical": False,
    }])
    user.user_profile_memory = profile
    db.commit()
    assert "organic chem exam" in build_memory_block(user, "nutrition")


# ─── Failure 3: scheduler blind to conversation ("already went") ─────────────

@pytest.mark.xfail(strict=True, reason="failure 3 — scheduler gates ignore "
                   "'already went'/'in class'; fixed in Phase 1 (Event table)")
def test_already_at_gym_suppresses_pre_workout_nudge(db, sms_capture):
    """User is already at the gym (session_state=at_gym). A pre_workout nudge
    should not fire. Today no scheduler gate consults that signal, so it does."""
    from tests.factories import make_user
    from models import set_session_state
    from engagement_tracker import should_send
    import scheduler

    user = make_user(
        db,
        confirmed_training_days="mon,tue,wed,thu,fri,sat,sun",  # always a training day
        unanswered_count=0,
    )
    set_session_state(user.id, "at_gym")

    # Guard: the ONLY reason to suppress should be the already-went signal, not
    # engagement gating — otherwise a spurious XPASS. Assert the message would
    # otherwise be allowed to send.
    assert should_send(user, "pre_workout"), "engagement gate would suppress; fix setup"

    scheduler.send_scheduled_message(user.id, "pre_workout")
    assert sms_capture == [], f"nudge fired despite already being at the gym: {sms_capture}"


# ─── Failure 5a: injuries are immortal (never heal) ──────────────────────────

@pytest.mark.xfail(strict=True, reason="failure 5a — no heal-invalidation path; "
                   "fixed when validity-window invalidation lands (Phase 1/3)")
def test_healed_injury_leaves_active_context(db, driver):
    """A previously-reported injury should leave active context once the user
    says it healed. Today nothing invalidates it, so it renders forever."""
    from tests.factories import make_user
    from memory import apply_facts, build_memory_block
    from models import get_session, User

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "constraints",
        "text": "tweaked left shoulder", "replaces_text": None,
        "safety_critical": True,
    }])
    user.user_profile_memory = profile
    db.commit()

    driver.send(user, "good news — my shoulder is all healed now, back to normal")

    s = get_session()
    try:
        fresh = s.query(User).get(user.id)
        assert "tweaked left shoulder" not in build_memory_block(fresh, "training")
    finally:
        s.close()


# ─── Failure 5b: contradictory numeric facts coexist ─────────────────────────

@pytest.mark.xfail(strict=True, reason="failure 5b — numeric-divergent facts "
                   "coexist; fixed in Phase 1 (substring-match + validity windows)")
def test_changed_numeric_fact_yields_one_current_value(db):
    """'trains 3 days/week' then 'trains 5 days/week' should collapse to one
    current fact. Today the dedup ladder treats different numbers as distinct
    facts, so both persist."""
    from memory import apply_facts

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "trains 3 days per week", "replaces_text": None,
        "safety_critical": False,
    }])
    # A later turn where the user says they now train 5 days — a plain 'add'
    # (the extractor doesn't know the exact prior text to 'replace').
    profile, _ = apply_facts(profile, [{
        "action": "add", "category": "schedule",
        "text": "trains 5 days per week", "replaces_text": None,
        "safety_critical": False,
    }])

    live = [e for e in profile["schedule"] if "days per week" in e["text"]]
    assert len(live) == 1, f"expected one current value, got {[e['text'] for e in live]}"
    assert "5" in live[0]["text"]


# ─── Failure 9 (investigation): no webhook idempotency ───────────────────────

@pytest.mark.xfail(strict=True, reason="webhook idempotency — no MessageSid "
                   "dedup, so a Twilio retry double-writes; fixed in Phase 1")
def test_duplicate_message_sid_produces_one_inbound(db, driver):
    """Twilio re-delivers a message (slow synchronous classify_message exceeds
    its ~15s webhook timeout). Replaying the same MessageSid must write state
    once. Today it writes twice — the likely duplicate-meal root cause."""
    from tests.factories import make_user
    from models import get_session, Message

    user = make_user(db)
    driver.send(user, "just crushed a chicken burrito bowl", message_sid="SMdup123")
    driver.send(user, "just crushed a chicken burrito bowl", message_sid="SMdup123")

    s = get_session()
    try:
        inbound = s.query(Message).filter(
            Message.user_id == user.id, Message.direction == "in").count()
    finally:
        s.close()
    assert inbound == 1, f"duplicate delivery wrote {inbound} inbound rows"


# ─── Failure 4 & correction-honesty: no system to test against yet ───────────

@pytest.mark.skip(reason="failure 4 — split-day consistency needs the Phase 1 "
                  "split pointer to assert on deterministically; lands Phase 1/2")
def test_split_day_is_consistent_within_a_day():
    ...


@pytest.mark.skip(reason="correction-honesty tier-1 — needs the Phase 2 loop + "
                  "Phase 3 manage_log tool before 'the agent must not claim an "
                  "action it can't take' is expressible")
def test_agent_does_not_confabulate_a_deletion_it_cannot_perform():
    ...
