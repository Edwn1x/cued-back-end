"""Engaged-day retention for the admin console (founder, 2026-10-27).

The legacy retention rows count ANY inbound text inside a window — a 👍, an "ok" to a
nudge, a 1–5 rating, even STOP — and miss the people who train off the card and never
type. This section measures what the product is for and renders it NEXT TO the legacy
rows, same eligible set, so the gap between the columns is the finding.

Signals, per user, bucketed by the user's LOCAL day:
  core      they logged something: a meal (meals.logged_at, not deleted), a done set
            (set_logs.done_at, any source), a weigh-in (weight_logs.weighed_at)
  pull      they came to us unprompted: an inbound text with none of our outbounds in the
            UNPROMPTED_GAP_MIN before it, or a card open / tapback / stat-card view
            (activity_days — tracked from the day that table shipped; earlier card-only
            days are not recoverable, see `presence_since`)
  reactive  an inbound inside the gap after our outbound — they answered a ping. Reported
            as the nudge reply rate and NEVER an engaged day on its own.
  engaged   core or pull

What the page shows:
  windows   last 7 / 14 / 30 days — texted (the legacy definition) vs engaged vs core,
            same eligible set and denominators as the legacy rows
  cohorts   users activated the same ISO week (activated_at, else created_at): the share
            with an engaged day in week 0 (first 7 days), 1, 2 and 4 after activation.
            A week is only measurable once it has fully elapsed for that user.
  depth     median engaged days per week over the last 28 days, among users with any
  pull      pull days ÷ engaged days over the last 28 days
  nudges    proactive outbounds (not themselves a reply) answered within REPLY_WINDOW_MIN

Pure read; every input is the row set the dashboard already loads.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func

from activity_days import PRESENCE_SIGNALS, local_day
from models import ActivityDay, Meal, SetLog, WeightLog, WorkoutSession

UNPROMPTED_GAP_MIN = 30      # an inbound this soon after our outbound is a reply, not pull
REPLY_WINDOW_MIN = 60        # a proactive outbound counts as answered if an inbound lands within this
COHORT_WEEKS = (0, 1, 2, 4)  # weeks after activation shown per cohort
MAX_COHORTS = 8
DEPTH_DAYS = 28


def _naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):.0f}%" if den else "—"


def _ratio(num: int, den: int) -> str:
    return f"{num} / {den}" + (f" ({_pct(num, den)})" if den else "")


class _UserSignals:
    __slots__ = ("tz", "core", "pull", "texted", "engaged")

    def __init__(self, tz):
        self.tz = tz
        self.core: list[datetime] = []
        self.pull: list[datetime] = []
        self.texted: list[datetime] = []
        self.engaged: list[datetime] = []

    def days(self, kind: str) -> set[str]:
        return {local_day(t, self.tz) for t in getattr(self, kind)}

    def days_since(self, kind: str, cutoff: datetime) -> set[str]:
        return {local_day(t, self.tz) for t in getattr(self, kind) if t >= cutoff}

    def any_between(self, kind: str, start: datetime, end: datetime) -> bool:
        return any(start <= t < end for t in getattr(self, kind))


def _collect(session, users, messages, now):
    """Per-user signal instants (naive UTC) from the rows the dashboard already has plus
    three small queries. Also returns the nudge counters."""
    by_id = {u.id: _UserSignals(getattr(u, "user_timezone", None)) for u in users}
    uids = list(by_id)
    if not uids:
        return by_id, (0, 0), None

    for uid, at in (session.query(Meal.user_id, Meal.logged_at)
                    .filter(Meal.user_id.in_(uids), Meal.deleted_at.is_(None)).all()):
        if at is not None:
            by_id[uid].core.append(_naive(at))
    for uid, done_at, sess_date in (session.query(WorkoutSession.user_id, SetLog.done_at, WorkoutSession.date)
                                    .join(SetLog, SetLog.session_id == WorkoutSession.id)
                                    .filter(WorkoutSession.user_id.in_(uids), SetLog.done.is_(True)).all()):
        at = done_at or sess_date
        if at is not None:
            by_id[uid].core.append(_naive(at))
    for uid, at in (session.query(WeightLog.user_id, WeightLog.weighed_at)
                    .filter(WeightLog.user_id.in_(uids)).all()):
        if at is not None:
            by_id[uid].core.append(_naive(at))

    presence_since = None
    for uid, first_at in (session.query(ActivityDay.user_id, ActivityDay.first_at)
                          .filter(ActivityDay.user_id.in_(uids), ActivityDay.signal.in_(PRESENCE_SIGNALS)).all()):
        by_id[uid].pull.append(_naive(first_at))
    first_row = session.query(func.min(ActivityDay.first_at)).scalar()
    if first_row is not None:
        presence_since = _naive(first_row)

    # Inbound texts: unprompted (pull) vs a reply to one of our outbounds. Outbounds that
    # are not themselves a reply are nudges; answered if an inbound lands soon after.
    gap, reply_window = timedelta(minutes=UNPROMPTED_GAP_MIN), timedelta(minutes=REPLY_WINDOW_MIN)
    depth_cutoff = now - timedelta(days=DEPTH_DAYS)
    per_user = defaultdict(list)
    for m in messages:
        if m.user_id in by_id and m.created_at is not None and m.message_type != "reaction":
            per_user[m.user_id].append((_naive(m.created_at), m.direction, m.delivery_status))
    nudges = answered = 0
    for uid, rows in per_user.items():
        rows.sort(key=lambda r: r[0])
        sig = by_id[uid]
        last_out = last_in = None
        inbound_times = [t for t, d, _ in rows if d == "in"]
        for t, direction, status in rows:
            if direction == "in":
                sig.texted.append(t)
                if last_out is None or t - last_out > gap:
                    sig.pull.append(t)
                last_in = t
            else:
                if status != "failed" and (last_in is None or t - last_in > gap) and t >= depth_cutoff:
                    nudges += 1
                    if any(t < ti <= t + reply_window for ti in inbound_times):
                        answered += 1
                last_out = t
    for sig in by_id.values():
        sig.engaged = sig.core + sig.pull
    return by_id, (nudges, answered), presence_since


def engagement(session, users, messages, *, now: datetime | None = None) -> dict:
    now = _naive(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    sigs, (nudges, answered), presence_since = _collect(session, users, messages, now)

    # Windows — same eligibility as the legacy rows (app.py active_in_window): created at
    # least N days ago, or no created_at at all.
    windows = []
    for n in (7, 14, 30):
        cutoff = now - timedelta(days=n)
        eligible = [u for u in users if not u.created_at or _naive(u.created_at) <= cutoff]
        counts = {k: sum(1 for u in eligible if any(t >= cutoff for t in getattr(sigs[u.id], k)))
                  for k in ("texted", "engaged", "core")}
        windows.append({"days": n, "eligible": len(eligible),
                        **{k: counts[k] for k in counts},
                        **{f"{k}_rate": _pct(counts[k], len(eligible)) for k in counts}})

    # Cohorts by activation week.
    def activation(u):
        return _naive(u.activated_at) or _naive(u.created_at)

    cohort_users = defaultdict(list)
    for u in users:
        a = activation(u)
        if a is not None:
            monday = (a - timedelta(days=a.weekday())).date()
            cohort_users[monday].append((u, a))

    def cohort_row(label, members):
        row = {"label": label, "users": len(members)}
        for k in COHORT_WEEKS:
            measurable = engaged = 0
            for u, a in members:
                start, end = a + timedelta(days=7 * k), a + timedelta(days=7 * (k + 1))
                if end > now:
                    continue
                measurable += 1
                if sigs[u.id].any_between("engaged", start, end):
                    engaged += 1
            row[f"w{k}"] = _ratio(engaged, measurable) if measurable else "—"
        return row

    cohorts = [cohort_row(monday.strftime("wk of %b %d"), members)
               for monday, members in sorted(cohort_users.items(), reverse=True)[:MAX_COHORTS]]
    all_members = [m for members in cohort_users.values() for m in members]
    if all_members:
        cohorts.append(cohort_row("all activated", all_members))

    # Depth + pull over the last 28 days.
    cutoff = now - timedelta(days=DEPTH_DAYS)
    per_week, pull_days, engaged_days = [], 0, 0
    for u in users:
        s = sigs[u.id]
        e = s.days_since("engaged", cutoff)
        if not e:
            continue
        per_week.append(len(e) * 7.0 / DEPTH_DAYS)
        engaged_days += len(e)
        pull_days += len(s.days_since("pull", cutoff) & e)
    depth = f"{median(per_week):.1f}" if per_week else "—"

    # Today, by each user's LOCAL day — the headline card on the Metrics page, beside the
    # legacy "Active Today" (any inbound since UTC midnight).
    today = {"engaged": 0, "core": 0, "pull": 0, "texted": 0}
    for u in users:
        s = sigs[u.id]
        day = local_day(now, s.tz)
        for k in today:
            if day in s.days(k):
                today[k] += 1

    return {
        "today": today,
        "windows": windows,
        "cohorts": cohorts,
        "cohort_weeks": [f"W{k}" for k in COHORT_WEEKS],
        "depth_days_per_week": depth,
        "depth_users": len(per_week),
        "pull_rate": _pct(pull_days, engaged_days),
        "pull_days": pull_days,
        "engaged_days": engaged_days,
        "nudge_rate": _pct(answered, nudges),
        "nudges": nudges,
        "nudges_answered": answered,
        "presence_since": presence_since.strftime("%b %d") if presence_since else None,
        "window_days": DEPTH_DAYS,
        "gap_min": UNPROMPTED_GAP_MIN,
        "reply_min": REPLY_WINDOW_MIN,
    }


__all__ = ["engagement", "UNPROMPTED_GAP_MIN", "REPLY_WINDOW_MIN"]
