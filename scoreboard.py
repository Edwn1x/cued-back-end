"""
Scoreboard (scoreboard.py): the agenda the coach owns for this person, computed by code,
led from by the model.

The 11-thread audit (2026-11-21) found the same absence everywhere: nobody holds "lifts this
week against the plan, the day type that keeps slipping, the weigh-in asked three times and
never answered, seven days of intake against the target, the promises the coach made". Every
turn was reactive — a meal arrives and is logged, a bail arrives and is accepted, a heartbeat
fires from a template. The founder's own 30 days: five lifts, zero leg days, nine days at
2900+ cal, three scale asks unanswered, and the coach said "light dinner or just let today be a
high day". Alex asked "have I been losing weight?" two months in and no weigh-in had ever
been requested. Andrew was told to see a doctor and log everything for a week; neither was
followed up. Alexey's thumb put pull "on hold pending a rework" in memory for 17 days.

This block renders into every prompt that speaks as the coach (the reply loop and, through
build_loop_context, the heartbeat). Code does the arithmetic and the lookups; the LEAD line
names the one standing condition a coach with a stake would speak to. The model decides the
words and the moment.

Flag SCOREBOARD_ENABLED (default on). Fail-open: any error → no block.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.scoreboard")

# Outbound lines that are the coach asking for a weigh-in.
_WEIGH_ASK_RE = re.compile(
    r"\b(hop on the scale|step on (?:a|the) scale|on the scale|weigh[- ]?in|weigh yourself|scale number|"
    r"what(?:'s| is) the scale|send me (?:ur|your) weight|weight check)\b", re.I)
# Lines where the coach committed to something or asked them to report back.
_PROMISE_RE = re.compile(
    r"\b(i'?ll (?:send|text|check|look|find|ping|set|have|get back|follow up|pull|put|add|build|fix)\b[^.!?\n]{0,60}"
    r"|text me (?:when|after|once|how)\b[^.!?\n]{0,50}"
    r"|lmk (?:when|how|if)\b[^.!?\n]{0,50}"
    r"|get (?:it|that|the \w+) (?:checked|looked at)\b"
    r"|see a doctor\b|urgent care\b|tang center\b"
    r"|send me (?:ur|your) (?:food|day|meals|sets|numbers)\b[^.!?\n]{0,40})", re.I)
# Reminder / sticker / card plumbing rows are not the coach talking.
_NOT_COACH_TYPES = ("reminder", "water_reminder", "reaction", "workout_card", "stat_card", "connect_link",
                    "reminder_checkin", "outage_note", "gym_dead", "gym_line_d1", "gym_line_d2", "gym_optin_ask",
                    "gym_summon", "water_offer", "connect_offer", "onboarding_nudge", "onboarding_nudge_more")
_DAY_SLIP_DAYS = 14


def enabled() -> bool:
    return bool(getattr(config, "SCOREBOARD_ENABLED", True))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local(user, dt: datetime):
    from timefmt import to_local
    return to_local(dt, user)


def _week_start_utc(user, now: datetime) -> datetime:
    """Local Monday 00:00 of the current week, as naive UTC."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(getattr(user, "user_timezone", None) or "America/Los_Angeles")
    local = now.replace(tzinfo=timezone.utc).astimezone(tz)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(timezone.utc).replace(tzinfo=None)


def _plan_per_week(user) -> int:
    """The larger of the program's day count and the frequency they committed to."""
    n = 0
    prog = getattr(user, "program", None)
    if isinstance(prog, dict) and prog.get("days"):
        n = len(prog["days"])
    try:
        from heartbeat import _committed_per_week
        n = max(n, int(_committed_per_week(user)))
    except Exception:  # noqa: BLE001
        pass
    return n or 3


def _cycle(user) -> list[str]:
    prog = getattr(user, "program", None)
    if isinstance(prog, dict) and prog.get("days"):
        seen: list[str] = []
        for k in prog["days"].values():
            if k and k not in seen:
                seen.append(k)
        return seen
    try:
        from split_pointer import cycle_for
        from workouts.templates import normalize_template_key
        return [k for k in (normalize_template_key(d) for d in cycle_for(user)) if k]
    except Exception:  # noqa: BLE001
        return []


def _done_sessions(session, user, since: datetime | None = None) -> list[tuple[datetime, str | None]]:
    """[(when_utc, template_key)] of completed training — card sessions and legacy Workout rows,
    newest first, one per local day."""
    from models import WorkoutSession, Workout, active
    rows: list[tuple[datetime, str | None]] = []
    q = session.query(WorkoutSession).filter(WorkoutSession.user_id == user.id, WorkoutSession.status == "done")
    if since is not None:
        q = q.filter(WorkoutSession.date >= since - timedelta(days=1))
    for ws in q.all():
        when = ws.finished_at or ws.date
        if when and (since is None or when >= since):
            rows.append((when, ws.template_key))
    wq = active(session, Workout, user_id=user.id).filter(Workout.completed.is_(True))
    if since is not None:
        wq = wq.filter(Workout.date >= since)
    for w in wq.all():
        if w.date:
            rows.append((w.date, getattr(w, "split_day", None) or getattr(w, "workout_type", None)))
    rows.sort(key=lambda r: r[0], reverse=True)
    out: list[tuple[datetime, str | None]] = []
    days_seen: set = set()
    for when, key in rows:
        d = _local(user, when).date()
        if d in days_seen:
            continue
        days_seen.add(d)
        out.append((when, key))
    return out


