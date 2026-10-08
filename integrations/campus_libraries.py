"""
Campus libraries — study rooms you can book + library hours/feature tags.
=========================================================================

The two live sources behind moffittstatus.asuc.org (investigated + verified 2026-10-08):

  * Room availability: the UC Berkeley LibCal booking grid,
    `POST https://berkeley.libcal.com/spaces/availability/grid` (lid = library, gid = 0
    for all groups, start/end = a one-day window). Anonymous, returns
    `{slots: [{start, end, itemId, checksum, className?}]}`; a slot WITHOUT a className
    is free, `s-lc-eq-checkout` means booked. Slots are hourly, in campus-local time.
    Room names/capacities come from the `resources.push({...})` blobs embedded in the
    spaces page (one page per group; seeded below so a scrape miss never empties it).
    Booking itself needs the student's CalNet login — we only ever SEND THE LINK
    (`/space/<eid>`), exactly like the RSF virtual-line link.

  * Hours + feature tags: `https://www.lib.berkeley.edu/hours?hours_date_select=YYYY-MM-DD`
    (server-rendered Drupal; honoured for other days). One `li.library-hours-listing`
    per library: name, today's hours text ("9 a.m.-2 a.m.", "24 hours", or blank =
    closed), address, maps link and five service tags (equipment lending, evening
    hours, research assistance, snacks allowed, study spaces).

NOT taken: the site's "crowd" meter — a hardcoded weekday/weekend curve in its JS with
every capacity set to 200. There is no live crowd source for libraries; the coach must
never state one.

Envelope (mirrors integrations/rsf.py + weather.py): real User-Agent, short timeouts,
per-process TTL caches (grid 15 min, hours 6 h), 5 consecutive grid failures → stop
for the day, and EVERY failure degrades to None so the coach can say "couldn't reach
the booking system" instead of inventing a room. Both endpoints are unofficial and
may change shape without warning — a shape change is a None, never an exception.
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger("cued.campus_libraries")

TZ = ZoneInfo("America/Los_Angeles")      # LibCal + the hours page speak campus time
GRID_URL = "https://berkeley.libcal.com/spaces/availability/grid"
SPACES_URL = "https://berkeley.libcal.com/spaces"
SPACE_URL = "https://berkeley.libcal.com/space/{eid}"
HOURS_URL = "https://www.lib.berkeley.edu/hours"
BOOKED_CLASS = "s-lc-eq-checkout"
MIN_LIBRARIES_PARSED = 3      # the live page lists ~29; fewer than this = a redesign, not an answer

# LibCal location ids (the spaces page's location selector, read 2026-10-08). `hours`
# is the exact name on the hours page so the two sources join. Rooms are offered for
# ROOM_LIDS only (Moffitt first — founder decision 2026-10-08); the rest get hours.
LIBRARIES = {
    8868: {"slug": "moffitt", "name": "Moffitt", "hours": "Moffitt Library",
           "gids": (16363, 16365, 16366)},
    8867: {"slug": "main_stacks", "name": "Main Stacks", "hours": "Main (Gardner) Stacks", "gids": ()},
    8863: {"slug": "engineering", "name": "Engineering Library",
           "hours": "Engineering & Mathematical Sciences Library", "gids": ()},
    8862: {"slug": "earth_sciences", "name": "Earth Sciences Library",
           "hours": "Earth Sciences & Map Library", "gids": ()},
    8864: {"slug": "east_asian", "name": "East Asian Library", "hours": "East Asian Library", "gids": ()},
    8865: {"slug": "env_design", "name": "Environmental Design Library",
           "hours": "Environmental Design Library", "gids": ()},
    8866: {"slug": "igs", "name": "IGS Library", "hours": "Institute of Governmental Studies Library", "gids": ()},
    22244: {"slug": "business", "name": "Business Library", "hours": "Business Library", "gids": ()},
    24641: {"slug": "social_research", "name": "Social Research Library",
            "hours": "Social Research Library", "gids": ()},
}
ROOM_LIDS = (8868,)

# Hours-page libraries worth naming when someone asks "what's open" even though they
# have no bookable rooms. Everything else on the page (archives, desks, LBNL) is noise.
STUDY_LIBRARIES = {
    "Moffitt Library": "Moffitt", "Doe Library": "Doe", "Main (Gardner) Stacks": "Main Stacks",
    "Engineering & Mathematical Sciences Library": "Engineering Library",
    "Business Library": "Business Library (Haas)",
    "Chemistry, Astronomy & Physics Library": "Chem/Physics Library",
    "Bioscience, Natural Resources & Public Health Library": "Bioscience Library",
    "Earth Sciences & Map Library": "Earth Sciences Library", "East Asian Library": "East Asian Library",
    "Environmental Design Library": "Environmental Design Library",
    "Social Research Library": "Social Research Library", "Music Library": "Music Library",
    "Morrison Library": "Morrison", "Berkeley Law Library": "Law Library",
    "Institute of Governmental Studies Library": "IGS Library",
    "Ethnic Studies Library": "Ethnic Studies Library",
    "Graduate Services (study only)": "Graduate Services (Doe)",
    "South/Southeast Asia Library (study only)": "South/Southeast Asia Library",
}

# "need" filter → the hours page's service tag.
NEED_TAGS = {
    "snacks": "Snacks allowed", "food": "Snacks allowed", "eat": "Snacks allowed",
    "tech": "Equipment lending", "charger": "Equipment lending", "laptop": "Equipment lending",
    "research": "Research assistance", "librarian": "Research assistance",
    "late": None,     # handled by hours (open at/after 11pm), not a tag
    "any": None, "": None,
}


@dataclass(frozen=True)
class Room:
    eid: int
    name: str          # "Egret, Room 409"
    capacity: int
    group: str         # "4th floor" / "5th floor" / "Van Houten"
    lid: int = 8868

    @property
    def url(self) -> str:
        return SPACE_URL.format(eid=self.eid)

    @property
    def library(self) -> str:
        return LIBRARIES.get(self.lid, {}).get("name", "the library")


def _r(eid, name, cap, group):
    return Room(eid=eid, name=name, capacity=cap, group=group)


# Read live 2026-10-08 from the three Moffitt group pages (+ Palm from /space/62891).
# The weekly refresh unions on top of these; unknown grid itemIds (hidden/admin items,
# e.g. 101038–101046 which the public page 404s) are never offered.
SEED_ROOMS: dict[int, Room] = {r.eid: r for r in (
    _r(62878, "Egret, Room 409", 4, "4th floor"),
    _r(62879, "Goldeneye, Room 411", 4, "4th floor"),
    _r(62880, "Quail, Room 431", 4, "4th floor"),
    _r(62881, "Tern, Room 433", 4, "4th floor"),
    _r(62882, "Warbler, Room 435", 4, "4th floor"),
    _r(62884, "Room 415", 8, "Van Houten"),
    _r(62885, "Room 417", 8, "Van Houten"),
    _r(62886, "Hemlock, Room 503", 4, "5th floor"),
    _r(62887, "Ironwood, Room 505", 4, "5th floor"),
    _r(62888, "Juniper, Room 509", 4, "5th floor"),
    _r(62889, "Laurel, Room 511", 4, "5th floor"),
    _r(62890, "Mesquite, Room 513", 4, "5th floor"),
    _r(62891, "Palm, Room 517", 4, "5th floor"),
    _r(62892, "Redwood, Room 519", 4, "5th floor"),
    _r(62893, "Tamarack, Room 521", 4, "5th floor"),
)}


@dataclass
class LibraryDay:
    name: str                      # hours-page name
    short: str                     # what the coach says
    hours_text: str                # "9 a.m.-2 a.m." / "24 hours" / "" (closed)
    open_min: int | None           # minutes from local midnight
    close_min: int | None          # may exceed 1440 for an overnight close
    tags: tuple = ()
    maps_url: str | None = None

    @property
    def is_24h(self) -> bool:
        return self.open_min == 0 and self.close_min == 1440 and "24" in self.hours_text

    @property
    def closed_all_day(self) -> bool:
        return self.open_min is None

    def is_open_at(self, local: datetime) -> bool:
        """Open at this campus-local clock time. An overnight close ("9 a.m.-2 a.m.") is
        stored past 1440, so 1am reads as yesterday's spill-over (today's hours stand in
        for yesterday's — the same most days) rather than as 'not open yet'."""
        if self.open_min is None:
            return False
        m = local.hour * 60 + local.minute
        if self.open_min <= m < self.close_min:
            return True
        return self.close_min > 1440 and m + 1440 < self.close_min

    def open_until_text(self) -> str:
        if self.closed_all_day:
            return "closed today"
        if self.is_24h:
            return "open 24 hours"
        return f"open {self.hours_text.replace(' a.m.', 'am').replace(' p.m.', 'pm').replace('-', '–')}"


