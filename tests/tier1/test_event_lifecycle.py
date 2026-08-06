"""
Memory-freshness Fix 1 — event lifecycle (upcoming → passed → done).

Whether an event is upcoming or passed is a FACT code computes from its datetimes
and now — the model must never infer it from a timeless string. The store bug this
guards first: a date-only log_event ("exam friday", no time) used to default
occurred_at to *now*, so the event silently landed on today and, under the
lifecycle readers, would render as already-passed — a new wrong output.

Lifecycle is computed, never stored (rewrite/memory-freshness/INVESTIGATION.md
§design-question): `upcoming_events` / `recently_passed_events` derive state from
occurred_at/ends_at vs now in the user's local tz. Retirement is the bounded 48h
passed-window plus the existing anti-repetition machinery — no write-on-render state.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


def _naive_utc(aware: datetime) -> datetime:
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _local_now(tz="America/Los_Angeles") -> datetime:
    return datetime.now(ZoneInfo(tz))


def _section(ctx: str, header_prefix: str) -> str:
    """The body of the ## section whose header starts with header_prefix ('' if absent)."""
    for chunk in ctx.split("\n\n"):
        if chunk.startswith("## ") and chunk[3:].startswith(header_prefix):
            return chunk
    return ""


def _ctx(user_id: int) -> str:
    from agent_loop import build_loop_context
    from models import get_session, User
    s = get_session()
    try:
        return build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


# ── 1. store: a date-only event keeps its date ───────────────────────────────

def test_date_only_event_keeps_its_date(db):
    """'exam tomorrow' with no time must store TOMORROW (all-day), not now-today."""
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from models import get_session, Event
    from timefmt import to_local

    user = make_user(db, user_timezone="America/Los_Angeles")
    out = handle_log_event(user.id, {"description": "orgo exam", "date": "tomorrow"})
    assert out.startswith("ok"), out

    s = get_session()
    try:
        ev = s.query(Event).filter(Event.user_id == user.id).one()
    finally:
        s.close()
    tomorrow_local = _local_now().date() + timedelta(days=1)
    start_local = to_local(ev.occurred_at, user)
    assert start_local.date() == tomorrow_local, (
        f"date-only event lost its date: stored {start_local.date()}, wanted {tomorrow_local}")
    # all-day shape: starts at local midnight, ends the same local day
    assert (start_local.hour, start_local.minute) == (0, 0)
    assert ev.ends_at is not None and to_local(ev.ends_at, user).date() == tomorrow_local


# ── 2. reader + render: forward visibility ───────────────────────────────────

def test_future_event_renders_in_upcoming_not_today(db):
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from events import upcoming_events

    user = make_user(db)
    handle_log_event(user.id, {"description": "coding interview",
                               "starts_at": "14:15", "date": "tomorrow"})

    ups = upcoming_events(user.id)
    assert [e.raw_text for e in ups] == ["coding interview"]

    ctx = _ctx(user.id)
    upcoming = _section(ctx, "UPCOMING EVENTS")
    assert "coding interview" in upcoming, "future event invisible before its day"
    assert "coding interview" not in _section(ctx, "TODAY'S EVENTS")


# ── 3. reader + render: passed is never upcoming; ages out at 48h ────────────

def test_passed_prior_day_event_recently_passed_then_ages_out(db):
    from tests.factories import make_user
    from events import record_event, upcoming_events, recently_passed_events

    user = make_user(db)
    y = _local_now() - timedelta(days=1)
    start = _naive_utc(y.replace(hour=14, minute=15, second=0, microsecond=0))
    record_event(user.id, "scheduled", source="model", raw_text="coding interview",
                 occurred_at=start,
                 ends_at=start + timedelta(minutes=60))

    assert upcoming_events(user.id) == [], "a passed event must NEVER read as upcoming"
    passed = recently_passed_events(user.id)
    assert [e.raw_text for e in passed] == ["coding interview"]

    ctx = _ctx(user.id)
    assert "coding interview" in _section(ctx, "RECENTLY PASSED")
    assert "coding interview" not in _section(ctx, "UPCOMING EVENTS")
    assert "coding interview" not in _section(ctx, "TODAY'S EVENTS")

    # 48h later the follow-up window has closed: the event retires everywhere.
    later = datetime.now(timezone.utc) + timedelta(days=3)
    assert recently_passed_events(user.id, now=later) == []
    assert upcoming_events(user.id, now=later) == []


# ── 4. render: today's passed event is marked PASSED in place ────────────────

def test_todays_passed_event_carries_passed_suffix(db):
    """Same-day: a 2:15pm event at 6pm must not render identically to one at 9pm.
    (Times are real-now-relative with small offsets — the same trade the episodic
    quiet-gate tests make.)"""
    from tests.factories import make_user
    from events import record_event

    user = make_user(db)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    record_event(user.id, "scheduled", source="model", raw_text="advising appt",
                 occurred_at=now_utc - timedelta(minutes=40),
                 ends_at=now_utc - timedelta(minutes=20))
    record_event(user.id, "scheduled", source="model", raw_text="study group",
                 occurred_at=now_utc + timedelta(minutes=40),
                 ends_at=now_utc + timedelta(minutes=80))

    today = _section(_ctx(user.id), "TODAY'S EVENTS")
    assert "advising appt" in today and "study group" in today
    entries = today.split("; ")   # today's events render "; "-joined on one line
    appt = next(e for e in entries if "advising appt" in e)
    group = next(e for e in entries if "study group" in e)
    assert "PASSED" in appt, "a passed same-day event renders identically to an upcoming one"
    assert "PASSED" not in group


# ── 5. tz boundary: local day, not UTC day ───────────────────────────────────

def test_5pm_pt_event_not_passed_at_11pm_utc_same_local_day(db):
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from events import recently_passed_events, event_end
    from models import get_session, Event

    user = make_user(db, user_timezone="America/Los_Angeles")
    handle_log_event(user.id, {"description": "gym with Marcus",
                               "starts_at": "17:00", "date": "today"})

    s = get_session()
    try:
        ev = s.query(Event).filter(Event.user_id == user.id).one()
    finally:
        s.close()

    # 4pm PT on the event's local day == 11pm UTC — the UTC date has already rolled.
    tz = ZoneInfo("America/Los_Angeles")
    ref = datetime.now(tz).replace(hour=16, minute=0, second=0, microsecond=0)
    ref_utc = ref.astimezone(timezone.utc)
    assert event_end(ev) > _naive_utc(ref_utc), "effective end miscomputed"
    assert recently_passed_events(user.id, now=ref_utc) == [], \
        "a 5pm-PT event must not be 'passed' at 11pm UTC of the same local day"
