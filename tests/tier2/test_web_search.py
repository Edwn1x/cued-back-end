"""
Tier-2 (live) — web_search output hygiene + cost. The coach can look things up but
must speak findings naturally and never paste links (note: output hygiene). Also
records token cost; web_search bills per-search on top of tokens (priced in the
phase summary because Phase 4's heartbeat could invoke it).
Run: pytest --run-tier2 -s.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2


def test_web_search_output_hygiene_and_cost(db, monkeypatch):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, TokenUsage

    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    user = make_user(db, which_gym="rsf", name="Sam")

    reply = run_agent_loop(
        user, "quick q — what time does the RSF close on saturdays this summer? look it up",
        "freeform")
    print(f"\n[WEB_SEARCH] reply: {reply}")

    low = reply.lower()
    assert "http://" not in low and "https://" not in low and "www." not in low, \
        "pasted a raw link into the SMS (output-hygiene rule)"
    assert "[" not in reply or "]" not in reply.split("[", 1)[-1], \
        "looks like a reference-style citation leaked into the text"

    s = get_session()
    try:
        rows = s.query(TokenUsage).filter(
            TokenUsage.user_id == user.id, TokenUsage.site == "agent_loop.run").all()
    finally:
        s.close()
    std = sum(r.cost_usd or 0 for r in rows)
    print(f"[WEB_SEARCH] token cost (standard): ${std:.5f} over {len(rows)} model call(s)")
    print("[WEB_SEARCH] NOTE: web_search also bills ~$0.01/search separately from tokens — "
          "record the search count from the API/console for the phase summary.")


def test_actionable_logistics_are_verified_not_recalled(db, monkeypatch):
    """Parasite-pattern generalization: a location/directions claim the user will
    physically act on must be SEARCHED, not recalled from memory. Judged: the reply
    should either search or hedge — never assert confident campus directions cold."""
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, TokenUsage

    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    user = make_user(db, name="Sam", which_gym="rsf")

    reply = run_agent_loop(
        user,
        "i'm sick and have a 9am in li ka shing — how do i get there and how long's the walk from unit 2?",
        "freeform")
    print(f"\n[VERIFY-LOGISTICS] reply: {reply}")

    s = get_session()
    try:
        searched = s.query(TokenUsage).filter(
            TokenUsage.user_id == user.id, TokenUsage.site == "agent_loop.run").count() > 1
    finally:
        s.close()
    low = reply.lower()
    hedged = any(k in low for k in ("not sure", "double-check", "check", "map", "look",
                                    "let me", "i'd confirm", "confirm"))
    # Either it actually searched (multiple loop calls) or it hedged rather than
    # asserting confident directions. A cold confident route is the failure.
    assert searched or hedged, (
        "gave logistics the user will act on without searching or hedging — "
        f"reply: {reply!r}")
    assert "http" not in low, "no raw links in an SMS"
