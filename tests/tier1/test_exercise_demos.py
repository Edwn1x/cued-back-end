"""
Exercise demo video library (exercise_demos.py).

The resolver is PURE (no DB write, clock-independent) and CONSERVATIVE: it only returns
a demo when the name genuinely maps to the SAME movement, and misses (returns {}) rather
than risk a wrong-movement link. These tests pin the resolve/alias/seen/limit/flag
behaviour with plain stub users — no Postgres, no wall clock.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import config
import exercise_demos
from exercise_demos import EXERCISE_DEMO_LINKS, _normalize, unseen_demos_for


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)


def _user(seen=None):
    return NS(id=1, seen_exercise_demos=seen)


# ─── normalize ───────────────────────────────────────────────────────────────

def test_normalize_handles_spaces_hyphens_case():
    assert _normalize("  Pull-Ups ") == "pull_ups"
    assert _normalize("Romanian Deadlift") == "romanian_deadlift"
    assert _normalize("PULL   UP") == "pull_up"
    assert _normalize("bench-press") == "bench_press"


# ─── happy path: known movement ──────────────────────────────────────────────

def test_returns_demo_for_known_movement():
    out = unseen_demos_for(_user(), ["pull up"])
    assert out == {"pull up": EXERCISE_DEMO_LINKS["pull_up"]}


def test_direct_slug_key_resolves():
    out = unseen_demos_for(_user(), ["romanian_deadlift"])
    assert out == {"romanian_deadlift": EXERCISE_DEMO_LINKS["romanian_deadlift"]}


# ─── aliases (same-movement only) ────────────────────────────────────────────

@pytest.mark.parametrize("name,key", [
    ("pullups", "pull_up"),
    ("chin up", "pull_up"),
    ("rdl", "romanian_deadlift"),
    ("romanian dl", "romanian_deadlift"),
    ("ohp", "overhead_press"),
    ("military press", "overhead_press"),
    ("back squat", "squat"),
    ("deadlifts", "deadlift"),
    ("incline bench", "incline_bench_press"),
    ("dips", "weighted_dips"),
    ("lateral raises", "lateral_raise"),
    ("leg extensions", "leg_extension"),
    ("pec deck", "machine_pec_deck"),
])
def test_alias_resolves_to_same_movement(name, key):
    out = unseen_demos_for(_user(), [name])
    assert out == {name: EXERCISE_DEMO_LINKS[key]}


# ─── misses: no SAME-movement key → return {}, NEVER a wrong-movement link ────

@pytest.mark.parametrize("name", [
    "barbell curl",      # no generic curl entry (bayesian_cable_curl is a specific variation)
    "bb curl",
    "bicep curl",
    "back extension",    # no entry
    "back extensions",
    "hyperextension",
    "lat pulldown",      # a vertical pull — NOT machine_lat_pullover (a straight-arm pullover)
    "lat_pulldown",
    "seated calf raise", # only standing_calf_raise exists (different emphasis)
    "lying leg curl",    # only seated_leg_curl exists
    "face pull",
    "leg press",
])
def test_unmapped_movements_miss(name):
    assert unseen_demos_for(_user(), [name]) == {}


def test_lat_pulldown_never_returns_pullover_link():
    out = unseen_demos_for(_user(), ["lat pulldown"])
    assert out == {}
    assert EXERCISE_DEMO_LINKS["machine_lat_pullover"] not in out.values()


# ─── seen-tracking ───────────────────────────────────────────────────────────

def test_returns_empty_when_already_seen():
    assert unseen_demos_for(_user(seen={"pull_up": True}), ["pull up"]) == {}


def test_skips_seen_and_returns_next_unseen():
    out = unseen_demos_for(_user(seen={"pull_up": True}), ["pull up", "squat"])
    assert out == {"squat": EXERCISE_DEMO_LINKS["squat"]}


# ─── limit ───────────────────────────────────────────────────────────────────

def test_respects_limit_one_by_default():
    out = unseen_demos_for(_user(), ["pull up", "squat", "deadlift"])
    assert len(out) == 1


def test_respects_higher_limit_and_dedups_keys():
    out = unseen_demos_for(_user(), ["pull up", "squat", "deadlift"], limit=2)
    assert len(out) == 2
    # "squats" and "squat" resolve to the same key → only one squat demo
    out2 = unseen_demos_for(_user(), ["squat", "squats"], limit=5)
    assert list(out2.values()) == [EXERCISE_DEMO_LINKS["squat"]]


# ─── purity: NO DB writes ────────────────────────────────────────────────────

def test_resolver_performs_no_db_writes(monkeypatch):
    # If the resolver ever reached for a session, this would blow up the test.
    import models
    monkeypatch.setattr(models, "get_session",
                        lambda: (_ for _ in ()).throw(AssertionError("unseen_demos_for wrote to DB")))
    seen = {"pull_up": True}
    out = unseen_demos_for(_user(seen=seen), ["squat", "pull up"])
    assert out == {"squat": EXERCISE_DEMO_LINKS["squat"]}
    # the caller's seen dict is left untouched (pure)
    assert seen == {"pull_up": True}


# ─── flag gate ───────────────────────────────────────────────────────────────

def test_flag_off_injects_nothing(monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", False)
    assert unseen_demos_for(_user(), ["pull up"]) == {}


# ─── mark_demos_seen (background writer) is a no-op when the flag is off ──────

def test_mark_demos_seen_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", False)
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("should not open a session when flag is off")

    import models
    monkeypatch.setattr(models, "get_session", _boom)
    exercise_demos.mark_demos_seen(1, ["pull_up"])  # must not spawn/write
    assert called["n"] == 0


# ─── build_loop_context injection path (create path) ─────────────────────────
# These exercise the WIRING in agent_loop.build_loop_context — a real user with a
# card whose exercises resolve to a demo — not just the resolver in isolation.

from tests.factories import make_user

_PULL_DAY = {"pull": [{"slug": "pull_up", "label": "pull up", "sets": 3, "reps": 8}]}
_PULL_URL = EXERCISE_DEMO_LINKS["pull_up"]


def test_build_loop_context_injects_form_demo_block(db, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)
    # keep the background write out of the test DB; assert it's asked for the right key
    marked = {}
    monkeypatch.setattr(exercise_demos, "mark_demos_seen",
                        lambda uid, keys: marked.update(user_id=uid, keys=list(keys)))
    from agent_loop import build_loop_context
    u = make_user(db, custom_templates=_PULL_DAY)
    ctx = build_loop_context(u, db)
    assert "## FORM DEMO (share naturally if it fits — one time only)" in ctx
    assert f"pull up: {_PULL_URL}" in ctx
    # only the injected movement is marked seen
    assert marked.get("keys") == ["pull_up"] and marked.get("user_id") == u.id


def test_build_loop_context_no_form_demo_when_flag_off(db, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", False)
    from agent_loop import build_loop_context
    u = make_user(db, custom_templates=_PULL_DAY)
    ctx = build_loop_context(u, db)
    assert "## FORM DEMO" not in ctx


def test_build_loop_context_suppresses_already_seen_demo(db, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)
    monkeypatch.setattr(exercise_demos, "mark_demos_seen", lambda uid, keys: None)
    from agent_loop import build_loop_context
    u = make_user(db, custom_templates=_PULL_DAY, seen_exercise_demos={"pull_up": True})
    ctx = build_loop_context(u, db)
    assert "## FORM DEMO" not in ctx
    assert _PULL_URL not in ctx


def test_build_loop_context_fail_open_when_resolver_raises(db, monkeypatch):
    monkeypatch.setattr(config, "EXERCISE_DEMOS_ENABLED", True)

    def _boom(*a, **k):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(exercise_demos, "unseen_demos_for", _boom)
    from agent_loop import build_loop_context
    u = make_user(db, custom_templates=_PULL_DAY)
    ctx = build_loop_context(u, db)          # must not raise
    assert "## FORM DEMO" not in ctx
    assert isinstance(ctx, str) and ctx      # normal context still produced
