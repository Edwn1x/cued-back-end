"""
Per-user profile page — the link the coach texts each user, and the read-only
payload behind it.

The page lives on the marketing site (config.PROFILE_BASE_URL, cued.fit/profile.html)
and fetches its data from GET /profile/<token> on this service. The token is
STATELESS: an HMAC of the user id under PROFILE_TOKEN_SECRET (falls back to
FLASK_SECRET_KEY), so no column, no migration, and the same link is reproducible
from code anywhere it's needed (kickoff SMS, coach context, admin page). Rotating
the secret invalidates every link at once — the only revocation lever, by design.

The old link carried the raw phone number (?phone=1209...). That was fine while
the page rendered nothing, but a page that serves real data can't be keyed by
something a stranger can guess or enumerate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
from memory import CATEGORIES

logger = logging.getLogger(__name__)

_TOKEN_BYTES = 18          # 144-bit MAC prefix → 24 url-safe chars, no padding
_MAX_MEMORY_PER_CATEGORY = 40
_RECENT_MEALS = 15
_RECENT_WORKOUTS = 60         # the "my workouts" picker on the training tab
_RECENT_WEIGHTS = 40          # enough for a bodyweight line
_PROGRESS_DAYS = 120          # window for the lift-progress + consistency charts
_PROGRESS_WEEKS = 12
_MAX_LIFTS = 8


# ─── token ───────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    return (config.PROFILE_TOKEN_SECRET or config.FLASK_SECRET_KEY).encode("utf-8")


def _mac(user_id: int) -> str:
    digest = hmac.new(_secret(), f"profile:{user_id}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def profile_token(user_id: int) -> str:
    """'<id>.<mac>' — the id is public-ish; the mac is what makes it theirs."""
    return f"{int(user_id)}.{_mac(int(user_id))}"


def verify_profile_token(token: str | None) -> int | None:
    """The user id the token was minted for, or None. Constant-time compare on the MAC."""
    if not token or "." not in token:
        return None
    id_part, _, mac_part = token.partition(".")
    if not id_part.isdigit() or len(id_part) > 12 or not mac_part:
        return None
    user_id = int(id_part)
    if not hmac.compare_digest(mac_part, _mac(user_id)):
        return None
    return user_id


def profile_url(user) -> str:
    """The one link the coach may always send. Same shape everywhere it's built."""
    return f"{config.PROFILE_BASE_URL}?t={profile_token(user.id)}"


# ─── payload ─────────────────────────────────────────────────────────────────

