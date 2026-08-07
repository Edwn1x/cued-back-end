"""
Tier-2 (live) — heartbeat-stale-thread Fixes 2+3 gates. Tier-1 proved the storage
mechanics (TTL aging, trigger-audited safety supersession); these check the MODEL
actually routes through them on the live surface:

- Fix 2 gate (binary): a typed grocery-haul message must NOT land in `constraints`
  (the immortal safety bucket — the live Aug 7 miss); if the model saves it, it
  belongs in `food_on_hand`.
- Fix 3 gate (binary): "stomach's fully back to normal" against seeded active GI
  safety states must CLOSE those states (the recovered fact arrives → storage
  supersedes) — the arc must not keep coexisting.

Run: pytest --run-tier2 -s tests/tier2/test_food_and_safety_routing.py
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2


def _enable(monkeypatch):
    import config
    for f in ("REMEMBER_TOOL_ENABLED", "LOG_MEAL_TOOL_ENABLED",
              "MANAGE_LOG_TOOL_ENABLED", "LOG_EVENT_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _profile(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return dict(s.get(User, user_id).user_profile_memory or {})
    finally:
        s.close()


_GROCERY_WORDS = ("egg", "tender", "ground beef", "strip", "grocer", "trader joe", "tj's")


def test_grocery_haul_routes_to_food_on_hand_not_constraints(db, monkeypatch):
    """The live Aug 7 failure, replayed as a hard anchor: the TJ's haul must not
    become an immortal constraint. Binary on the mis-route; whether the model saves
    at all is printed (persistence is the image-suite's gate, not this one)."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from sms import log_incoming

    user = make_user(db, name="Sam", calorie_target=2800, protein_target=170)
    body = ("just did a trader joe's run for the week - dozen eggs, chicken tenders, "
            "2lb ground beef, a ny strip steak. use this stuff for my meals")
    log_incoming(user.id, body)
    reply = run_agent_loop(user, body, "freeform")
    print(f"\n[ROUTING grocery] coach: {reply!r}")

    prof = _profile(user.id)
    constraint_texts = [e.get("text", "").lower() for e in prof.get("constraints") or []]
    on_hand_texts = [e.get("text", "").lower() for e in prof.get("food_on_hand") or []]
    print(f"[ROUTING grocery] constraints={constraint_texts} food_on_hand={on_hand_texts}")

    hits_in_constraints = [t for t in constraint_texts
                           if any(w in t for w in _GROCERY_WORDS)]
    assert not hits_in_constraints, (
        f"GROCERY MIS-ROUTE: on-hand food landed in the immortal constraints bucket "
        f"again — the exact live failure. {hits_in_constraints}")
    if not on_hand_texts:
        print("[ROUTING grocery] FINDING: the model saved no on-hand inventory this "
              "turn — routing is clean but capture didn't happen; watch the "
              "image-persistence suite before concluding.")


def test_recovery_message_closes_seeded_active_gi_states(db, monkeypatch):
    """Fix 3 end-to-end: two active GI safety states are on file; the user reports
    full recovery; after the turn the stale states must be CLOSED (in history), not
    coexisting with the recovered fact — the live 13-entry arc must be impossible
    to rebuild."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from sms import log_incoming
    from memory import apply_facts, HISTORY_KEY
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified

    user = make_user(db, name="Sam")
    profile, _ = apply_facts(None, [
        {"action": "add", "category": "constraints",
         "text": "currently experiencing symptoms consistent with a stomach parasite infection",
         "replaces_text": None, "safety_critical": True},
        {"action": "add", "category": "constraints",
         "text": "currently experiencing appetite suppression from the gut infection",
         "replaces_text": None, "safety_critical": True},
    ])
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=12)).isoformat()
    for e in profile["constraints"]:
        e["ts"] = stale_ts
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.user_profile_memory = profile
        flag_modified(u, "user_profile_memory")
        s.commit()
    finally:
        s.close()

    body = ("good news - stomach is fully back to normal, gut's recovered. "
            "been eating regular meals all week, zero issues")
    log_incoming(user.id, body)
    reply = run_agent_loop(user, body, "freeform")
    print(f"\n[RECOVERY] coach: {reply!r}")

    prof = _profile(user.id)
    live = [e.get("text", "") for e in prof.get("constraints") or []]
    hist = [h.get("text", "") for h in prof.get(HISTORY_KEY) or []]
    print(f"[RECOVERY] live constraints={live}")
    print(f"[RECOVERY] history={hist}")

    still_active = [t for t in live if "currently experiencing" in t.lower()]
    assert not still_active, (
        f"RECOVERY GATE FAILED: stale 'currently experiencing' safety states are "
        f"still live after the user reported full recovery — the immortal-arc "
        f"crowding rebuilds. Still live: {still_active}")