def compute(user, session, now: datetime | None = None) -> dict:
    now = now or _now()
    from models import Message, WeightLog
    from adaptive_targets import intake_by_local_day
    out: dict = {"now": now}

    # ── training this week vs the plan, and the day that keeps slipping ──
    week0 = _week_start_utc(user, now)
    done_week = _done_sessions(session, user, since=week0)
    out["plan_per_week"] = _plan_per_week(user)
    out["done_this_week"] = len(done_week)
    out["weekday_index"] = _local(user, now).weekday()           # 0 = Monday
    all_done = _done_sessions(session, user)
    if all_done:
        last_when, last_key = all_done[0]
        out["last_session_days"] = (now - last_when).days
        out["last_session_key"] = last_key
    else:
        start = getattr(user, "activated_at", None) or getattr(user, "created_at", None)
        out["last_session_days"] = (now - start).days if start else None
        out["last_session_key"] = None
    out["never_trained"] = not all_done
    cycle = _cycle(user)
    slipping: list[tuple[str, int | None]] = []
    # A day type is "slipping" only while OTHER days of the cycle keep happening — a month with
    # no lifts at all is the training gap, not three slipping days.
    others_recent = any(k in cycle and (now - w).days < _DAY_SLIP_DAYS for w, k in all_done)
    # A day NEVER done only counts once they've been training with you for a full slip window —
    # a first-week lifter with one push day is not "slipping" on pull and legs.
    history_days = (now - all_done[-1][0]).days if all_done else 0
    if cycle and len(cycle) >= 2 and others_recent:
        for key in cycle:
            last = next((w for w, k in all_done if k == key), None)
            days = (now - last).days if last else None
            if (days is None and history_days >= _DAY_SLIP_DAYS) or (days is not None and days >= _DAY_SLIP_DAYS):
                slipping.append((key, days))
    out["cycle"] = cycle
    out["slipping"] = slipping

    # ── the weigh-in ──
    last_w = (session.query(WeightLog).filter(WeightLog.user_id == user.id)
              .order_by(WeightLog.weighed_at.desc()).first())
    out["weigh_in_days"] = (now - last_w.weighed_at).days if last_w and last_w.weighed_at else None
    out["weigh_in_lbs"] = float(last_w.weight_lbs) if last_w else None
    since_w = last_w.weighed_at if last_w and last_w.weighed_at else (getattr(user, "onboarding_completed_at", None) or week0 - timedelta(days=60))
    asks = (session.query(Message.created_at, Message.body)
            .filter(Message.user_id == user.id, Message.direction == "out", Message.created_at > since_w,
                    Message.message_type.notin_(_NOT_COACH_TYPES))
            .order_by(Message.created_at.asc()).all())
    ask_rows = [(t, b) for t, b in asks if b and _WEIGH_ASK_RE.search(b)]
    out["weigh_asks"] = len(ask_rows)
    out["weigh_ask_last_days"] = (now - ask_rows[-1][0]).days if ask_rows else None

    # ── seven days of food against the target ──
    start7 = week0 - timedelta(days=0)  # placeholder; replaced below with a rolling 7 full days
    from timefmt import local_day_bounds
    day0, _day1 = local_day_bounds(user, now=now)
    start7 = day0 - timedelta(days=7)
    days = intake_by_local_day(session, user, start7, day0)
    target = getattr(user, "calorie_target", None) or 0
    logged = [d for d in days.values() if (d.get("meals") or 0) > 0]
    kcals = [d.get("kcal") or 0 for d in logged]
    out["food_days_logged"] = len(logged)
    out["food_avg_kcal"] = int(sum(kcals) / len(kcals)) if kcals else None
    out["food_days_over"] = sum(1 for k in kcals if target and k > target * 1.1)
    out["food_days_under"] = sum(1 for k in kcals if target and k < target * 0.75)
    out["calorie_target"] = target or None

    # ── what the coach left open ──
    recent_out = (session.query(Message)
                  .filter(Message.user_id == user.id, Message.direction == "out",
                          Message.created_at >= now - timedelta(hours=72),
                          Message.message_type.notin_(_NOT_COACH_TYPES))
                  .order_by(Message.created_at.desc()).limit(40).all())
    last_in = (session.query(Message.created_at).filter(Message.user_id == user.id, Message.direction == "in")
               .order_by(Message.created_at.desc()).first())
    last_in_at = last_in[0] if last_in and last_in[0] else None
    out["open_question"] = None
    if recent_out:
        newest = recent_out[0]
        if newest.body and newest.body.rstrip().endswith("?") and (last_in_at is None or last_in_at < newest.created_at):
            out["open_question"] = ((now - newest.created_at).total_seconds() / 3600, newest.body.strip()[:140])
    promises = []
    seen_p: set = set()
    for m in recent_out:
        if not m.body:
            continue
        hit = _PROMISE_RE.search(m.body)
        if hit and "i'll log" not in hit.group(0).lower():       # "lmk when u eat it and i'll log it" is a standing offer, not a promise
            text = hit.group(0).strip()[:90]
            norm = re.sub(r"\W+", " ", text.lower()).strip()
            if norm in seen_p:
                continue
            seen_p.add(norm)
            promises.append(((now - m.created_at).total_seconds() / 3600, text))
    out["promises"] = promises[:3]

    out["lead"] = _lead(out)
    return out


