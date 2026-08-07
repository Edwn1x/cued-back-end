"""
Fix 3 (heartbeat-stale-thread) — superseded safety:true states close via the existing
validity-window mechanism, freeing category room. Live evidence: 13 immortal
`constraints` entries, all one cyclospora/GI arc (Jul 20-31), where "gut has fully
recovered" coexists with five "currently experiencing…" states — 1,428 chars against
a 400-char soft cap, eviction-immune, and now demonstrably the reason real
grocery/food data gets evicted (Fix 2's log). Stale immortal entries cause live data
loss; this is no longer a deferred no-op.

Mechanism (deterministic, precision-first — modeled on the A5 regex-floor style):
a RESOLUTION-phrased safety entry ("recovered / resolved / back to normal") closes
OLDER safety entries in the same category only when ALL of these hold:
  - the older entry is transient-phrased ("currently / recently / still / holding
    off") or itself an older resolution (superseded recovery states collapse too),
  - the two share a body-system topic bucket (gut/stomach/GI ≠ shoulder ≠ knee …),
  - the older entry carries no allergy/intolerance vocabulary (an allergy is never
    machine-closed — the worst write this system can make stays impossible).
Every closure records a trigger (`resolved_by:<entry_id>:<text>`) and logs at
WARNING via the untouched Phase-1 invalidate_entry guard: a safety closure without
a trigger is still rejected. Durable-phrased safety facts ("bad knee - doctor said
no squats") are NEVER machine-closed, whatever the topic.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _iso_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed_gi_arc():
    """A condensed replica of the live 13-entry arc: three stale transient states +
    one older resolution, oldest first."""
    from memory import apply_facts
    profile, _ = apply_facts(None, [
        {"action": "add", "category": "constraints",
         "text": "currently experiencing symptoms consistent with a parasite infection",
         "replaces_text": None, "safety_critical": True},
        {"action": "add", "category": "constraints",
         "text": "recently ate 1 pound of ground beef despite active gastrointestinal "
                 "symptoms from cyclospora infection; may experience worsening digestive distress",
         "replaces_text": None, "safety_critical": True},
        {"action": "add", "category": "constraints",
         "text": "currently experiencing appetite suppression; stomach calming, experiencing gas",
         "replaces_text": None, "safety_critical": True},
        {"action": "add", "category": "constraints",
         "text": "holding off on steak while gut is still recovering from cyclospora infection",
         "replaces_text": None, "safety_critical": True},
    ])
    for i, days in enumerate((18, 17, 16, 15)):
        profile["constraints"][i]["ts"] = _iso_days_ago(days)
    return profile


def test_recovered_fact_closes_superseded_states(db, caplog):
    """The anchor: the recovery fact arrives (extraction or remember — both land in
    apply_facts) and the stale transient states close, each with a recorded trigger,
    at WARNING. The category's live chars drop back under crowding."""
    import logging
    from memory import apply_facts, HISTORY_KEY

    profile = _seed_gi_arc()
    with caplog.at_level(logging.WARNING, logger="cued.memory"):
        profile, stats = apply_facts(profile, [
            {"action": "add", "category": "constraints",
             "text": "gut has fully recovered from cyclospora infection; stomach "
                     "tolerating regular foods well",
             "replaces_text": None, "safety_critical": True},
        ])

    live = [e["text"] for e in profile["constraints"]]
    assert live == ["gut has fully recovered from cyclospora infection; stomach "
                    "tolerating regular foods well"], f"stale states survived: {live}"

    hist = profile.get(HISTORY_KEY, [])
    assert len(hist) == 4
    for h in hist:
        assert h["invalidated_by"] == "superseded_by_resolution"
        assert (h["invalidated_trigger"] or "").startswith("resolved_by:"), \
            "safety closure must record its trigger"
    # the Phase-1 guard's WARNING fired for every safety closure — never silent
    assert sum("SAFETY_INVALIDATION" in r.message for r in caplog.records) >= 4


def test_non_safety_flagged_recovery_still_closes_the_arc(db):
    """Live-gate finding (tier-2, run 2/3): the model emits the recovered state with
    safety_critical=False about half the time — the resolver must work either way.
    Precision lives in the closure conditions, not the resolver's flag."""
    from memory import apply_facts, HISTORY_KEY

    profile = _seed_gi_arc()
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "gut issues fully resolved as of Aug 6 2026 — back to normal meals",
         "replaces_text": None, "safety_critical": False},
    ])
    live = [e["text"] for e in profile["constraints"]]
    assert live == ["gut issues fully resolved as of Aug 6 2026 — back to normal meals"], \
        f"a non-safety-flagged recovery failed to close the arc: {live}"
    assert len(profile.get(HISTORY_KEY, [])) == 4


def test_newer_resolution_supersedes_older_resolution(db):
    """'gut has recovered' (Jul 25) + 'gut has fully recovered' (Jul 31) must not
    coexist — the newest resolution is the single surviving state."""
    from memory import apply_facts

    profile = _seed_gi_arc()
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "gut has recovered from cyclospora infection",
         "replaces_text": None, "safety_critical": True},
    ])
    profile["constraints"][-1]["ts"] = _iso_days_ago(13)
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "gut has fully recovered from cyclospora infection; stomach "
                 "tolerating regular foods well",
         "replaces_text": None, "safety_critical": True},
    ])
    live = [e["text"] for e in profile["constraints"]]
    assert len(live) == 1 and "fully recovered" in live[0]


