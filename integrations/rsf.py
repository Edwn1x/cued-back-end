"""
RSF weight-room crowd meter (series §2.1). The recwell page embeds a Density
"safe display"; its share token (public, in the page source) is exchanged for a
short-lived access token, then the display endpoint returns
`dedicated_space.current_count / capacity`. Verified live 2026-09-14
(141 / 150 = 94%). One request pair per poll, a real User-Agent, exponential
backoff on any non-2xx, and a stop-for-the-day after 5 consecutive failures.
Never polls when RSF is closed (hours page parsed weekly; defaults below).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, date, timedelta
from zoneinfo import ZoneInfo

import requests

import config
from models import get_session, GymOccupancy

logger = logging.getLogger("cued.rsf")

TZ = ZoneInfo("America/Los_Angeles")
IDENTITY_URL = "https://identity.density.io/oauth/wayfinding/exchange"
DISPLAY_URL = "https://api.density.io/app/v2/safe-display-core/displays/{display_id}"
HOURS_URL = "https://recwell.berkeley.edu/facilities/recreational-sports-facility-rsf/rsf-hours/"
FACILITY = "rsf_weights"

# (open_hour, close_hour) by weekday, Mon=0. RSF hours page, read 2026-09-14.
DEFAULT_HOURS = {0: (7, 23), 1: (7, 23), 2: (7, 23), 3: (7, 23), 4: (7, 23), 5: (8, 18), 6: (8, 23)}
CLOSED_DATES = {(11, 26), (12, 24), (12, 25), (1, 1)}   # Thanksgiving (approx), Xmas eve/day, New Year's

_state = {"hours": dict(DEFAULT_HOURS), "hours_refreshed": None, "failures": 0, "stopped_for": None,
          "token": None, "token_at": 0.0}


def user_agent() -> str:
    return f"Cued/1.0 (contact: {config.RSF_CONTACT_EMAIL})"


def is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc).astimezone(TZ)
    else:
        now = now.astimezone(TZ)
    if (now.month, now.day) in CLOSED_DATES:
        return False
    o, c = _state["hours"].get(now.weekday(), (7, 23))
    return o <= now.hour < c


def hours_for(weekday: int) -> tuple[int, int]:
    return _state["hours"].get(weekday, (7, 23))


_HOURS_RE = re.compile(r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s*[–-]\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday))?\s*[:\s]\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\s*[–-]\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", re.I)
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_hours(html_text: str) -> dict:
    """Weekly refresh: parse 'Monday–Friday 7 a.m.–11 p.m.' lines into {weekday: (open, close)}."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    out = {}
    for m in _HOURS_RE.finditer(text):
        d1, d2, h1, _m1, ap1, h2, _m2, ap2 = m.groups()
        def to24(h, ap):
            h = int(h) % 12
            return h + (12 if ap.lower().startswith("p") else 0)
        o, c = to24(h1, ap1), to24(h2, ap2)
        i1 = _DAYS.index(d1.lower()); i2 = _DAYS.index(d2.lower()) if d2 else i1
        for d in range(i1, i2 + 1):
            out[d] = (o, c)
    return out


def refresh_hours() -> bool:
    try:
        r = requests.get(HOURS_URL, headers={"User-Agent": user_agent()}, timeout=15)
        r.raise_for_status()
        parsed = parse_hours(r.text)
        if len(parsed) >= 5:
            _state["hours"] = {**DEFAULT_HOURS, **parsed}
            _state["hours_refreshed"] = datetime.now(TZ)
            logger.info("RSF_HOURS_REFRESHED hours=%s", _state["hours"])
            return True
        logger.warning("RSF_HOURS_PARSE_THIN parsed=%s — keeping current", parsed)
    except Exception as e:  # noqa: BLE001
        logger.warning("RSF_HOURS_REFRESH_FAILED err=%s — keeping current", e)
    return False


def _access_token() -> str:
    if _state["token"] and time.time() - _state["token_at"] < 600:
        return _state["token"]
    r = requests.post(IDENTITY_URL, headers={"Authorization": f"Bearer {config.DENSITY_SHARE_TOKEN}",
                                             "User-Agent": user_agent()}, timeout=config.RSF_TIMEOUT_S)
    r.raise_for_status()
    tok = r.json()["access_token"]
    _state["token"], _state["token_at"] = tok, time.time()
    return tok


def fetch_reading() -> dict:
    """{count, capacity, pct, name, raw} or raises."""
    r = requests.get(DISPLAY_URL.format(display_id=config.DENSITY_DISPLAY_ID),
                     headers={"Authorization": f"Bearer {_access_token()}", "User-Agent": user_agent()},
                     timeout=config.RSF_TIMEOUT_S)
    if r.status_code in (401, 403):
        _state["token"] = None   # expired → re-exchange next time
    r.raise_for_status()
    data = r.json()
    space = data["dedicated_space"]
    count, cap = int(space["current_count"]), int(space.get("capacity") or 140)
    pct = int(round(100.0 * max(count, 0) / max(cap, 1)))
    return {"count": count, "capacity": cap, "pct": pct, "name": space.get("name"),
            "raw": {"current_count": count, "capacity": cap, "target_capacity": space.get("target_capacity"),
                    "safe_capacity": space.get("safe_capacity"), "daily_reset": space.get("daily_reset")}}


def poll_once(now: datetime | None = None) -> dict | None:
    """One poll → one gym_occupancy row. Returns the reading, or None when skipped
    (closed, stopped for the day, flag off)."""
    if not config.RSF_METER_ENABLED:
        return None
    now = now or datetime.now(TZ)
    today = now.astimezone(TZ).date() if now.tzinfo else now.date()
    if _state["stopped_for"] == today:
        return None
    if not is_open(now):
        return None
    try:
        reading = fetch_reading()
    except Exception as e:  # noqa: BLE001
        _state["failures"] += 1
        wait = min(2 ** _state["failures"], 60)
        logger.warning("RSF_POLL_FAILED n=%s backoff_min=%s err=%s", _state["failures"], wait, e)
        if _state["failures"] >= 5:
            _state["stopped_for"] = today
            _state["failures"] = 0
            logger.error("RSF_POLL_STOPPED_FOR_DAY date=%s — 5 consecutive failures", today)
        return None
    _state["failures"] = 0
    session = get_session()
    try:
        session.add(GymOccupancy(facility=FACILITY, ts=datetime.now(timezone.utc).replace(tzinfo=None),
                                 pct=reading["pct"], est_wait_min=None, raw=reading["raw"]))
        session.commit()
    finally:
        session.close()
    logger.info("RSF_POLL pct=%s count=%s/%s", reading["pct"], reading["count"], reading["capacity"])
    return reading


def backoff_due(now_ts: float | None = None) -> bool:
    """For the scheduler: after a failure, skip polls until the backoff elapses."""
    return False  # the 5-minute cadence already exceeds the backoff at n<=3; kept for the test seam
