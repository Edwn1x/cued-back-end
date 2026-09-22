"""
Adaptive calorie targets (adaptive_targets.py; proposal in rewrite/proposals/).
Synthetic 4-week series drive the decision; the logging-completeness gate must
turn sparse logging into "no change + why", never a number.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

FOUNDER = dict(height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male",
               workout_days="4-5", avg_steps=10000, occupation="student", activity_level="active",
               workout_time="14:00", current_split="ppl", cooking_situation="cooks", diet="omnivore",
               injuries="none", wake_time="11:30", sleep_time="02:00", name="Nau", onboarding_step=3)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed(db, user, *, days=14, meals_per_day=3, kcal_per_day=2450, weights=None, created_days_ago=30):
    """meals for the last `days` local days + weigh-ins [(days_ago, lbs)]. Naive UTC, noon-ish."""
    from models import get_session, Meal, WeightLog, User
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.created_at = _now() - timedelta(days=created_days_ago)
        for d in range(days):
            day = _now() - timedelta(days=d)
            for i in range(meals_per_day):
                s.add(Meal(user_id=user.id, description=f"meal {i}", calories=int(kcal_per_day / meals_per_day),
                           eaten_at=day.replace(hour=12 + i * 3, minute=0, second=0, microsecond=0) - timedelta(hours=0)))
        for days_ago, lbs in (weights or []):
            s.add(WeightLog(user_id=user.id, weighed_at=_now() - timedelta(days=days_ago, hours=2), weight_lbs=lbs))
        s.commit()
    finally:
        s.close()


def _eval(db, user):
    from adaptive_targets import evaluate
    from models import get_session, User
    s = get_session()
    try:
        return evaluate(s.get(User, user.id), s)
    finally:
        s.close()


# ─── math ───────────────────────────────────────────────────────────────────

def test_ewma_smooths_and_seeds_on_the_first_point():
    from adaptive_targets import ewma
    t0 = _now() - timedelta(days=5)
    pts = [(t0 + timedelta(days=i), w) for i, w in enumerate([140, 143, 139, 141, 140])]
    tr = ewma(pts, alpha=0.1)
    assert tr[0][1] == 140.0
    assert tr[1][1] == 140.3            # 140 + 0.1*(143-140): a 3-lb jump moves the trend 0.3
    assert all(139 <= v <= 141 for _, v in tr)


def test_apply_goal_is_the_shared_rule():
    from macro_calculator import apply_goal, calculate_targets, goal_profile
    from types import SimpleNamespace as NS
    u = NS(**{k: v for k, v in FOUNDER.items() if k != "onboarding_step"}, goal="fat_loss,muscle_building")
    t = calculate_targets(u)
    assert apply_goal(t["tdee"], u.goal, **goal_profile(u))["calories"] == t["calories"] == 2450


# ─── gates ──────────────────────────────────────────────────────────────────

def test_sparse_logging_means_no_change_and_a_plain_reason(db):
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=1, weights=[(13, 139), (7, 139), (0, 139)])  # breakfast only
    r = _eval(db, user)
    assert r["change"] is False and r["new"] == 2250
    assert "only 0 of the last 14 days had 2+ meals logged" in r["reason"]


def test_too_few_weighins_means_no_change(db):
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, weights=[(13, 139), (0, 139)])
    r = _eval(db, user)
    assert r["change"] is False and "only 2 weigh-ins" in r["reason"]


def test_short_span_means_no_change(db):
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, weights=[(4, 139), (2, 139), (0, 139)])
    r = _eval(db, user)
    assert r["change"] is False and "span only" in r["reason"]


# ─── decisions ──────────────────────────────────────────────────────────────

def test_flat_trend_on_a_cut_moves_the_target_down_by_at_most_150(db):
    """Cut at 2250, eating 2250 on average, scale flat → expenditure ≈ 2250 → maintenance
    should be ~2250 not 2731 → cut target wants ~1750 → clamped to one −150 step."""
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, kcal_per_day=2250,
          weights=[(13, 139.0), (10, 139.2), (7, 138.9), (3, 139.1), (0, 139.0)])
    r = _eval(db, user)
    assert r["change"] is True and r["new"] == 2100, r
    assert r["counted_days"] >= 10 and r["weighins"] == 5
    assert "down 150" in r["reason"]


def test_cut_that_is_working_is_left_alone(db):
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, kcal_per_day=2250,
          weights=[(13, 141.0), (10, 140.4), (7, 139.8), (3, 139.1), (0, 138.6)])  # ~ -1.1 lb/wk raw
    r = _eval(db, user)
    assert r["change"] is False and "cut is working" in r["reason"]


def test_recomp_dropping_fast_moves_up(db):
    """Recomp at 2450, eating 2450, but the trend is falling >0.5 lb/wk → they burn more
    than we thought → target goes up (clamped to +150)."""
    user = make_user(db, goal="fat_loss,muscle_building", calorie_target=2450, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, kcal_per_day=2450,
          weights=[(13, 141.0), (10, 139.5), (7, 138.0), (3, 136.5), (0, 135.0)])
    r = _eval(db, user)
    assert r["change"] is True and r["new"] == 2600, r


def test_recomp_holding_flat_is_left_alone(db):
    user = make_user(db, goal="fat_loss,muscle_building", calorie_target=2450, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, kcal_per_day=2450,
          weights=[(13, 139.0), (10, 139.3), (7, 138.8), (3, 139.2), (0, 139.0)])
    r = _eval(db, user)
    assert r["change"] is False and "recomp holding flat" in r["reason"]


# ─── cycle writes + cadence ─────────────────────────────────────────────────

def test_apply_cycle_writes_a_row_every_run_and_only_when_due(db):
    from adaptive_targets import apply_cycle, run_all
    from models import get_session, TargetAdjustment, User
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, kcal_per_day=2250,
          weights=[(13, 139.0), (10, 139.2), (7, 138.9), (3, 139.1), (0, 139.0)], created_days_ago=20)
    assert run_all() == 1
    s = get_session()
    try:
        rows = s.query(TargetAdjustment).filter_by(user_id=user.id).all()
        assert len(rows) == 1 and rows[0].changed and rows[0].old_target == 2250 and rows[0].new_target == 2100
        u = s.get(User, user.id)
        assert u.calorie_target == 2100 and u.targets_source == "adaptive"
    finally:
        s.close()
    assert run_all() == 0, "not due again for 14 days"
    assert apply_cycle(user.id) is None
    r = apply_cycle(user.id, force=True)      # forced: a second row, now 'within noise' or a further step
    assert r is not None
    s = get_session()
    try:
        assert s.query(TargetAdjustment).filter_by(user_id=user.id).count() == 2
    finally:
        s.close()


def test_fresh_user_is_not_due(db):
    from adaptive_targets import run_all
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, created_days_ago=5,
          weights=[(13, 139.0), (7, 139.0), (0, 139.0)])
    assert run_all() == 0


# ─── log_weight tool ────────────────────────────────────────────────────────

def test_log_weight_writes_a_row_and_weight_follows_and_protein_follows(db):
    from agent_tools import handle_log_weight
    from models import get_session, WeightLog, User
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, targets_source="computed", **FOUNDER)
    out = handle_log_weight(user.id, {"weight_lbs": 145, "note": "after breakfast"})
    assert out.startswith("ok: logged 145 lb"), out
    assert "protein target 139 → 145g (follows weight)" in out
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.weight_lbs == 145 and u.protein_target == 145
        assert s.query(WeightLog).filter_by(user_id=user.id).count() == 1
    finally:
        s.close()


def test_log_weight_past_date_does_not_override_a_newer_reading(db):
    from agent_tools import handle_log_weight
    from models import get_session, User
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    handle_log_weight(user.id, {"weight_lbs": 140})
    out = handle_log_weight(user.id, {"weight_lbs": 150, "date": "yesterday"})
    assert "dated" in out
    s = get_session()
    try:
        assert s.get(User, user.id).weight_lbs == 140
    finally:
        s.close()


def test_log_weight_user_pick_protein_is_left_alone(db):
    from agent_tools import handle_log_weight
    from models import get_session, User
    user = make_user(db, goal="fat_loss", calorie_target=2200, protein_target=150, targets_source="user", **FOUNDER)
    out = handle_log_weight(user.id, {"weight_lbs": 145})
    assert "follows weight" not in out
    s = get_session()
    try:
        assert s.get(User, user.id).protein_target == 150
    finally:
        s.close()


def test_log_weight_rejects_implausible_and_records_no_scale(db):
    from agent_tools import handle_log_weight
    from models import get_session, User
    user = make_user(db, goal="fat_loss", **FOUNDER)
    assert "outside a plausible range" in handle_log_weight(user.id, {"weight_lbs": 30})   # a kg value typed as lb
    assert "must be a number" in handle_log_weight(user.id, {"weight_lbs": "heavy"})
    assert handle_log_weight(user.id, {"no_scale": True}) == "ok: noted — no scale, weigh-in nudges are off"
    s = get_session()
    try:
        assert s.get(User, user.id).weigh_in_opt_out is True
    finally:
        s.close()


# ─── context: coach loop + heartbeat ────────────────────────────────────────

def test_loop_context_shows_trend_and_todays_cycle_result(db):
    from agent_loop import build_loop_context
    from adaptive_targets import apply_cycle
    from models import get_session, User
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=3, kcal_per_day=2250,
          weights=[(13, 139.0), (10, 139.2), (7, 138.9), (3, 139.1), (0, 139.0)], created_days_ago=20)
    apply_cycle(user.id, force=True)
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## WEIGHT" in ctx and "Quote the TREND, never a single reading" in ctx
    assert "## TARGET CHANGED TODAY" in ctx and "2250 → 2100 cal" in ctx and "Mention it ONCE" in ctx


def test_loop_context_no_change_cycle_explains_the_gap(db):
    from agent_loop import build_loop_context
    from adaptive_targets import apply_cycle
    from models import get_session, User
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    _seed(db, user, days=14, meals_per_day=1, weights=[(13, 139), (7, 139), (0, 139)], created_days_ago=20)
    apply_cycle(user.id, force=True)
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## TARGET CHECK TODAY (no change)" in ctx and "meals logged" in ctx and "Don't nag" in ctx


def test_heartbeat_gets_the_weigh_in_condition_only_when_due(db, monkeypatch):
    import config, heartbeat
    from models import get_session, User, Message
    monkeypatch.setattr(config, "ADAPTIVE_TARGETS_ENABLED", True)
    due = make_user(db, goal="fat_loss", calorie_target=2250, **FOUNDER)                     # never weighed
    fresh = make_user(db, goal="fat_loss", calorie_target=2250, **FOUNDER)
    _seed(db, fresh, days=0, weights=[(2, 139)])                                                # weighed 2 days ago
    opted = make_user(db, goal="fat_loss", calorie_target=2250, weigh_in_opt_out=True, **FOUNDER)
    nudged = make_user(db, goal="fat_loss", calorie_target=2250, **FOUNDER)
    s = get_session()
    try:
        s.add(Message(user_id=nudged.id, direction="out", body="hop on the scale real quick when you're up",
                      message_type="heartbeat", created_at=_now() - timedelta(days=2)))
        s.commit()
        ctxs = {name: heartbeat._proactive_context(s.get(User, u.id), s)
                for name, u in (("due", due), ("fresh", fresh), ("opted", opted), ("nudged", nudged))}
    finally:
        s.close()
    assert "## WEIGH-IN DUE" in ctxs["due"] and "Last weigh-in: never" in ctxs["due"]
    for name in ("fresh", "opted", "nudged"):
        assert "WEIGH-IN DUE" not in ctxs[name], name


def test_profile_payload_has_weight_and_adjustments(db):
    from profile_page import build_profile_payload
    from adaptive_targets import apply_cycle
    from agent_tools import handle_log_weight
    from models import get_session, User
    user = make_user(db, goal="fat_loss", calorie_target=2250, protein_target=139, **FOUNDER)
    handle_log_weight(user.id, {"weight_lbs": 140})
    apply_cycle(user.id, force=True)
    s = get_session()
    try:
        p = build_profile_payload(s, s.get(User, user.id))
    finally:
        s.close()
    assert p["weight"]["latest_lbs"] == 140 and p["weight"]["trend_lbs"] == 140.0 and len(p["weight"]["logs"]) == 1
    assert len(p["target_adjustments"]) == 1 and p["target_adjustments"][0]["changed"] is False


def test_voice_routes_weight_to_log_weight_and_explains_cycles_once():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "A scale or a body-weight number" in v and "log_weight" in v
    assert "TARGET CHANGED TODAY / TARGET CHECK TODAY" in v and "mention ONCE" in v
