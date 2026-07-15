"""
Rule-3 invariants + early-return regression net — GREEN TODAY, must STAY green
through every phase. These are the tripwires: if a later phase breaks the safety
floor or lets a goodnight drop an injury, one of these goes red.

(Species 1 of 3. The broken roadmap failures live in test_acceptance.py as
strict-xfail; cases with no system yet are skip-until there.)
"""

from __future__ import annotations


# ─── Safety floor (Rule 3: deterministic, no LLM in the path) ────────────────

def test_safety_allergy_is_captured_by_regex_prepass(db):
    """An allergy reaches User.restrictions via the regex pre-pass, no LLM."""
    from tests.factories import make_user
    from memory import apply_safety_signals_task
    from models import get_session, User

    user = make_user(db)
    apply_safety_signals_task(user.id, "hey just fyi i'm allergic to peanuts")

    s = get_session()
    try:
        fresh = s.query(User).get(user.id)
        assert "peanut" in (fresh.restrictions or "").lower()
    finally:
        s.close()


def test_safety_injury_is_captured_as_safety_constraint(db):
    """An injury lands in the JSON profile as a safety:true constraint, no LLM."""
    from tests.factories import make_user
    from memory import apply_safety_signals_task
    from models import get_session, User

    user = make_user(db)
    apply_safety_signals_task(user.id, "my shoulder's killing me today")

    s = get_session()
    try:
        fresh = s.query(User).get(user.id)
        constraints = (fresh.user_profile_memory or {}).get("constraints", [])
        assert any(e.get("safety") for e in constraints), constraints
    finally:
        s.close()


def test_safety_constraint_renders_for_every_agent(db):
    """A safety:true constraint is injected for agents whose slice map does NOT
    include 'constraints' (nutrition, coach) — the universal safety floor that
    Phase 2's unified render must preserve."""
    from tests.factories import make_user
    from memory import apply_facts, build_memory_block

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "constraints",
        "text": "torn left rotator cuff", "replaces_text": None,
        "safety_critical": True,
    }])
    user.user_profile_memory = profile
    db.commit()

    for agent in ("nutrition", "coach", "training", "readiness"):
        assert "torn left rotator cuff" in build_memory_block(user, agent), \
            f"safety entry missing from {agent} block"


# ─── Early-return branch regression net (primary behavior works today) ───────

def test_goodnight_suppresses_further_outbound_and_sets_quiet_hours(db, driver):
    """Goodnight sends one signoff, sets quiet_until, and does NOT run the coach loop."""
    from tests.factories import make_user
    from models import get_session, User

    user = make_user(db)
    replies = driver.send(user, "goodnight")

    assert len(replies) == 1, f"expected exactly one signoff, got {replies}"
    s = get_session()
    try:
        fresh = s.query(User).get(user.id)
        assert fresh.quiet_until is not None
    finally:
        s.close()


def test_goodnight_still_captures_a_safety_fact(db, driver):
    """The documented fix: safety pre-pass runs at the top of the webhook, so an
    injury reported alongside a goodnight is NOT dropped by the early return."""
    from tests.factories import make_user
    from models import get_session, User

    user = make_user(db)
    driver.send(user, "night, my knee is killing me")

    s = get_session()
    try:
        fresh = s.query(User).get(user.id)
        constraints = (fresh.user_profile_memory or {}).get("constraints", [])
        assert any(e.get("safety") for e in constraints), \
            "injury dropped on the goodnight early-return path"
    finally:
        s.close()
