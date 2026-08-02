"""
Macro-accuracy Phase B — user-history prior (the first moat source).

`meal_history.match_meal_history` is deterministic code: normalize → Jaccard →
group → median macros. Tier-1 pins the four spec cases (match surfaces the prior,
empty history stays empty, the chicken-rice→chicken-quinoa near-miss is rejected,
cross-user isolation) plus the seams that rot silently: soft-delete filtering,
portion-annotation stripping (Phase A writes portioned descriptions), median
aggregation, and the loop/tool wiring. Whether the MODEL leans on the surfaced
prior — and says "using your usual" only truthfully — is tier-2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_meal(db, user_id, description, *, calories=None, protein_g=None,
              carbs_g=None, fat_g=None, days_ago=1, deleted=False):
    from models import Meal
    when = _utcnow_naive() - timedelta(days=days_ago)
    m = Meal(user_id=user_id, description=description, calories=calories,
             protein_g=protein_g, carbs_g=carbs_g, fat_g=fat_g,
             eaten_at=when, source="text", log_type="user_reported",
             deleted_at=_utcnow_naive() if deleted else None)
    db.add(m)
    db.commit()
    return m


def test_prior_meal_surfaces_macros_for_matching_entry(db):
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    _add_meal(db, user.id, "chicken and rice", calories=650, protein_g=45, days_ago=3)

    matches = match_meal_history(user.id, "chicken and rice bowl")
    assert matches, "a logged repeat must surface"
    top = matches[0]
    assert top["count"] == 1
    assert top["calories"] == 650 and top["protein_g"] == 45
    assert "chicken" in top["description"].lower()


def test_no_history_no_false_match(db):
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    assert match_meal_history(user.id, "chicken and rice") == []


def test_near_but_different_meal_does_not_match(db):
    """The too-loose failure the spec names: quinoa is not rice."""
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    _add_meal(db, user.id, "chicken and rice", calories=650, protein_g=45)

    assert match_meal_history(user.id, "chicken and quinoa") == []


def test_cross_user_isolation(db):
    from tests.factories import make_user
    from meal_history import match_meal_history

    a = make_user(db)
    b = make_user(db)
    _add_meal(db, a.id, "chicken and rice", calories=650, protein_g=45)

    assert match_meal_history(b.id, "chicken and rice") == [], \
        "user A's history seeded user B's estimate"


def test_soft_deleted_meals_never_surface(db):
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    _add_meal(db, user.id, "chicken and rice", calories=650, protein_g=45, deleted=True)

    assert match_meal_history(user.id, "chicken and rice") == [], \
        "soft-deleted row resurrected through the matcher (bypassing the chokepoint)"


def test_portion_annotations_do_not_defeat_matching(db):
    """Phase A logs portioned descriptions; quantities/units must normalize away in
    BOTH directions."""
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    _add_meal(db, user.id, "chicken breast ~6oz, white rice ~2 cups",
              calories=620, protein_g=52)

    matches = match_meal_history(user.id, "chicken breast with rice")
    assert matches and matches[0]["calories"] == 620


def test_repeats_aggregate_with_median_macros(db):
    """Repeat logs collapse to one group; median resists the one-off mislog."""
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    for cal, pro, days in ((600, 45, 10), (650, 48, 6), (1400, 44, 3)):  # 1400 = mislog
        _add_meal(db, user.id, "chicken and rice", calories=cal, protein_g=pro, days_ago=days)

    matches = match_meal_history(user.id, "chicken and rice")
    assert len(matches) == 1, "same normalized meal must group, not list 3 times"
    top = matches[0]
    assert top["count"] == 3
    assert top["calories"] == 650, "median, not mean — the 1400 mislog must not drag the prior"
    assert top["protein_g"] == 45


def test_empty_query_matches_nothing(db):
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    _add_meal(db, user.id, "chicken and rice", calories=650)
    assert match_meal_history(user.id, "") == []
    assert match_meal_history(user.id, "~2 cups") == [], \
        "a query that is ONLY portion tokens has no content to match on"


# ---- tool surface -----------------------------------------------------------

def test_tool_handler_formats_match_and_fallthrough(db):
    from tests.factories import make_user
    from agent_tools import dispatch_tool

    user = make_user(db)
    _add_meal(db, user.id, "chicken and rice", calories=650, protein_g=45, days_ago=2)

    out = dispatch_tool("match_meal_history", {"description": "chicken and rice"}, user.id)
    assert out.startswith("ok:"), out
    assert "650" in out and "45" in out

    out = dispatch_tool("match_meal_history", {"description": "tofu scramble"}, user.id)
    assert "no history match" in out, "no-match branch must be a clean tool answer too"


def test_loop_offers_tool_iff_flag_on(db, monkeypatch, anthropic_stub):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    captured = {}

    def handler(kw):
        captured["tools"] = [t.get("name") for t in kw.get("tools", [])]
        return "sounds good"

    anthropic_stub.reply_with(handler)
    user = make_user(db)

    monkeypatch.setattr(config, "MEAL_HISTORY_TOOL_ENABLED", True)
    run_agent_loop(user, "had my usual lunch", "freeform")
    assert "match_meal_history" in captured["tools"]

    monkeypatch.setattr(config, "MEAL_HISTORY_TOOL_ENABLED", False)
    run_agent_loop(user, "had my usual lunch", "freeform")
    assert "match_meal_history" not in captured["tools"]