@dataclass(frozen=True)
class Run:
    """A contiguous free stretch for one room (campus-local naive datetimes)."""
    room: Room
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


_state = {
    "rooms": dict(SEED_ROOMS),          # eid → Room
    "rooms_refreshed": None,
    "grid": {},                          # (lid, iso-date) → (fetched_at, slots)
    "hours": {},                         # iso-date → (fetched_at, {name: LibraryDay})
    "failures": 0,
    "stopped_for": None,
}


def user_agent() -> str:
    return f"Cued/1.0 (contact: {config.RSF_CONTACT_EMAIL})"


def _now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def reset_state() -> None:
    """Test seam."""
    _state.update({"rooms": dict(SEED_ROOMS), "rooms_refreshed": None, "grid": {}, "hours": {},
                   "failures": 0, "stopped_for": None})


# ─── hours page ──────────────────────────────────────────────────────────────

_LISTING_RE = re.compile(r'<li class="library-hours-listing"[^>]*>.*?</li>', re.S)
_NAME_RE = re.compile(r'class="library-name">\s*<a[^>]*>([^<]+)')
_HOURS_RE = re.compile(r'class="library-hours">([^<]*)')
_TAG_RE = re.compile(r'available-service (available|unavailable)">\s*<i[^>]*></i>\s*<div class="tooltip">([^<]+)')
_MAPS_RE = re.compile(r'google-maps-link"[^>]*href="([^"]+)"')
_CLOCK_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|noon|midnight)", re.I)


