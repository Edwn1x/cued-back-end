"""
Adaptive calorie targets — rewrite/proposals/adaptive-targets.md, built 2026-09-14.

Every predictive equation is a one-time guess (±10%, i.e. ±250 cal for a 139-lb
lifter). This closes the loop: what the scale does (EWMA trend over weigh-ins)
against what they logged eating → an expenditure estimate → a bounded, damped,
explained move of users.calorie_target every CYCLE_DAYS. The logging-completeness
gate is the whole game: sparse logging must produce "no change + why", never a
number. Every cycle writes a target_adjustments row (changed or not) so the
coach can explain it once (agent_loop context) and the admin/profile pages can
show the history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from models import get_session, User, Meal, WeightLog, TargetAdjustment, active
from macro_calculator import calculate_targets, apply_goal, goal_profile, override_bounds

logger = logging.getLogger("cued.adaptive")

EWMA_ALPHA = 0.3              # smoothing per weigh-in. 0.1 (MacroFactor's) assumes DAILY data;
                              # at 1–2 weigh-ins a week it lagged a real 2.4-lb drop down to
                              # 0.6 in the tests. 0.3 still eats a 3-lb water swing (→ 0.9).
WINDOW_DAYS = 14
CYCLE_DAYS = 14
MIN_COUNTED_DAYS = 10         # well-logged days in the window
MEALS_PER_COUNTED_DAY = 2     # a day with <2 meals logged is not a logged day
MIN_WEIGHINS = 3
MIN_SPAN_DAYS = 10            # first→last weigh-in in the window
MAX_STEP = 150                # cal per cycle
DAMP = 0.7                    # weight on the new estimate vs the current maintenance
KCAL_PER_LB = 3500
RECOMP_FLAT_LB_PER_WEEK = 0.5 # recomp: nudge back toward flat past this
FLOOR = 1400
WEIGH_IN_NUDGE_DAYS = 7


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tz(user):
    try:
        return ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:  # noqa: BLE001
        return ZoneInfo("America/Los_Angeles")


def ewma(points: list[tuple[datetime, float]], alpha: float = EWMA_ALPHA) -> list[tuple[datetime, float]]:
    """Exponentially weighted trend over (when, lbs) sorted ascending. The first
    point seeds the average. Returns the same timestamps with trend values."""
    out, cur = [], None
    for when, w in sorted(points, key=lambda p: p[0]):
        cur = w if cur is None else cur + alpha * (w - cur)
        out.append((when, round(cur, 2)))
    return out


def weight_series(session, user, since: datetime | None = None) -> list[tuple[datetime, float]]:
    q = session.query(WeightLog).filter(WeightLog.user_id == user.id)
    if since is not None:
        q = q.filter(WeightLog.weighed_at >= since)
    return [(w.weighed_at, float(w.weight_lbs)) for w in q.order_by(WeightLog.weighed_at.asc()).all()
            if w.weighed_at and w.weight_lbs]


def intake_by_local_day(session, user, start_utc: datetime, end_utc: datetime) -> dict:
    """{local_date: {"meals": n, "kcal": total}} for ACTIVE meals in [start, end)."""
    tz = _tz(user)
    days: dict = {}
    for m in (active(session, Meal, user_id=user.id)
              .filter(Meal.eaten_at >= start_utc, Meal.eaten_at < end_utc).all()):
        if not m.eaten_at:
            continue
        d = m.eaten_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
        rec = days.setdefault(d, {"meals": 0, "kcal": 0})
        rec["meals"] += 1
        rec["kcal"] += int(m.calories or 0)
    return days


def evaluate(user, session, now: datetime | None = None) -> dict:
    """Pure decision for one cycle. No writes. Returns
      {"change": bool, "old": int, "new": int, "reason": str, "counted_days": int,
       "weighins": int, "trend_delta_lbs": float|None, "est_expenditure": int|None,
       "trend_now": float|None}"""
    now = now or _utcnow()
    start = now - timedelta(days=WINDOW_DAYS)
    old = int(user.calorie_target or 0)
    base = {"change": False, "old": old, "new": old, "counted_days": 0, "weighins": 0,
            "trend_delta_lbs": None, "est_expenditure": None, "trend_now": None}
    if not old:
        return dict(base, reason="no target set yet")

    # ── intake completeness gate ──
    days = intake_by_local_day(session, user, start, now)
    counted = {d: r for d, r in days.items() if r["meals"] >= MEALS_PER_COUNTED_DAY}
    base["counted_days"] = len(counted)
    if len(counted) < MIN_COUNTED_DAYS:
        return dict(base, reason=(f"only {len(counted)} of the last {WINDOW_DAYS} days had "
                                  f"{MEALS_PER_COUNTED_DAY}+ meals logged (need {MIN_COUNTED_DAYS})"))
    mean_intake = sum(r["kcal"] for r in counted.values()) / len(counted)

    # ── weight gate ──
    pts = weight_series(session, user, since=start)
    base["weighins"] = len(pts)
    if len(pts) < MIN_WEIGHINS:
        return dict(base, reason=f"only {len(pts)} weigh-ins in the last {WINDOW_DAYS} days (need {MIN_WEIGHINS})")
    span = (pts[-1][0] - pts[0][0]).days
    if span < MIN_SPAN_DAYS:
        return dict(base, reason=f"weigh-ins span only {span} days (need {MIN_SPAN_DAYS})")

    # Seed the trend with everything before the window so the first in-window
    # point isn't a raw reading; then read the trend at the window's ends.
    all_pts = weight_series(session, user)
    trend = ewma(all_pts)
    in_win = [(t, v) for t, v in trend if t >= start]
    t0, t1 = in_win[0], in_win[-1]
    delta_lbs = round(t1[1] - t0[1], 2)
    days_between = max((t1[0] - t0[0]).days, 1)
    base.update(trend_delta_lbs=delta_lbs, trend_now=t1[1])

    # ── expenditure estimate ──
    est = int(round(mean_intake - (delta_lbs * KCAL_PER_LB) / days_between))
    base["est_expenditure"] = est

    # ── current maintenance (what the goal rule was applied to) ──
    # A maintenance they reported from their own tracking (users.reported_maintenance,
    # bounded on the way in) is the better prior than the equation when present.
    computed = calculate_targets(user)
    cur_maint = int(getattr(user, "reported_maintenance", None) or computed["tdee"])
    new_maint = int(round(DAMP * est + (1 - DAMP) * cur_maint))
    proposed = apply_goal(new_maint, user.goal or "", **goal_profile(user))["calories"]

    # ── direction check against the goal ──
    goal = user.goal or ""
    per_week = delta_lbs / days_between * 7
    recomp = "fat_loss" in goal and "muscle" in goal
    cutting = "fat_loss" in goal and not recomp
    bulking = ("muscle" in goal or "strength" in goal) and not recomp
    if cutting and per_week <= -0.5:
        return dict(base, reason=f"cut is working (trend {per_week:+.1f} lb/wk) — leaving {old}")
    if bulking and 0.25 <= per_week <= 1.0:
        return dict(base, reason=f"bulk is on pace (trend {per_week:+.1f} lb/wk) — leaving {old}")
    if recomp and abs(per_week) <= RECOMP_FLAT_LB_PER_WEEK:
        return dict(base, reason=f"recomp holding flat (trend {per_week:+.1f} lb/wk) — leaving {old}")

    # ── clamp: ±MAX_STEP per cycle, and to a sane band around the base rate ──
    step = max(-MAX_STEP, min(MAX_STEP, proposed - old))
    new = int(round((old + step) / 50.0) * 50)
    bmr = int(computed["bmr"])
    new = max(FLOOR, max(int(bmr * 1.1), min(int(bmr * 2.2), new)))
    if new == old:
        return dict(base, reason=f"estimate {est} cal vs maintenance {cur_maint} — within noise, leaving {old}")
    direction = "down" if new < old else "up"
    why = (f"trend {per_week:+.1f} lb/wk on a {'cut' if cutting else 'bulk' if bulking else 'recomp'}, "
           f"est. expenditure {est} cal from {len(counted)} logged days → {direction} {abs(new - old)}")
    return dict(base, change=True, new=new, reason=why)


def _due(user, session, now: datetime) -> bool:
    if (user.onboarding_step or 0) < 3 or not user.calorie_target:
        return False
    last = (session.query(TargetAdjustment).filter(TargetAdjustment.user_id == user.id)
            .order_by(TargetAdjustment.at.desc()).first())
    anchor = last.at if last else (user.created_at or now)
    return (now - anchor).days >= CYCLE_DAYS


def apply_cycle(user_id: int, now: datetime | None = None, force: bool = False) -> dict | None:
    """Run one cycle for a user if due (or forced). Writes a target_adjustments row
    every time it runs — changed or not — so the coach can explain it once."""
    now = now or _utcnow()
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or (not force and not _due(user, session, now)):
            return None
        r = evaluate(user, session, now)
        row = TargetAdjustment(user_id=user.id, at=now, old_target=r["old"], new_target=r["new"],
                               changed=bool(r["change"]), reason=r["reason"][:300],
                               est_expenditure=r["est_expenditure"], trend_delta_lbs=r["trend_delta_lbs"],
                               counted_days=r["counted_days"], weighins=r["weighins"])
        session.add(row)
        if r["change"]:
            user.calorie_target = r["new"]
            user.targets_source = "adaptive"
        session.commit()
        logger.info("TARGET_CYCLE user=%s changed=%s %s→%s reason=%s", user_id, r["change"], r["old"], r["new"], r["reason"])
        return r
    finally:
        session.close()


def run_all() -> int:
    """Scheduler sweep (daily): every completed user due for a cycle."""
    session = get_session()
    try:
        ids = [u.id for u in session.query(User).filter(User.onboarding_step >= 3).all()]
    finally:
        session.close()
    n = 0
    for uid in ids:
        try:
            if apply_cycle(uid) is not None:
                n += 1
        except Exception as e:  # noqa: BLE001 — one user must not stop the sweep
            logger.error("TARGET_CYCLE_FAILED user=%s err=%s", uid, e)
    return n


# ─── context for the coach + heartbeat ──────────────────────────────────────

def weight_context(user, session) -> str:
    pts = weight_series(session, user)
    if not pts:
        return ""
    trend = ewma(pts)
    latest_when, latest_raw = pts[-1]
    _, latest_trend = trend[-1]
    week_ago = latest_when - timedelta(days=7)
    older = [v for t, v in trend if t <= week_ago]
    line = (f"## WEIGHT\nlatest weigh-in {latest_raw:g} lb ({latest_when:%b %d}); trend {latest_trend:g} lb")
    if older:
        line += f" ({latest_trend - older[-1]:+.1f} over the last week)"
    line += (". Quote the TREND, never a single reading; a one-day jump is water. "
             f"{len(pts)} weigh-ins on file.")
    return line


def todays_adjustment_context(user, session) -> str:
    tz = _tz(user)
    day_start = (datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(timezone.utc).replace(tzinfo=None))
    row = (session.query(TargetAdjustment)
           .filter(TargetAdjustment.user_id == user.id, TargetAdjustment.at >= day_start)
           .order_by(TargetAdjustment.at.desc()).first())
    if not row:
        return ""
    if row.changed and (row.reason or "").startswith("calculator update"):
        return (f"## TARGET CHANGED TODAY (a fix on our side, not the scale)\n"
                f"{row.old_target} → {row.new_target} cal. Detail: {row.reason}. "
                "Tell them ONCE, plainly and in your words, that you re-ran their numbers with a "
                "better formula and this is where they land now — say the new calorie AND protein "
                "targets, and that the old protein number was too high for the budget. Own it (\"my "
                "numbers were off\"), no apology spiral, no lecture. If they push back, set_targets "
                "still allows their pick within 15%. Never call it a diet, deficit or cut with a minor.")
    if row.changed:
        return (f"## TARGET CHANGED TODAY\n{row.old_target} → {row.new_target} cal. Reason: {row.reason}. "
                "Mention it ONCE, in your words, no lecture — it's the scale talking, not you. If they "
                "push back, hold the number and explain; set_targets still allows their pick within 15%.")
    return (f"## TARGET CHECK TODAY (no change)\nStayed at {row.old_target}. Why: {row.reason}. "
            "If it's a logging gap, say so once, plainly, and what would fix it. Don't nag.")


def weigh_in_condition(user, session, now: datetime | None = None) -> str:
    """Heartbeat standing condition: a weigh-in is due and hasn't been nudged this
    week. Empty when opted out, mid-onboarding, or recently weighed/nudged."""
    now = now or _utcnow()
    if (user.onboarding_step or 0) < 3 or getattr(user, "weigh_in_opt_out", False):
        return ""
    last = (session.query(WeightLog).filter(WeightLog.user_id == user.id)
            .order_by(WeightLog.weighed_at.desc()).first())
    if last and last.weighed_at and (now - last.weighed_at).days < WEIGH_IN_NUDGE_DAYS:
        return ""
    from models import Message
    recent_nudge = (session.query(Message.id)
                    .filter(Message.user_id == user.id, Message.direction == "out",
                            Message.created_at >= now - timedelta(days=WEIGH_IN_NUDGE_DAYS),
                            Message.body.ilike("%scale%")).first())
    if recent_nudge:
        return ""
    ago = f"{(now - last.weighed_at).days} days ago" if last and last.weighed_at else "never"
    return ("## WEIGH-IN DUE (standing condition)\n"
            f"Last weigh-in: {ago}. Worth ONE line, in the morning, once this week: ask them to hop "
            "on the scale — same time, before food — and you'll do the math. If they say they don't "
            "own a scale, log_weight with no_scale=true and never ask again.")
