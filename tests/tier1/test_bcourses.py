"""Part 1.4 — bCourses (Canvas) calendar feed: pasted URL → stored row + code reply,
6h sync parses the ICS into the shared event store (source='bcourses'), prunes
vanished events, tolerates transient fetch failures, and surfaces through the
normal UPCOMING reader. No OAuth, no model."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

import config

FEED = "https://bcourses.berkeley.edu/feeds/calendars/user_AbC123xYz.ics"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "BCOURSES_ENABLED", True)
    yield


def _row(uid):
    from models import get_session
    from integrations.base import get_integration
    s = get_session()
    try:
        return get_integration(s, uid, "bcourses")
    finally:
        s.close()


def _run_inbound(db, user, body, monkeypatch, inline_result=3):
    """inline_result: what the bounded first pull returns (int upserted, or None =
    didn't finish → background)."""
    from models import get_session, User
    from integrations import bcourses
    spawned = []
    monkeypatch.setattr(bcourses, "_spawn_first_sync", lambda uid: spawned.append(uid))
    monkeypatch.setattr(bcourses, "_try_sync_now", lambda uid: inline_result)
    s = get_session()
    try:
        u = s.get(User, user.id)
        terminal = bcourses.handle_inbound_feed_url(s, u, body, channel="imessage")
        s.commit()
    finally:
        s.close()
    return terminal, spawned


# ─── URL sniff ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,found", [
    (FEED, True),
    (f"here's my calendar {FEED} lmk", True),
    ("https://www.bcourses.berkeley.edu/feeds/calendars/user_abc.ics", True),
    ("HTTPS://BCOURSES.BERKELEY.EDU/feeds/calendars/user_ABC.ics", True),
    ("https://bcourses.berkeley.edu/calendar", False),
    ("https://calendar.google.com/calendar/ical/x/basic.ics", False),
    ("i use bcourses for everything", False),
    ("", False),
])
def test_feed_url_detection(body, found):
    from integrations.bcourses import find_feed_url
    assert (find_feed_url(body) is not None) is found