def parse_hours_text(text: str) -> tuple[int | None, int | None]:
    """'9 a.m.-2 a.m.' → (540, 1560); '24 hours' → (0, 1440); '' / unparseable → (None, None).
    An overnight close is expressed past 1440 so `open <= m < close` works until it."""
    t = (text or "").strip()
    if not t:
        return None, None
    if "24" in t and "hour" in t.lower():
        return 0, 1440
    clocks = _CLOCK_RE.findall(t)
    if len(clocks) < 2:
        return None, None

    def to_min(h, m, ap):
        ap = ap.lower()
        if ap.startswith("noon"):
            return 12 * 60
        if ap.startswith("mid"):
            return 0
        h = int(h) % 12 + (12 if ap.startswith("p") else 0)
        return h * 60 + int(m or 0)

    o = to_min(*clocks[0])
    c = to_min(*clocks[1])
    if c <= o:
        c += 1440
    return o, c


def parse_hours_page(html_text: str) -> dict[str, LibraryDay]:
    out = {}
    for block in _LISTING_RE.findall(html_text):
        m = _NAME_RE.search(block)
        if not m:
            continue
        name = _html.unescape(m.group(1)).strip()
        hm = _HOURS_RE.search(block)
        hours_text = _html.unescape(hm.group(1)).strip() if hm else ""
        o, c = parse_hours_text(hours_text)
        tags = tuple(t.strip() for a, t in _TAG_RE.findall(block) if a == "available")
        mm = _MAPS_RE.search(block)
        out[name] = LibraryDay(name=name, short=STUDY_LIBRARIES.get(name, name), hours_text=hours_text,
                               open_min=o, close_min=c, tags=tags, maps_url=mm.group(1) if mm else None)
    return out


