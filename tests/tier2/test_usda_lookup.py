"""
Tier-2 (live) — macro-accuracy Phase D: USDA lookup end to end.

Case 1 hits the real USDA API only (no Anthropic spend) — the external contract
check. Case 2 runs the full loop: a weighed generic food should land near the
USDA-scaled truth. Uses USDA_API_KEY from the environment, falling back to
DEMO_KEY (30 req/hr — fine at this volume).

Run: pytest --run-tier2 -s tests/tier2/test_usda_lookup.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.tier2


def _key(monkeypatch):
    import config
    monkeypatch.setattr(config, "USDA_API_KEY",
                        os.getenv("USDA_API_KEY") or "DEMO_KEY")


def test_live_search_returns_plausible_generic_entry(db, monkeypatch):
    """External contract: a common generic food returns a parsed, macro-bearing
    entry (per 100g, KCAL energy). Guards against silent API-shape drift."""
    _key(monkeypatch)
    from usda import search_usda

    results = search_usda("grilled chicken breast")
    for r in results:
        print(f"[USDA] {r['description']!r} ({r['data_type']}) "
              f"{r['calories']}cal/{r['protein_g']}g protein per 100g")
    assert results, "live USDA search returned nothing for a staple food"
    top = results[0]
    assert "chicken" in top["description"].lower()
    # plain cooked chicken breast per 100g: broad plausibility bands only
    assert top["calories"] and 100 <= top["calories"] <= 300
    assert top["protein_g"] and 15 <= top["protein_g"] <= 40


def test_weighed_generic_food_logs_near_usda_scaled_truth(db, monkeypatch):
    """Full loop: '200g plain grilled chicken breast' ≈ 2 × (~165cal/31g). The
    stated weight removes portion uncertainty, so a big miss means the reference
    data (not the portion) was wrong. NOTE: a good generic estimate can also land
    here — the band is evidence, not proof, that USDA was consulted; the tool-use
    trace in the printed reply/log is the corroboration."""
    _key(monkeypatch)
    import config
    for f in ("LOG_MEAL_TOOL_ENABLED", "USDA_LOOKUP_TOOL_ENABLED",
              "MEAL_HISTORY_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, Meal, active

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "just ate 200g of plain grilled chicken breast, "
                                 "nothing on it", "freeform")
    print(f"\n[USDA-LOOP reply] {reply!r}")

    s = get_session()
    try:
        meals = active(s, Meal, user_id=user.id).all()
        for m in meals:
            print(f"[USDA-LOOP logged] {m.description!r} {m.calories}cal/{m.protein_g}g")
        total_cal = sum(m.calories or 0 for m in meals)
        total_pro = sum(m.protein_g or 0 for m in meals)
    finally:
        s.close()

    assert meals, f"weighed generic food never logged: {reply!r}"
    assert 260 <= total_cal <= 420, f"logged {total_cal} cal for 200g plain breast: {reply!r}"
    assert 50 <= total_pro <= 75, f"logged {total_pro}g protein for 200g plain breast: {reply!r}"