def test_active_unrelated_safety_entry_is_never_closed(db):
    """A GI recovery must not touch an active shoulder injury — different body
    system, no topic overlap. The 'never close an active, non-superseded safety
    entry' guarantee."""
    from memory import apply_facts

    profile = _seed_gi_arc()
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "currently experiencing sharp left shoulder pain on heavy bench",
         "replaces_text": None, "safety_critical": True},
    ])
    profile["constraints"][-1]["ts"] = _iso_days_ago(5)
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "gut has fully recovered from cyclospora infection",
         "replaces_text": None, "safety_critical": True},
    ])
    live = [e["text"] for e in profile["constraints"]]
    assert any("shoulder" in t for t in live), "an unrelated ACTIVE safety entry was closed"


def test_allergy_vocabulary_is_never_machine_closed(db):
    """Even with topic overlap AND transient phrasing, allergy/intolerance entries
    are excluded from machine closure — only an explicit, triggered invalidate can
    touch them."""
    from memory import apply_facts

    profile, _ = apply_facts(None, [
        {"action": "add", "category": "constraints",
         "text": "currently experiencing stomach upset from dairy - lactose intolerant",
         "replaces_text": None, "safety_critical": True},
    ])
    profile["constraints"][0]["ts"] = _iso_days_ago(10)
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "stomach has fully recovered, digestion back to normal",
         "replaces_text": None, "safety_critical": True},
    ])
    live = [e["text"] for e in profile["constraints"]]
    assert any("lactose intolerant" in t for t in live), "an allergy was machine-closed"


def test_durable_phrased_safety_fact_is_never_machine_closed(db):
    """A doctor's-order style durable fact has no transient phrasing — topic overlap
    alone must not close it."""
    from memory import apply_facts

    profile, _ = apply_facts(None, [
        {"action": "add", "category": "constraints",
         "text": "chronic gut condition - doctor said avoid trigger foods",
         "replaces_text": None, "safety_critical": True},
    ])
    profile["constraints"][0]["ts"] = _iso_days_ago(30)
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "stomach bug fully recovered",
         "replaces_text": None, "safety_critical": True},
    ])
    live = [e["text"] for e in profile["constraints"]]
    assert any("doctor said" in t for t in live)


def test_trigger_guard_intact_and_flag_off_disables(db, monkeypatch):
    """Phase-1 guard regression: a trigger-less safety invalidation is still
    rejected. And the whole supersession pass is flag-gated."""
    import config
    from memory import apply_facts, invalidate_entry, HISTORY_KEY

    profile = _seed_gi_arc()
    entry_id = profile["constraints"][0]["id"]
    assert invalidate_entry(profile, entry_id, by="model", trigger=None) is False
    assert not profile.get(HISTORY_KEY)

    monkeypatch.setattr(config, "MEMORY_SAFETY_SUPERSESSION_ENABLED", False)
    profile, _ = apply_facts(profile, [
        {"action": "add", "category": "constraints",
         "text": "gut has fully recovered from cyclospora infection",
         "replaces_text": None, "safety_critical": True},
    ])
    assert len(profile["constraints"]) == 5, "flag off must disable machine closure"


def test_consolidation_closes_settled_arc_and_reports_it(db, caplog):
    """The nightly job closes an ALREADY-SETTLED arc (recovery + stale states both
    in place — the live prod shape, where no new write may ever arrive), reports it
    in the human-readable summary, and the dropped-safety invariant accepts exactly
    these audited closures and nothing else."""
    import logging
    from memory import apply_facts
    from consolidation import consolidate_user
    from tests.factories import make_user
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified

    user = make_user(db)
    profile = _seed_gi_arc()
    profile, _ = apply_facts(profile, [])   # no-op write; arc must persist untouched
    # settle the resolution INTO the profile with supersession off, mirroring prod
    # (the arc formed before the mechanism existed)
    import config
    orig = config.MEMORY_SAFETY_SUPERSESSION_ENABLED
    config.MEMORY_SAFETY_SUPERSESSION_ENABLED = False
    try:
        profile, _ = apply_facts(profile, [
            {"action": "add", "category": "constraints",
             "text": "gut has fully recovered from cyclospora infection; stomach "
                     "tolerating regular foods well",
             "replaces_text": None, "safety_critical": True},
        ] + [
            {"action": "add", "category": cat, "text": t,
             "replaces_text": None, "safety_critical": False}
            for cat, t in (("identity", "junior at berkeley"),
                           ("goals", "lean for summer"),
                           ("schedule", "tuesdays are class-heavy"),
                           ("training_preferences", "prefers morning lifts"))
        ])
    finally:
        config.MEMORY_SAFETY_SUPERSESSION_ENABLED = orig
    assert len(profile["constraints"]) == 5   # arc + resolution coexist (prod shape)

    s = get_session()
    try:
        u = s.get(User, user.id)
        u.user_profile_memory = profile
        flag_modified(u, "user_profile_memory")
        s.commit()
    finally:
        s.close()

    with caplog.at_level(logging.WARNING, logger="cued.memory"):
        result = consolidate_user(user.id)
    assert result["status"] == "ok", f"consolidation did not land: {result}"
    assert "recovered" in result["summary"] or "superseded" in result["summary"]
    assert sum("SAFETY_INVALIDATION" in r.message for r in caplog.records) >= 4

    s = get_session()
    try:
        prof = s.get(User, user.id).user_profile_memory
        live = [e["text"] for e in prof["constraints"]]
        assert len(live) == 1 and "fully recovered" in live[0]
    finally:
        s.close()