def fetch_hours(day: date) -> dict[str, LibraryDay] | None:
    """Every library's hours for `day`, from the hours page (TTL-cached per day). None on
    any failure or when the page parses thin (a redesign) — never a half-answer."""
    if not config.CAMPUS_LIBRARIES_ENABLED:
        return None
    key = day.isoformat()
    hit = _state["hours"].get(key)
    if hit and time.time() - hit[0] <= config.CAMPUS_LIBRARIES_HOURS_TTL_S:
        return hit[1]
    try:
        r = requests.get(HOURS_URL, params={"hours_date_select": key},
                         headers={"User-Agent": user_agent()}, timeout=config.CAMPUS_LIBRARIES_TIMEOUT_S)
        r.raise_for_status()
        parsed = parse_hours_page(r.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("LIBRARY_HOURS_FETCH_FAILED day=%s err=%s", key, e)
        return hit[1] if hit else None      # stale beats nothing
    if len(parsed) < MIN_LIBRARIES_PARSED:
        logger.warning("LIBRARY_HOURS_PARSE_THIN day=%s n=%s", key, len(parsed))
        return hit[1] if hit else None
    _state["hours"][key] = (time.time(), parsed)
    logger.info("LIBRARY_HOURS_FETCHED day=%s n=%s", key, len(parsed))
    return parsed


def open_libraries(day: date, at_local: datetime, *, need: str | None = None,
                   hours: dict[str, LibraryDay] | None = None) -> list[LibraryDay] | None:
    """Study libraries open at `at_local` (campus-local naive), optionally filtered by a
    `need` (snacks/tech/research → a service tag; 'late' → still open at 11pm). None when
    hours couldn't be fetched."""
    hours = hours if hours is not None else fetch_hours(day)
    if hours is None:
        return None
    need = (need or "").strip().lower()
    tag = NEED_TAGS.get(need, None)
    out = []
    for name in STUDY_LIBRARIES:
        lib = hours.get(name)
        if not lib or not lib.is_open_at(at_local):
            continue
        if tag and tag not in lib.tags:
            continue
        if need == "late" and not (lib.close_min is not None and lib.close_min >= 23 * 60 + 60):
            continue
        out.append(lib)
    # 24h first, then latest close, then name — the ones you can count on lead.
    out.sort(key=lambda l: (not l.is_24h, -(l.close_min or 0), l.short))
    return out


# ─── room directory ──────────────────────────────────────────────────────────

_RES_RE = re.compile(r"resources\.push\(\{(.*?)\}\);", re.S)


def _js_str(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape")


def parse_spaces_page(html_text: str, *, lid: int) -> dict[int, Room]:
    out = {}
    for m in _RES_RE.finditer(html_text):
        body = m.group(1)

        def f(key):
            # quoted values may contain commas ("Egret, Room 409 (Capacity 4)"); bare
            # numbers end at the comma.
            mm = re.search(r"\b" + key + r':\s*(?:"((?:[^"\\]|\\.)*)"|([^,\n]+))', body)
            if not mm:
                return None
            return (mm.group(1) if mm.group(1) is not None else mm.group(2)).strip()

        try:
            eid = int(f("eid"))
            cap = int(f("capacity") or 0)
        except (TypeError, ValueError):
            continue
        title = _js_str(f("title") or "")
        title = re.sub(r"\s*\(Capacity \d+\)\s*$", "", title).strip()
        group = _js_str(f("grouping") or "")
        g = group.lower()
        short = ("Van Houten" if "van houten" in g else
                 "5th floor" if "5th" in g else "4th floor" if "4th" in g else group[:40])
        if title and cap > 0:
            out[eid] = Room(eid=eid, name=title, capacity=cap, group=short, lid=lid)
    return out


def refresh_rooms() -> bool:
    """Weekly: union the live directory over the seeds (never shrinks on a miss)."""
    if not config.CAMPUS_LIBRARIES_ENABLED:
        return False
    found = {}
    for lid in ROOM_LIDS:
        for gid in LIBRARIES[lid]["gids"]:
            try:
                r = requests.get(SPACES_URL, params={"lid": lid, "gid": gid},
                                 headers={"User-Agent": user_agent()}, timeout=config.CAMPUS_LIBRARIES_TIMEOUT_S)
                r.raise_for_status()
                found.update(parse_spaces_page(r.text, lid=lid))
            except Exception as e:  # noqa: BLE001
                logger.warning("LIBRARY_ROOMS_REFRESH_FAILED lid=%s gid=%s err=%s", lid, gid, e)
    if not found:
        return False
    _state["rooms"] = {**_state["rooms"], **found}
    _state["rooms_refreshed"] = _now_local()
    logger.info("LIBRARY_ROOMS_REFRESHED n=%s new=%s", len(_state["rooms"]), len(set(found) - set(SEED_ROOMS)))
    return True


def rooms() -> dict[int, Room]:
    return _state["rooms"]


def room(eid: int) -> Room | None:
    return _state["rooms"].get(int(eid)) if eid is not None else None


# ─── availability grid ───────────────────────────────────────────────────────

def _parse_slot_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def fetch_grid(lid: int, day: date) -> list[dict] | None:
    """Raw LibCal slots for one library-day (TTL-cached). None on flag-off, stop-for-day,
    HTTP error, non-JSON (outside the booking window comes back as an HTML page) or a
    changed shape."""
    if not config.CAMPUS_LIBRARIES_ENABLED:
        return None
    today = _now_local().date()
    if _state["stopped_for"] == today:
        return None
    key = (lid, day.isoformat())
    hit = _state["grid"].get(key)
    if hit and time.time() - hit[0] <= config.CAMPUS_LIBRARIES_GRID_TTL_S:
        return hit[1]
    data = {"lid": lid, "gid": 0, "eid": -1, "seat": 0, "seatId": 0, "zone": 0,
            "start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat(),
            "pageIndex": 0, "pageSize": 18}
    try:
        r = requests.post(GRID_URL, data=data, timeout=config.CAMPUS_LIBRARIES_TIMEOUT_S,
                          headers={"User-Agent": user_agent(), "Referer": f"{SPACES_URL}?lid={lid}&gid=0",
                                   "X-Requested-With": "XMLHttpRequest"})
        r.raise_for_status()
        payload = r.json()
        slots = payload["slots"]
        if not isinstance(slots, list):
            raise ValueError("slots not a list")
        for s in slots[:3]:
            _parse_slot_time(s["start"]); _parse_slot_time(s["end"]); int(s["itemId"])
    except Exception as e:  # noqa: BLE001
        _state["failures"] += 1
        logger.warning("LIBRARY_GRID_FAILED lid=%s day=%s n=%s err=%s", lid, day, _state["failures"], e)
        if _state["failures"] >= 5:
            _state["stopped_for"] = today
            _state["failures"] = 0
            logger.error("LIBRARY_GRID_STOPPED_FOR_DAY date=%s — 5 consecutive failures", today)
        return None
    _state["failures"] = 0
    _state["grid"][key] = (time.time(), slots)
    logger.info("LIBRARY_GRID_FETCHED lid=%s day=%s slots=%s items=%s", lid, day, len(slots),
                len({s.get("itemId") for s in slots}))
    return slots


def merge_runs(slots: list[dict], *, directory: dict[int, Room] | None = None) -> list[Run]:
    """Free hourly slots → contiguous runs per KNOWN room (unknown itemIds are skipped —
    they're hidden/admin items the public booking page 404s on)."""
    directory = directory if directory is not None else rooms()
    by_room: dict[int, list[tuple[datetime, datetime]]] = {}
    for s in slots:
        if s.get("className"):
            continue
        try:
            eid = int(s["itemId"])
            st, en = _parse_slot_time(s["start"]), _parse_slot_time(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if eid not in directory:
            continue
        by_room.setdefault(eid, []).append((st, en))
    runs = []
    for eid, ivs in by_room.items():
        ivs.sort()
        cur_s, cur_e = ivs[0]
        for st, en in ivs[1:]:
            if st <= cur_e:
                cur_e = max(cur_e, en)
            else:
                runs.append(Run(directory[eid], cur_s, cur_e))
                cur_s, cur_e = st, en
        runs.append(Run(directory[eid], cur_s, cur_e))
    return runs


def open_runs(lid: int, day: date, *, start: datetime | None = None, min_minutes: int = 60,
              min_capacity: int = 1, now: datetime | None = None, slots: list[dict] | None = None,
              limit: int = 3) -> list[Run] | None:
    """Rooms at `lid` free for ≥ `min_minutes` from `start` (campus-local naive; default now,
    rounded up to the hour) that seat ≥ `min_capacity`. Each run is clipped to begin no
    earlier than `start`. Best-fit first: earliest start, then the snuggest capacity, then
    the longest stretch. None when the grid couldn't be read."""
    now = now or _now_local()
    if start is None or start < now:
        start = now
    if start.minute or start.second:
        start = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    slots = slots if slots is not None else fetch_grid(lid, day)
    if slots is None:
        return None
    out = []
    for r in merge_runs(slots):
        if r.room.capacity < max(1, min_capacity):
            continue
        s = max(r.start, start)
        if r.end - s < timedelta(minutes=min_minutes):
            continue
        out.append(Run(r.room, s, r.end))
    out.sort(key=lambda r: (r.start, r.room.capacity, -r.minutes, r.room.name))
    return out[:limit] if limit else out


# ─── rendering for the coach ─────────────────────────────────────────────────

def _clock(dt: datetime, *, day: date | None = None) -> str:
    """'3pm' / '4:30pm'; a run that ends at the day's edge (00:00 next day, or LibCal's
    23:59) reads 'midnight' instead of '12am' / '11:59pm'."""
    if day is not None and (dt.date() > day or (dt.hour == 23 and dt.minute == 59)):
        return "midnight"
    h = dt.hour % 12 or 12
    ap = "am" if dt.hour < 12 else "pm"
    return f"{h}:{dt.minute:02d}{ap}" if dt.minute else f"{h}{ap}"


def _daylabel(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return day.strftime("%a %b %-d")


def format_runs(runs: list[Run], *, day: date, today: date) -> str:
    if not runs:
        return "no rooms free for that window"
    lib = runs[0].room.library
    parts = [f"{r.room.name} ({r.room.group}, fits {r.room.capacity}) free {_clock(r.start)}–{_clock(r.end, day=day)} "
             f"[eid {r.room.eid}]" for r in runs]
    return f"{lib} rooms {_daylabel(day, today)}: " + "; ".join(parts)


def format_open(libs: list[LibraryDay], *, at_local: datetime, limit: int = 6) -> str:
    if not libs:
        return f"no study libraries open at {_clock(at_local)}"
    parts = []
    for lib in libs[:limit]:
        extras = [t.lower().replace("snacks allowed", "snacks ok").replace("equipment lending", "tech lending")
                  .replace("research assistance", "research help")
                  for t in lib.tags if t in ("Snacks allowed", "Equipment lending", "Research assistance")]
        parts.append(f"{lib.short} ({lib.open_until_text()}" + (f"; {', '.join(extras)}" if extras else "") + ")")
    more = f" +{len(libs) - limit} more" if len(libs) > limit else ""
    return f"open at {_clock(at_local)}: " + "; ".join(parts) + more


def link_text(r: Room) -> str:
    return (f"here's the booking link for {r.name} at {r.library} (fits {r.capacity}) — tap it, sign in "
            f"with calnet, pick your hour: {r.url}")


def send_room_link(user_id: int, eid) -> str:
    """On-demand (agent_tools.send_study_room_link): text the LibCal booking link for one
    room as its own bubble — the real capability behind 'want the link?', mirroring
    gym_beats.send_line_link so the coach can never fake it. Returns a status line."""
    from models import get_session, User
    from sms import send_sms
    r = room(eid)
    if r is None:
        return f"error: no room with eid {eid!r} — only offer rooms from find_study_space"
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.phone:
            return "error: no phone on file"
        phone = user.phone
    finally:
        session.close()
    send_sms(phone, link_text(r), user_id=user_id, message_type="study_room_link")
    logger.info("STUDY_ROOM_LINK_SENT user=%s eid=%s", user_id, r.eid)
    return f"ok: sent the booking link for {r.name} — booking needs their calnet login, you can't confirm it for them"