def _iso(dt: datetime | None) -> str | None:
    """Stored datetimes are naive UTC; emit aware ISO so the page can localize."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _csv(value: str | None) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _str(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _num(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 1)


def _memory_block(user) -> dict:
    """Valid entries only — invalidated ones already live under __history__, so the
    category lists are the live truth. Oldest first within a category, so the page
    reads like a thread: newest at the bottom."""
    profile = user.user_profile_memory or {}
    out = {}
    for cat in CATEGORIES:
        entries = [e for e in (profile.get(cat) or []) if isinstance(e, dict) and e.get("text")]
        entries.sort(key=lambda e: e.get("ts") or "")
        if entries:
            out[cat] = [
                {"text": e["text"], "since": e.get("ts"), "safety": bool(e.get("safety"))}
                for e in entries[:_MAX_MEMORY_PER_CATEGORY]
            ]
    return out


_WS_RE = re.compile(r"\s+")


def _lift_key(name) -> str | None:
    """Exercise names are free text from the model ("Bench", "bench press ")."""
    n = _WS_RE.sub(" ", str(name or "").strip().lower())
    return n or None


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def build_training_progress(workouts, user, *, now: datetime) -> dict:
    """Two small series the training tab leads with, both computed here so the
    page stays a renderer and the logic is testable:

    lifts   — per exercise (name lower-cased + whitespace-folded), one point per LOCAL day: the top
              weight lifted, the reps at that weight, and an Epley estimated
              1RM. Only exercises with a numeric weight count; sorted by how
              often they were trained, capped at _MAX_LIFTS.
    weekly  — workouts per local week (Mon-start) for the last _PROGRESS_WEEKS
              weeks, oldest first, zero-filled — the consistency strip.
    """
    from timefmt import to_local

    per_lift: dict[str, dict[str, dict]] = defaultdict(dict)   # key -> local_date -> point
    weekly: dict[str, int] = defaultdict(int)
    cutoff = now - timedelta(days=_PROGRESS_DAYS)
    for w in workouts:
        if w.date is None:
            continue
        when = w.date.replace(tzinfo=timezone.utc) if w.date.tzinfo is None else w.date
        if when < cutoff:
            continue
        local = to_local(w.date, user)
        day = local.date().isoformat()
        week_start = (local.date() - timedelta(days=local.weekday())).isoformat()
        weekly[week_start] += 1
        for e in (w.exercises or []):
            if not isinstance(e, dict):
                continue
            key = _lift_key(e.get("name"))
            weight = _float(e.get("weight"))
            if not key or weight is None:
                continue
            reps = e.get("reps")
            reps = int(reps) if isinstance(reps, (int, float)) and reps > 0 else None
            e1rm = round(weight * (1 + reps / 30.0), 1) if reps else weight
            pt = per_lift[key].get(day)
            if pt is None or weight > pt["top_weight"] or (weight == pt["top_weight"] and (reps or 0) > (pt["reps"] or 0)):
                per_lift[key][day] = {"date": day, "top_weight": _num(weight), "reps": reps, "e1rm": _num(e1rm)}

    lifts = []
    for key, days in per_lift.items():
        sessions = [days[d] for d in sorted(days)]
        best = max(sessions, key=lambda s: (s["top_weight"], s["reps"] or 0))
        lifts.append({"name": key, "sessions": sessions, "best": best})
    # most-trained first; ties go to the heavier lift so bench/squat/deadlift lead
    lifts.sort(key=lambda l: (-len(l["sessions"]), -(l["best"]["top_weight"] or 0), l["name"]))

    this_monday = (to_local(now, user).date() - timedelta(days=to_local(now, user).weekday()))
    weeks = []
    for i in range(_PROGRESS_WEEKS - 1, -1, -1):
        ws = (this_monday - timedelta(weeks=i)).isoformat()
        weeks.append({"week_start": ws, "workouts": weekly.get(ws, 0)})

    return {"lifts": lifts[:_MAX_LIFTS], "weekly": weeks}


def build_profile_payload(session, user, *, now: datetime | None = None) -> dict:
    """Everything the user is entitled to see about themselves, shaped for the page.

    Typed columns are the source of truth for the profile facts (same rule as the
    prompt builders); user_profile_memory is what the coach has accumulated;
    the recent-log tails come straight from the soft-delete-filtered tables.
    """
    from models import Meal, Workout, WeightLog, active
    from timefmt import local_day_bounds, to_local

    now = now or datetime.now(timezone.utc)
    day_start, day_end = local_day_bounds(user, now=now)

    meals_q = active(session, Meal, user.id)
    todays_meals = (meals_q.filter(Meal.eaten_at >= day_start, Meal.eaten_at < day_end)
                    .order_by(Meal.eaten_at.asc()).all())
    recent_meals = meals_q.order_by(Meal.eaten_at.desc()).limit(_RECENT_MEALS).all()
    recent_workouts = (active(session, Workout, user.id)
                       .order_by(Workout.date.desc()).limit(_RECENT_WORKOUTS).all())
    progress = build_training_progress(recent_workouts, user, now=now)
    local_today = to_local(now, user).date().isoformat()
    recent_weights = (session.query(WeightLog).filter(WeightLog.user_id == user.id)
                      .order_by(WeightLog.weighed_at.desc()).limit(_RECENT_WEIGHTS).all())

    height = None
    if user.height_ft is not None:
        height = {"ft": int(user.height_ft), "in": int(user.height_in or 0)}

    return {
        "name": user.name,
        "first_name": (user.name or "").split(" ")[0],
        "phone": user.phone,
        "timezone": user.user_timezone or "America/Los_Angeles",
        "member_since": _iso(user.created_at),
        "onboarding_complete": (user.onboarding_step or 0) >= 3,
        "coaching_branch": _str(user.coaching_branch),
        "about": {
            "age": user.age,
            "gender": _str(user.gender),
            "occupation": _str(user.occupation),
            "year": _str(user.year),
            "height": height,
            "weight_lbs": _num(user.weight_lbs),
            "body_fat_pct": _num(user.body_fat_pct),
        },
        "goals": {
            "goals": _csv(user.goal),
            "goal_other": _str(user.goal_other),
            "priority": _str(user.confirmed_goal_priority),
            "obstacle": _str(user.biggest_obstacle),
            "motivation": _str(user.motivation),
        },
        "targets": {
            "calories": user.calorie_target,
            "protein_g": user.protein_target,
        },
        "today": {
            "date": day_start and _iso(day_start),
            "local_date": local_today,
            "calories": sum(m.calories or 0 for m in todays_meals),
            "protein_g": sum(m.protein_g or 0 for m in todays_meals),
            "carbs_g": sum(m.carbs_g or 0 for m in todays_meals),
            "fat_g": sum(m.fat_g or 0 for m in todays_meals),
            "meals_logged": len(todays_meals),
        },
        "training": {
            "experience": _str(user.experience),
            "equipment": _str(user.equipment),
            "gym": _str(user.which_gym),
            "split": _str(user.confirmed_training_split) or _str(user.current_split),
            "days": _csv(user.confirmed_training_days or user.workout_days),
            "time": _str(user.confirmed_workout_time) or _str(user.workout_time),
            "injuries": _str(user.injuries),
            "activity_level": _str(user.activity_level),
            "avg_steps": user.avg_steps,
            "progress": progress,
        },
        "nutrition": {
            "diet": _str(user.diet),
            "restrictions": _str(user.restrictions),
            "cooking_situation": _str(user.cooking_situation),
            "meal_plan": _str(user.meal_plan_status),
            "meals_per_day": _str(user.meals_per_day),
            "food_context": _str(user.food_context),
        },
        "routine": {
            "wake_time": _str(user.wake_time),
            "sleep_time": _str(user.sleep_time),
            "sleep_quality": _str(user.sleep_quality),
            "stress_level": _str(user.stress_level),
            "wearable": _str(user.wearable),
            "weigh_in_day": _str(user.weigh_in_day),
            "schedule_details": _str(user.schedule_details),
        },
        "memory": _memory_block(user),
        "recent": {
            "meals": [{
                "id": m.id,
                "eaten_at": _iso(m.eaten_at),
                "description": m.description,
                "calories": m.calories,
                "protein_g": m.protein_g,
                "carbs_g": m.carbs_g,
                "fat_g": m.fat_g,
                "source": m.source,
            } for m in recent_meals],
            "workouts": [{
                "id": w.id,
                "date": _iso(w.date),
                "local_date": to_local(w.date, user).date().isoformat() if w.date else None,
                "type": _str(w.workout_type),
                "exercises": [
                    {"name": e.get("name"), "sets": e.get("sets"), "reps": e.get("reps"),
                     "weight": e.get("weight")}
                    for e in (w.exercises or []) if isinstance(e, dict)
                ],
                "notes": _str(w.user_notes),
                "completed": bool(w.completed),
            } for w in recent_workouts],
            "weights": [{
                "weighed_at": _iso(x.weighed_at),
                "weight_lbs": _num(x.weight_lbs),
                "notes": _str(x.notes),
            } for x in recent_weights],
        },
    }
