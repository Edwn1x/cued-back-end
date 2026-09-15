"""
occupancy — read side of the RSF meter (series §2.3).
  now()      → {pct, label, line_on, est_wait_min, as_of} from the latest row (None if stale/none)
  expected() → median pct for (weekday, hour) over the last 4 weeks, once ≥14 days of data exist
  window()   → the lowest-expected 90-minute block still ahead and open today
Labels: dead (<25) · light (<50) · busy (<75) · packed (<95) · line (≥95).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from models import get_session, GymOccupancy
from integrations.rsf import FACILITY, TZ, is_open, hours_for

STALE_MIN = 20
LINE_PCT = 95


def label_for(pct: int) -> str:
    if pct < 25:
        return "dead"
    if pct < 50:
        return "light"
    if pct < 75:
        return "busy"
    if pct < LINE_PCT:
        return "packed"
    return "line"


# the coach's word for it (§3.4) and the beat's
WORD = {"dead": "empty", "light": "half empty", "busy": "filling up", "packed": "packed", "line": "line's on"}


def now(at: datetime | None = None) -> dict | None:
    at = at or datetime.now(timezone.utc).replace(tzinfo=None)
    session = get_session()
    try:
        row = (session.query(GymOccupancy).filter(GymOccupancy.facility == FACILITY)
               .order_by(GymOccupancy.ts.desc()).first())
        if not row or not row.ts or (at - row.ts) > timedelta(minutes=STALE_MIN):
            return None
        pct = int(row.pct or 0)
        return {"pct": pct, "label": label_for(pct), "line_on": pct >= LINE_PCT or row.est_wait_min is not None,
                "est_wait_min": row.est_wait_min, "as_of": row.ts}
    finally:
        session.close()


def as_of_local(reading: dict) -> str:
    return reading["as_of"].replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%-I:%M%p").lower()


def context_line(reading: dict | None) -> str:
    """'rsf weight room: 38% (light), as of 4:12pm.' — the model phrases; it never invents a number."""
    if not reading:
        return ""
    return f"rsf weight room: {reading['pct']}% ({reading['label']}), as of {as_of_local(reading)}."


def expected(weekday: int, hour: int, at: datetime | None = None) -> int | None:
    at = at or datetime.now(timezone.utc).replace(tzinfo=None)
    since = at - timedelta(days=28)
    session = get_session()
    try:
        rows = (session.query(GymOccupancy.ts, GymOccupancy.pct)
                .filter(GymOccupancy.facility == FACILITY, GymOccupancy.ts >= since, GymOccupancy.pct.isnot(None)).all())
    finally:
        session.close()
    days = {r.ts.date() for r in rows}
    if len(days) < 14:
        return None
    vals = []
    for ts, pct in rows:
        local = ts.replace(tzinfo=timezone.utc).astimezone(TZ)
        if local.weekday() == weekday and local.hour == hour:
            vals.append(int(pct))
    return int(statistics.median(vals)) if vals else None


def window(hours_ahead: int = 6, at: datetime | None = None) -> dict | None:
    """Lowest-expected 90-min block starting on a half hour, still ahead and open
    today. None when no expectation data yet."""
    at_local = (at or datetime.now(timezone.utc)).astimezone(TZ) if (at and at.tzinfo) else datetime.now(TZ)
    o, c = hours_for(at_local.weekday())
    best = None
    start = at_local.replace(minute=0 if at_local.minute < 30 else 30, second=0, microsecond=0) + timedelta(minutes=30)
    end_limit = min(at_local + timedelta(hours=hours_ahead), at_local.replace(hour=c, minute=0, second=0, microsecond=0))
    while start + timedelta(minutes=90) <= end_limit:
        if start.hour >= o:
            hs = {start.hour, (start + timedelta(minutes=45)).hour, (start + timedelta(minutes=89)).hour}
            exps = [expected(start.weekday(), h, at) for h in hs]
            if all(e is not None for e in exps):
                score = sum(exps) / len(exps)
                if best is None or score < best["expected_pct"]:
                    best = {"start": start, "end": start + timedelta(minutes=90), "expected_pct": int(round(score))}
        start += timedelta(minutes=30)
    return best
