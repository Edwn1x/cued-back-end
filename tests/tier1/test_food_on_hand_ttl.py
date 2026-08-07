"""
Fix 2 (heartbeat-stale-thread) — grocery/on-hand food is transient INVENTORY, not an
immortal constraint. Live Aug 7: a TJ's grocery haul was remembered into `constraints`
— the category whose safety entries are eviction-immune — and the 400-char soft cap
then evicted a prior food-on-hand list (MEMORY_EVICT reason=category_soft_cap),
silently dropping real image-persisted food facts (undoing the tenders-persistence
fix at the cap layer).

The fix: a dedicated non-immortal `food_on_hand` category. Entries there age out via
a TTL (FOOD_ON_HAND_TTL_DAYS, 0=off) — groceries bought Aug 6 are irrelevant by Aug
20 — reusing the freshness discipline (validity windows: expiry INVALIDATES into
__history__, auditable, never a silent delete). Safety facts are untouched: they
can't land in a TTL'd category's expiry (safety is skipped), and constraints keeps
its immortality.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _utcnow():
    return datetime.now(timezone.utc)


def _iso_days_ago(days):
    return (_utcnow() - timedelta(days=days)).isoformat()


def test_food_on_hand_is_a_category_and_reaches_the_remember_tool():
    from memory import CATEGORIES, CATEGORY_INJECTION_MAP
    from agent_tools import REMEMBER_TOOL

    assert "food_on_hand" in CATEGORIES
    assert "food_on_hand" in REMEMBER_TOOL["input_schema"]["properties"]["category"]["enum"]
    # the tool description must ROUTE on-hand food there, away from constraints
    desc = REMEMBER_TOOL["description"]
    assert "food_on_hand" in desc
    # nutrition's legacy per-agent slice sees inventory (the live unified loop
    # renders all categories and needs no map change)
    assert "food_on_hand" in CATEGORY_INJECTION_MAP["nutrition"]["categories"]


def test_grocery_fact_is_non_immortal_and_evictable(db):
    """The core of the fix: an on-hand food fact carries no safety flag and IS a
    legitimate eviction target — unlike a real safety constraint."""
    from memory import apply_facts, _evict_one

    profile, stats = apply_facts(None, [
        {"action": "add", "category": "food_on_hand",
         "text": "TJ's run Aug 6 - on hand: dozen eggs, chicken tenders, 2x ground beef",
         "replaces_text": None, "safety_critical": False},
        {"action": "add", "category": "constraints",
         "text": "severe peanut allergy", "replaces_text": None, "safety_critical": True},
    ])
    assert stats["added"] == 2
    food = profile["food_on_hand"][0]
    assert not food.get("safety"), "on-hand food must never be safety/immortal"

    # eviction pressure inside food_on_hand takes the food fact; the safety
    # constraint is untouchable even under direct pressure on its category
    assert _evict_one(profile, prefer_category="food_on_hand", reason="test") is True
    assert _evict_one(profile, prefer_category="constraints", reason="test") is False
    assert profile["constraints"][0]["text"] == "severe peanut allergy"


def test_food_on_hand_expires_after_ttl(db):
    """A stale grocery entry is invalidated (into __history__, auditable) on the
    next write pass; a fresh one survives. Expiry is invalidation, not deletion."""
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [
        {"action": "add", "category": "food_on_hand",
         "text": "on hand: NY strip steak, pork, fruit", "replaces_text": None,
         "safety_critical": False},
        {"action": "add", "category": "food_on_hand",
         "text": "on hand: fresh salmon from today's run", "replaces_text": None,
         "safety_critical": False},
    ])
    profile["food_on_hand"][0]["ts"] = _iso_days_ago(20)   # stale (default TTL 14)
    profile["food_on_hand"][1]["ts"] = _iso_days_ago(2)    # fresh

    # any subsequent write pass sweeps TTLs (even a no-op facts list)
    profile, _ = apply_facts(profile, [])

    live = [e["text"] for e in profile["food_on_hand"]]
    assert live == ["on hand: fresh salmon from today's run"]
    hist = profile.get(HISTORY_KEY, [])
    assert len(hist) == 1
    assert hist[0]["invalidated_by"] == "expired:ttl"
    assert "NY strip" in hist[0]["text"]


def test_ttl_zero_disables_expiry(db, monkeypatch):
    import config
    from memory import apply_facts, HISTORY_KEY

    monkeypatch.setattr(config, "FOOD_ON_HAND_TTL_DAYS", 0)
    profile, _ = apply_facts(None, [
        {"action": "add", "category": "food_on_hand",
         "text": "on hand: very old rice", "replaces_text": None, "safety_critical": False},
    ])
    profile["food_on_hand"][0]["ts"] = _iso_days_ago(90)

    profile, _ = apply_facts(profile, [])
    assert [e["text"] for e in profile["food_on_hand"]] == ["on hand: very old rice"]
    assert not profile.get(HISTORY_KEY)


def test_ttl_never_touches_safety_or_other_categories(db):
    """Belt+suspenders: a safety-flagged entry inside the TTL'd category is skipped,
    and old entries in non-TTL categories (goals, constraints) never expire."""
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [
        {"action": "add", "category": "food_on_hand",
         "text": "keeps epi-pen in the fridge door", "replaces_text": None,
         "safety_critical": True},
        {"action": "add", "category": "goals",
         "text": "lean for summer", "replaces_text": None, "safety_critical": False},
        {"action": "add", "category": "constraints",
         "text": "tweaked left shoulder", "replaces_text": None, "safety_critical": True},
    ])
    for cat in ("food_on_hand", "goals", "constraints"):
        profile[cat][0]["ts"] = _iso_days_ago(90)

    profile, _ = apply_facts(profile, [])
    assert len(profile["food_on_hand"]) == 1
    assert len(profile["goals"]) == 1
    assert len(profile["constraints"]) == 1
    assert not profile.get(HISTORY_KEY)


def test_consolidation_expires_stale_food_on_hand(db):
    """The nightly job sweeps TTLs too (covers write-quiet users) and reports the
    close in the human-readable summary — the founder's daily sanity check."""
    from memory import apply_facts
    from consolidation import consolidate_user
    from tests.factories import make_user
    from models import get_session, User

    user = make_user(db)
    profile, _ = apply_facts(None, [
        {"action": "add", "category": "food_on_hand",
         "text": "on hand: expired groceries from weeks ago", "replaces_text": None,
         "safety_critical": False},
        {"action": "add", "category": "goals",
         "text": "lean for summer", "replaces_text": None, "safety_critical": False},
        {"action": "add", "category": "identity",
         "text": "junior at berkeley", "replaces_text": None, "safety_critical": False},
    ])
    profile["food_on_hand"][0]["ts"] = _iso_days_ago(20)
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.user_profile_memory = profile
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(u, "user_profile_memory")
        s.commit()
    finally:
        s.close()

    result = consolidate_user(user.id)
    assert result["status"] == "ok"
    assert "expired groceries" in result["summary"] or "ttl" in result["summary"].lower()

    s = get_session()
    try:
        prof = s.get(User, user.id).user_profile_memory
        assert prof["food_on_hand"] == []
        assert [e["text"] for e in prof["goals"]] == ["lean for summer"]
    finally:
        s.close()
