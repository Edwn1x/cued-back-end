"""
Phase 5 — nightly consolidation. Deterministic core: a seeded messy profile comes
out clean, safety entries survive every pass, the bounded-delta guardrail aborts a
runaway run (live memory untouched), the change is named in a human-readable summary,
re-running is idempotent, a quiet night writes nothing, and rollback restores.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _iso_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _entry(text, *, uses=0, days_ago=0, safety=False, id=None):
    import uuid
    e = {"id": id or uuid.uuid4().hex[:12], "text": text,
         "ts": _iso_days_ago(days_ago), "uses": uses}
    if safety:
        e["safety"] = True
    return e


def _profile(uid):
    from models import get_session, User
    from memory import HISTORY_KEY
    s = get_session()
    try:
        prof = s.get(User, uid).user_profile_memory or {}
        return {c: list(v) for c, v in prof.items()}
    finally:
        s.close()


def _valid_texts(prof):
    from memory import HISTORY_KEY
    return {e["text"] for c, v in prof.items() if c != HISTORY_KEY for e in v}


def _runs(uid):
    from models import get_session, ConsolidationRun
    s = get_session()
    try:
        return s.query(ConsolidationRun).filter(ConsolidationRun.user_id == uid).all()
    finally:
        s.close()


def test_messy_profile_comes_out_clean(db):
    from tests.factories import make_user
    from consolidation import consolidate_user

    profile = {
        "goals": [
            _entry("wants to run a half marathon", uses=3, days_ago=1),        # keeper
            _entry("wants to run a half marathon soon", uses=0, days_ago=1),   # near-dupe -> merged
        ],
        "schedule": [
            _entry("trains 3 days per week", uses=2, days_ago=2),              # superseded
            _entry("trains 5 days per week", uses=2, days_ago=0),              # newest wins
        ],
        "identity": [
            _entry("mentioned liking jazz once", uses=0, days_ago=60),         # stale -> closed
        ],
        "constraints": [
            _entry("allergic to peanuts", uses=0, days_ago=200, safety=True),  # safety -> survives
        ],
        "training_preferences": [
            _entry("prefers morning workouts", uses=5, days_ago=3),            # healthy -> survives
        ],
    }
    user = make_user(db, user_profile_memory=profile)

    res = consolidate_user(user.id)
    assert res["status"] == "ok", res

    surviving = _valid_texts(_profile(user.id))
    assert surviving == {
        "wants to run a half marathon",
        "trains 5 days per week",
        "allergic to peanuts",
        "prefers morning workouts",
    }, surviving


def test_summary_names_the_changes(db):
    from tests.factories import make_user
    from consolidation import consolidate_user

    profile = {
        "identity": [_entry("mentioned liking jazz once", uses=0, days_ago=60)],
        "goals": [
            _entry("wants to bulk to 180", uses=2, days_ago=1),
            _entry("wants to bulk to 180 lbs", uses=0, days_ago=1),
        ],
        "training_preferences": [_entry("likes supersets", uses=4, days_ago=1)],  # survives -> not 100%
    }
    user = make_user(db, user_profile_memory=profile)

    res = consolidate_user(user.id)
    summary = res["summary"]
    assert "closed:" in summary and "jazz" in summary
    assert "merged:" in summary and "goals" in summary
    # persisted on the run row for the founder's daily audit
    run = _runs(user.id)[0]
    assert run.summary == summary and run.aborted is False


def test_safety_entry_is_never_closed(db):
    from tests.factories import make_user
    from consolidation import consolidate_user

    # an ancient, never-used safety fact — would be stale-closed if it weren't safety
    profile = {"constraints": [_entry("injured left shoulder — no overhead press",
                                      uses=0, days_ago=365, safety=True)]}
    user = make_user(db, user_profile_memory=profile)

    res = consolidate_user(user.id)
    assert res["status"] == "noop", res  # nothing removable -> quiet night
    assert "no overhead press" in " ".join(_valid_texts(_profile(user.id)))


def test_bounded_delta_aborts_and_leaves_memory_untouched(db):
    from tests.factories import make_user
    from consolidation import consolidate_user

    # 4 stale never-used non-safety entries: a clean run would remove ALL of them
    # (100% > 50% cap) -> abort, live memory unchanged.
    profile = {"goals": [_entry(f"old throwaway note {i}", uses=0, days_ago=90)
                         for i in range(4)]}
    user = make_user(db, user_profile_memory=profile)
    before = _valid_texts(_profile(user.id))

    res = consolidate_user(user.id)
    assert res["status"] == "aborted", res
    assert _valid_texts(_profile(user.id)) == before, "aborted run must not mutate memory"

    run = _runs(user.id)[0]
    assert run.aborted is True and run.prev_profile is not None


def test_quiet_night_writes_no_run(db):
    from tests.factories import make_user
    from consolidation import consolidate_user

    profile = {"goals": [_entry("wants to deadlift 405", uses=3, days_ago=1)]}
    user = make_user(db, user_profile_memory=profile)

    res = consolidate_user(user.id)
    assert res["status"] == "noop"
    assert _runs(user.id) == [], "a no-op night must not create a run row or summary"


def test_consolidation_is_idempotent(db):
    from tests.factories import make_user
    from consolidation import consolidate_user

    profile = {
        "identity": [_entry("liked jazz once", uses=0, days_ago=60)],
        "goals": [_entry("run a marathon", uses=4, days_ago=1)],
    }
    user = make_user(db, user_profile_memory=profile)

    first = consolidate_user(user.id)
    assert first["status"] == "ok"
    second = consolidate_user(user.id)
    assert second["status"] == "noop", "a settled profile must not keep changing"


def test_rollback_restores_prior_profile(db):
    from tests.factories import make_user
    from consolidation import consolidate_user, rollback

    profile = {
        "identity": [_entry("liked jazz once", uses=0, days_ago=60)],
        "goals": [_entry("run a marathon", uses=4, days_ago=1)],
    }
    user = make_user(db, user_profile_memory=profile)

    res = consolidate_user(user.id)
    assert "liked jazz once" not in _valid_texts(_profile(user.id))

    ok = rollback(user.id, res["run_id"])
    assert ok is True
    assert "liked jazz once" in _valid_texts(_profile(user.id)), "rollback must restore the closed entry"