def test_paste_stores_feed_pulls_inline_and_replies(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from integrations.bcourses import GOT_IT
    user = make_user(db)

    terminal, spawned = _run_inbound(db, user, f"this is it {FEED}", monkeypatch, inline_result=3)
    assert terminal is True
    row = _row(user.id)
    assert row is not None and row.status == "connected"
    assert row.meta["feed_url"] == FEED
    assert row.access_token is None
    assert [b for _p, b in sms_capture] == [GOT_IT]
    assert spawned == []                         # inline pull finished → no background


def test_empty_feed_says_so_in_the_reply(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from integrations.bcourses import GOT_IT_EMPTY
    user = make_user(db)
    terminal, spawned = _run_inbound(db, user, FEED, monkeypatch, inline_result=0)
    assert terminal and spawned == []
    assert [b for _p, b in sms_capture] == [GOT_IT_EMPTY]


def test_slow_first_pull_goes_async_with_the_plain_reply(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from integrations.bcourses import GOT_IT
    user = make_user(db)
    terminal, spawned = _run_inbound(db, user, FEED, monkeypatch, inline_result=None)
    assert terminal and spawned == [user.id]
    assert [b for _p, b in sms_capture] == [GOT_IT]


def test_pasted_url_is_scrubbed_from_the_logged_inbound(db, sms_capture, monkeypatch):
    """The feed link is a bearer-ish secret: it must not stay in messages (the
    conversation window the model sees)."""
    from tests.factories import make_user
    from sms import log_incoming
    from models import get_session, Message
    from integrations.bcourses import REDACTED
    user = make_user(db)
    log_incoming(user.id, f"here u go {FEED}", channel="imessage")
    _run_inbound(db, user, f"here u go {FEED}", monkeypatch)
    s = get_session()
    try:
        m = (s.query(Message).filter(Message.user_id == user.id, Message.direction == "in")
             .order_by(Message.id.desc()).first())
        assert FEED not in m.body and REDACTED in m.body and m.body.startswith("here u go")
    finally:
        s.close()


def test_feed_sync_defers_to_a_live_canvas_token(db, monkeypatch):
    from tests.factories import make_user
    from integrations import bcourses
    from models import get_session, Integration
    monkeypatch.setattr(config, "CANVAS_ENABLED", True)
    user = make_user(db)
    _connect(db, user.id)
    s = get_session()
    try:
        s.add(Integration(user_id=user.id, provider="canvas", status="connected",
                          access_token="ciphertext", meta={}))
        s.commit()
    finally:
        s.close()
    assert bcourses.sync_user(user.id) == {"skipped": "canvas token active"}


def test_repaste_replaces_url_and_clears_error(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from integrations.bcourses import save_feed
    from models import get_session
    user = make_user(db)
    save_feed(user.id, FEED)
    s = get_session()
    try:
        from integrations.base import get_integration
        r = get_integration(s, user.id, "bcourses")
        r.status = "error"
        r.meta = {**r.meta, "last_error": "boom", "fail_count": 3}
        s.commit()
    finally:
        s.close()

    new = FEED.replace("AbC123xYz", "NewTok9")
    terminal, _ = _run_inbound(db, user, new, monkeypatch)
    assert terminal
    row = _row(user.id)
    assert row.status == "connected" and row.meta["feed_url"] == new
    assert "last_error" not in row.meta and "fail_count" not in row.meta


def test_plain_message_falls_through(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    user = make_user(db)
    terminal, spawned = _run_inbound(db, user, "what's due this week", monkeypatch)
    assert terminal is False and spawned == [] and sms_capture == []
    assert _row(user.id) is None


def test_flag_off_is_noop(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    monkeypatch.setattr(config, "BCOURSES_ENABLED", False)
    user = make_user(db)
    terminal, _ = _run_inbound(db, user, FEED, monkeypatch)
    assert terminal is False and sms_capture == [] and _row(user.id) is None


# ─── ICS parse + sync ─────────────────────────────────────────────────────────

def _ics(*vevents):
    body = "".join(vevents)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Instructure//Canvas//EN\r\n"
            f"{body}END:VCALENDAR\r\n")


def _vevent(uid, summary, start, end=None, all_day=False):
    if all_day:
        s = f"DTSTART;VALUE=DATE:{start:%Y%m%d}\r\n"
        e = f"DTEND;VALUE=DATE:{end:%Y%m%d}\r\n" if end else ""
    elif start.tzinfo is None:
        s = f"DTSTART:{start:%Y%m%dT%H%M%S}\r\n"          # floating → user-local
        e = f"DTEND:{end:%Y%m%dT%H%M%S}\r\n" if end else ""
    else:
        s = f"DTSTART:{start:%Y%m%dT%H%M%S}Z\r\n"
        e = f"DTEND:{end:%Y%m%dT%H%M%S}Z\r\n" if end else ""
    return (f"BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\n{s}{e}"
            f"DESCRIPTION:x\r\nEND:VEVENT\r\n")


def _connect(db, user_id):
    from integrations.bcourses import save_feed
    save_feed(user_id, FEED)


def test_sync_parses_upserts_windows_and_prunes(db, monkeypatch):
    from tests.factories import make_user
    from integrations import bcourses
    from events import upcoming_events
    from models import get_session, Event

    user = make_user(db)
    _connect(db, user.id)
    now = datetime.now(timezone.utc)
    due = (now + timedelta(days=3)).replace(hour=6, minute=59, second=59, microsecond=0)
    allday = (now + timedelta(days=5)).date()
    past = now - timedelta(days=10)
    floating_local = (now + timedelta(days=2)).astimezone(ZoneInfo("America/Los_Angeles")) \
        .replace(hour=14, minute=0, second=0, microsecond=0, tzinfo=None)

    feed = _ics(
        _vevent("event-assignment-101", "HW 3 [CS 61C]", due, due),
        _vevent("event-calendar-event-7", "Midterm review", allday, allday + timedelta(days=1), all_day=True),
        _vevent("event-assignment-old", "HW 1", past, past),
        _vevent("event-calendar-event-sem", "Fall semester", now + timedelta(days=1), now + timedelta(days=90)),
        _vevent("event-calendar-event-lab", "Lab section", floating_local, floating_local + timedelta(hours=2)),
    )
    monkeypatch.setattr(bcourses, "_fetch_ics", lambda url, **kw: feed)

    res = bcourses.sync_user(user.id)
    assert res == {"upserted": 3, "deleted": 0}

    ups = upcoming_events(user.id, days=60)
    by_title = {e.title: e for e in ups}
    assert "due: HW 3 [CS 61C]" in by_title            # assignment → 'due:' prefix
    assert by_title["due: HW 3 [CS 61C]"].source == "bcourses"
    assert by_title["due: HW 3 [CS 61C]"].occurred_at == due.replace(tzinfo=None)
    assert by_title["due: HW 3 [CS 61C]"].raw_text == "due: HW 3 [CS 61C]"   # readers fall back to raw_text
    assert by_title["Midterm review"].all_day is True
    assert "Lab section" in by_title
    lab = by_title["Lab section"]
    assert lab.all_day is False
    # floating 2pm LA == 21:00 UTC (PDT) or 22:00 UTC (PST)
    assert lab.occurred_at.hour in (21, 22)
    assert "HW 1" not in by_title and "Fall semester" not in by_title

    # row bookkeeping
    row = _row(user.id)
    assert row.status == "connected" and row.meta.get("last_sync_at") and row.meta.get("fail_count") == 0

    # idempotent: same feed again → no duplicates
    bcourses.sync_user(user.id)
    s = get_session()
    try:
        n = s.query(Event).filter(Event.user_id == user.id, Event.source == "bcourses",
                                  Event.deleted_at.is_(None)).count()
        assert n == 3
    finally:
        s.close()

    # assignment dropped from the feed → soft-deleted; others untouched
    feed2 = _ics(
        _vevent("event-calendar-event-7", "Midterm review", allday, allday + timedelta(days=1), all_day=True),
        _vevent("event-calendar-event-lab", "Lab section", floating_local, floating_local + timedelta(hours=2)),
    )
    monkeypatch.setattr(bcourses, "_fetch_ics", lambda url, **kw: feed2)
    res2 = bcourses.sync_user(user.id)
    assert res2 == {"upserted": 2, "deleted": 1}
    titles = {e.title for e in upcoming_events(user.id, days=60)}
    assert "due: HW 3 [CS 61C]" not in titles and "Midterm review" in titles

    # a re-added assignment revives the same row, no duplicate
    monkeypatch.setattr(bcourses, "_fetch_ics", lambda url, **kw: feed)
    bcourses.sync_user(user.id)
    s = get_session()
    try:
        n = s.query(Event).filter(Event.user_id == user.id, Event.source == "bcourses",
                                  Event.external_id == "event-assignment-101").count()
        assert n == 1
    finally:
        s.close()


def test_due_deadline_gates_heartbeat_only_in_its_window(db, monkeypatch):
    """A timed due marker starting within 90 min counts as a block (zero-length, so
    it stops gating the moment it passes); an all-day one never gates."""
    from tests.factories import make_user
    from integrations import bcourses
    from events import calendar_block_soon
    user = make_user(db)
    _connect(db, user.id)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    soon = now + timedelta(minutes=30)
    feed = _ics(_vevent("event-assignment-9", "quiz", soon, soon))
    monkeypatch.setattr(bcourses, "_fetch_ics", lambda url, **kw: feed)
    bcourses.sync_user(user.id)
    assert calendar_block_soon(user.id, now=now) is True
    assert calendar_block_soon(user.id, now=now + timedelta(minutes=31)) is False


def test_fetch_failures_flip_to_error_only_after_three_and_heal(db, monkeypatch):
    from tests.factories import make_user
    from integrations import bcourses
    user = make_user(db)
    _connect(db, user.id)

    def _boom(url, **kw):
        raise RuntimeError("503 from canvas")
    monkeypatch.setattr(bcourses, "_fetch_ics", _boom)

    assert "error" in bcourses.sync_user(user.id)
    assert _row(user.id).status == "connected" and _row(user.id).meta["fail_count"] == 1
    bcourses.sync_user(user.id)
    assert _row(user.id).status == "connected"
    bcourses.sync_user(user.id)
    row = _row(user.id)
    assert row.status == "error" and row.meta["last_error"].startswith("503")

    # the coach sees the state, never the URL
    from integrations.base import status_line
    sl = status_line(user.id)
    assert sl == "bcourses error" and "bcourses.berkeley.edu" not in sl

    # error rows are still polled and heal on the next good pull
    monkeypatch.setattr(bcourses, "_fetch_ics", lambda url, **kw: _ics())
    assert bcourses.sync_all() == 1
    row = _row(user.id)
    assert row.status == "connected" and row.meta["fail_count"] == 0 and "last_error" not in row.meta
    assert status_line(user.id) == "bcourses connected"


def test_non_ics_response_is_a_failure(monkeypatch):
    from integrations import bcourses

    class _R:
        text = "<html>login please</html>"
        def raise_for_status(self):
            pass
    monkeypatch.setattr(bcourses.requests, "get", lambda *a, **k: _R())
    with pytest.raises(ValueError):
        bcourses._fetch_ics(FEED)


def test_sync_skips_when_not_connected_or_flag_off(db, monkeypatch):
    from tests.factories import make_user
    from integrations import bcourses
    user = make_user(db)
    assert bcourses.sync_user(user.id) == {"skipped": "not connected"}
    monkeypatch.setattr(config, "BCOURSES_ENABLED", False)
    assert bcourses.sync_user(user.id) == {"skipped": "flag off"}
    assert bcourses.sync_all() == 0


def test_sync_all_touches_only_bcourses_rows(db, monkeypatch):
    from tests.factories import make_user
    from integrations import bcourses
    from models import get_session, Integration
    a, b = make_user(db), make_user(db)
    _connect(db, a.id)
    s = get_session()
    try:
        s.add(Integration(user_id=b.id, provider="gcal", status="connected", meta={}))
        s.commit()
    finally:
        s.close()
    seen = []
    monkeypatch.setattr(bcourses, "sync_user", lambda uid: seen.append(uid))
    assert bcourses.sync_all() == 1 and seen == [a.id]
