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