def _lead(sb: dict) -> str | None:
    """The one standing condition to speak to, by priority. None = nothing owed right now."""
    slipping = sb.get("slipping") or []
    if sb.get("weigh_asks", 0) >= 1 and (sb.get("weigh_in_days") is None or sb["weigh_in_days"] >= 7):
        n = sb["weigh_asks"]
        return (f"the weigh-in: asked {n}× and never answered — a fat-loss or recomp plan has no instrument "
                f"without it. Close it today (same time, before eating), don't ask a fourth time the same way.")
    if slipping:
        key, days = slipping[0]
        when = "never" if days is None else f"{days} days"
        return (f"{key} keeps slipping ({when} since the last one) while other days happen — that day is the "
                f"conversation now: name it, hold it, don't offer it as optional.")
    if (not sb.get("never_trained") and sb.get("plan_per_week", 0) >= 2 and sb.get("done_this_week", 0) == 0
            and sb.get("weekday_index", 0) >= 3):
        return (f"the week's at zero against {sb['plan_per_week']}/week and it's already "
                f"{['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][sb['weekday_index']]} — one session this week is "
                f"the ask, concretely (which day, what time), not 'wanna'.")
    for age_h, text in sb.get("promises") or []:
        if age_h >= 24:
            return f"you left this open {int(age_h)}h ago and never came back to it: \"{text}\" — follow through or close it."
    oq = sb.get("open_question")
    if oq and oq[0] >= 24:
        return f"your question from {int(oq[0])}h ago is unanswered: \"{oq[1]}\" — it's closed; don't re-ask it, move to the next real thing."
    return None


def render(sb: dict, user) -> str:
    name = (getattr(user, "name", None) or "them").split()[0]
    lines = [f"## SCOREBOARD — what you own for {name} this week (code-computed; lead from it, never recite it)"]
    wk = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][sb.get("weekday_index", 0)]
    if sb.get("never_trained"):
        d = sb.get("last_session_days")
        lines.append(f"- training: {sb['done_this_week']}/{sb['plan_per_week']} this week (it's {wk}); "
                     f"NO completed session on record" + (f" — {d} days since they joined" if d is not None else ""))
    else:
        lines.append(f"- training: {sb['done_this_week']}/{sb['plan_per_week']} this week (it's {wk}); last session "
                     f"{sb['last_session_days']} days ago" + (f" ({sb['last_session_key']})" if sb.get("last_session_key") else ""))
    for key, days in sb.get("slipping") or []:
        lines.append(f"- {key}: " + ("never done" if days is None else f"{days} days since the last one") + " — slipping")
    if sb.get("weigh_in_days") is None:
        lines.append("- weigh-in: no number yet" + (f"; asked {sb['weigh_asks']}× (last {sb['weigh_ask_last_days']}d ago), never answered"
                                                     if sb.get("weigh_asks") else " — never asked"))
    else:
        lines.append(f"- weigh-in: {sb['weigh_in_lbs']:g} lb, {sb['weigh_in_days']} days ago"
                     + (f"; asked {sb['weigh_asks']}× since, no new number" if sb.get("weigh_asks") else ""))
    if sb.get("calorie_target"):
        if sb.get("food_days_logged"):
            lines.append(f"- food, last 7 days: {sb['food_days_logged']} days logged, avg {sb['food_avg_kcal']} vs "
                         f"{sb['calorie_target']} target, {sb['food_days_over']} days >10% over, "
                         f"{sb['food_days_under']} days <75%")
        else:
            lines.append(f"- food, last 7 days: nothing logged against a {sb['calorie_target']} target")
    oq = sb.get("open_question")
    if oq:
        lines.append(f"- your open question ({int(oq[0])}h, unanswered): \"{oq[1]}\"")
    for age_h, text in sb.get("promises") or []:
        lines.append(f"- you said ({int(age_h)}h ago): \"{text}\" — verify it was done before repeating or promising again")
    if sb.get("lead"):
        lines.append(f"LEAD: {sb['lead']}")
    else:
        lines.append("LEAD: nothing owed right now — react to what's in front of you.")
    return "\n".join(lines)


def context_block(user, session, *, now: datetime | None = None) -> str | None:
    """The block, or None (off / not onboarded / failure)."""
    if not enabled():
        return None
    if (getattr(user, "onboarding_step", 0) or 0) < 3:
        return None
    try:
        return render(compute(user, session, now), user)
    except Exception as e:  # noqa: BLE001 — never break a turn over the scoreboard
        logger.warning("SCOREBOARD_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return None
