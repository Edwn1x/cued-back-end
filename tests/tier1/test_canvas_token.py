"""Part 1.4b — Canvas personal access token: pasted token → validated, Fernet-stored,
scrubbed from the logged inbound, answered in code; 30-min planner sync maps
assignments/quizzes/events/notes into the event store (source='canvas'), drops
submitted work, prunes vanished items, supersedes the feed's twins while live, and
revokes cleanly on 401 (feed sync resumes). Never a model call, never the token in
context."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

import config

TOKEN = "7~" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5zA7bC9dE1fG3hI5jK7lM9nO1pQ3"   # 64 after ~
FEED = "https://bcourses.berkeley.edu/feeds/calendars/user_AbC123xYz.ics"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(config, "CANVAS_ENABLED", True)
    monkeypatch.setattr(config, "BCOURSES_ENABLED", True)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    yield


def _row(uid, provider="canvas"):
    from models import get_session
    from integrations.base import get_integration
    s = get_session()
    try:
        return get_integration(s, uid, provider)
    finally:
        s.close()


def _run_inbound(db, user, body, monkeypatch, *, whoami=None, sync=None):
    from models import get_session, User
    from integrations import canvas
    spawned = []
    monkeypatch.setattr(canvas, "_spawn_sync", lambda uid: spawned.append(uid))
    if whoami is not None:
        monkeypatch.setattr(canvas, "whoami", whoami)
    if sync is not None:
        monkeypatch.setattr(canvas, "sync_user", sync)
    s = get_session()
    try:
        u = s.get(User, user.id)
        terminal = canvas.handle_inbound_token(s, u, body, channel="imessage")
        s.commit()
    finally:
        s.close()
    return terminal, spawned


# ─── token sniff ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,found", [
    (TOKEN, True),
    (f"here's my token {TOKEN} pls dont leak it", True),
    ("1234~" + "x" * 64, True),
    ("7~short", False),
    ("call me at 7~8pm", False),
    (FEED, False),
    ("what's due this week", False),
    ("", False),
])
def test_token_detection(body, found):
    from integrations.canvas import find_token
    assert (find_token(body) is not None) is found


# ─── paste flow ───────────────────────────────────────────────────────────────

def test_valid_token_is_stored_encrypted_scrubbed_and_answered(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from sms import log_incoming
    from models import get_session, Message
    from integrations import crypto
    from integrations.canvas import REPLY_OK, REDACTED
    user = make_user(db)
    log_incoming(user.id, f"ok here {TOKEN}", channel="imessage")

    terminal, spawned = _run_inbound(
        db, user, f"ok here {TOKEN}", monkeypatch,
        whoami=lambda tok, **kw: {"id": 424242, "name": "Nau"},
        sync=lambda uid, **kw: {"upserted": 5, "done": 1, "pruned": 0, "superseded": 0})
    assert terminal is True and spawned == []
    row = _row(user.id)
    assert row.status == "connected" and row.external_id == "424242"
    assert row.access_token and row.access_token != TOKEN          # ciphertext at rest
    assert crypto.decrypt(row.access_token) == TOKEN
    assert [b for _p, b in sms_capture] == [REPLY_OK.format(n=5)]

    s = get_session()
    try:
        m = (s.query(Message).filter(Message.user_id == user.id, Message.direction == "in")
             .order_by(Message.id.desc()).first())
        assert TOKEN not in m.body and REDACTED in m.body and m.body.startswith("ok here")
    finally:
        s.close()


def test_empty_board_and_slow_pull_replies(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from integrations.canvas import REPLY_EMPTY, REPLY_ASYNC
    u1, u2 = make_user(db), make_user(db)
    _run_inbound(db, u1, TOKEN, monkeypatch, whoami=lambda tok, **kw: {"id": 1},
                 sync=lambda uid, **kw: {"upserted": 0, "done": 0, "pruned": 0, "superseded": 0})
    sms_capture_1 = [b for _p, b in sms_capture]
    assert sms_capture_1 == [REPLY_EMPTY]

    def _slow(uid, **kw):
        raise TimeoutError("read timed out")
    terminal, spawned = _run_inbound(db, u2, TOKEN, monkeypatch, whoami=lambda tok, **kw: {"id": 2}, sync=_slow)
    assert terminal and spawned == [u2.id]
    assert [b for _p, b in sms_capture][-1] == REPLY_ASYNC


def test_bad_token_is_rejected_but_still_terminal_and_scrubbed(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from sms import log_incoming
    from models import get_session, Message
    from integrations.canvas import InvalidToken, REPLY_BAD_TOKEN, REDACTED
    user = make_user(db)
    log_incoming(user.id, TOKEN, channel="imessage")

    def _401(tok, **kw):
        raise InvalidToken("401")
    terminal, spawned = _run_inbound(db, user, TOKEN, monkeypatch, whoami=_401)
    assert terminal is True and spawned == []
    assert _row(user.id) is None
    assert [b for _p, b in sms_capture] == [REPLY_BAD_TOKEN]
    s = get_session()
    try:
        m = s.query(Message).filter(Message.user_id == user.id, Message.direction == "in").first()
        assert m.body == REDACTED
    finally:
        s.close()


def test_canvas_unreachable_asks_to_retry(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    from integrations.canvas import REPLY_TRY_LATER
    user = make_user(db)

    def _down(tok, **kw):
        raise ConnectionError("dns")
    terminal, _ = _run_inbound(db, user, TOKEN, monkeypatch, whoami=_down)
    assert terminal and _row(user.id) is None
    assert [b for _p, b in sms_capture] == [REPLY_TRY_LATER]


def test_plain_message_and_flag_off_fall_through(db, sms_capture, monkeypatch):
    from tests.factories import make_user
    user = make_user(db)
    assert _run_inbound(db, user, "what's due", monkeypatch)[0] is False
    monkeypatch.setattr(config, "CANVAS_ENABLED", False)
    assert _run_inbound(db, user, TOKEN, monkeypatch)[0] is False
    assert sms_capture == [] and _row(user.id) is None


# ─── planner sync ─────────────────────────────────────────────────────────────

def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db, user_id):
    from integrations.canvas import save_token
    save_token(user_id, TOKEN, external_id="424242")


COURSES = [
    {"id": 10, "name": "Discrete Math and Probability Theory", "course_code": "COMPSCI 70"},
    {"id": 11, "name": "Great Ideas in Computer Architecture (Machine Structures)",
     "course_code": "2026 Fall COMPSCI 61C 001 LEC 001 and more"},   # long code → name
]


def _items(now):
    due = now + timedelta(days=3)
    return [
        {"plannable_type": "assignment", "plannable_id": 501, "course_id": 10, "context_name": "CS 70",
         "plannable": {"id": 501, "title": "HW 4", "due_at": _iso(due)},
         "submissions": {"submitted": False, "graded": False, "missing": False}},
        {"plannable_type": "assignment", "plannable_id": 502, "course_id": 10, "context_name": "CS 70",
         "plannable": {"id": 502, "title": "HW 3", "due_at": _iso(now + timedelta(days=1))},
         "submissions": {"submitted": True, "graded": False}},                       # done → gone
        {"plannable_type": "quiz", "plannable_id": 77, "course_id": 11, "context_name": "CS 61C",
         "plannable": {"id": 77, "title": "Quiz 2", "due_at": _iso(now + timedelta(days=5))},
         "submissions": False},
        {"plannable_type": "assignment", "plannable_id": 503, "course_id": 11,
         "plannable": {"id": 503, "title": "Project 1", "due_at": None}, "submissions": False},  # undated
        {"plannable_type": "calendar_event", "plannable_id": 900, "course_id": 11, "context_name": "CS 61C",
         "plannable": {"id": 900, "title": "Midterm 1", "start_at": _iso(now + timedelta(days=8)),
                       "end_at": _iso(now + timedelta(days=8, hours=2)), "all_day": False}},
        {"plannable_type": "planner_note", "plannable_id": 3, "course_id": None,
         "plannable": {"id": 3, "title": "start studying", "todo_date": _iso(now + timedelta(days=2))}},
        {"plannable_type": "planner_note", "plannable_id": 4, "course_id": None,
         "plannable": {"id": 4, "title": "done thing", "todo_date": _iso(now + timedelta(days=2))},
         "planner_override": {"marked_complete": True}},
        {"plannable_type": "announcement", "plannable_id": 55, "course_id": 10,
         "plannable": {"id": 55, "title": "Welcome!"}},
        {"plannable_type": "calendar_event", "plannable_id": 901, "course_id": 11,
         "plannable": {"id": 901, "title": "Semester", "start_at": _iso(now + timedelta(days=1)),
                       "end_at": _iso(now + timedelta(days=80))}},                     # >24h → skip
    ]


def test_sync_maps_planner_items_drops_done_prunes_and_supersedes_feed(db, monkeypatch):
    from tests.factories import make_user
    from integrations import canvas
    from integrations.base import status_line
    from events import upcoming_events, upsert_external_event
    from models import get_session, Event

    user = make_user(db)
    _connect(db, user.id)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # a feed twin + a stale canvas row from a previous pull (HW 3 now submitted)
    upsert_external_event(user.id, source="bcourses", external_id="event-assignment-501",
                          title="due: HW 4", occurred_at=(now + timedelta(days=3)).replace(tzinfo=None))
    upsert_external_event(user.id, source="canvas", external_id="assignment:502",
                          title="due: HW 3 [compsci 70]", occurred_at=(now + timedelta(days=1)).replace(tzinfo=None))
    upsert_external_event(user.id, source="canvas", external_id="assignment:999",
                          title="due: deleted one", occurred_at=(now + timedelta(days=4)).replace(tzinfo=None))

    monkeypatch.setattr(canvas, "list_courses", lambda tok, **kw: COURSES)
    monkeypatch.setattr(canvas, "planner_items", lambda tok, lo, hi, **kw: _items(now))

    res = canvas.sync_user(user.id)
    assert res == {"upserted": 4, "done": 1, "pruned": 1, "superseded": 1}

    ups = {e.title: e for e in upcoming_events(user.id, days=60)}
    assert "due: HW 4 [compsci 70]" in ups                           # short code → lowercased
    assert ups["due: HW 4 [compsci 70]"].source == "canvas"
    assert "due: Quiz 2 [great ideas in computer arch]" in ups      # long code → name[:28]
    assert "Midterm 1 [great ideas in computer arch]" in ups
    assert ups["Midterm 1 [great ideas in computer arch]"].ends_at is not None
    assert "todo: start studying" in ups
    for gone in ("due: HW 3 [compsci 70]", "due: deleted one", "due: HW 4", "todo: done thing",
                 "Semester", "Welcome!"):
        assert gone not in ups
    s = get_session()
    try:
        feed_live = s.query(Event).filter(Event.user_id == user.id, Event.source == "bcourses",
                                          Event.deleted_at.is_(None)).count()
        assert feed_live == 0                                        # one board while the token is live
    finally:
        s.close()

    row = _row(user.id)
    assert row.status == "connected" and row.meta["course_codes"] == ["compsci 70", "great ideas in computer arch"]
    assert status_line(user.id) == "canvas connected (compsci 70 · great ideas in computer arch)"
    assert TOKEN not in status_line(user.id)

    # idempotent
    canvas.sync_user(user.id)
    s = get_session()
    try:
        n = s.query(Event).filter(Event.user_id == user.id, Event.source == "canvas",
                                  Event.deleted_at.is_(None)).count()
        assert n == 4
    finally:
        s.close()


def test_401_revokes_and_feed_sync_resumes(db, monkeypatch):
    from tests.factories import make_user
    from integrations import canvas, bcourses
    from integrations.base import status_line
    user = make_user(db)
    _connect(db, user.id)
    bcourses.save_feed(user.id, FEED)
    assert bcourses.sync_user(user.id) == {"skipped": "canvas token active"}

    def _401(tok, **kw):
        raise canvas.InvalidToken("401")
    monkeypatch.setattr(canvas, "list_courses", _401)
    assert canvas.sync_user(user.id) == {"error": "revoked"}
    row = _row(user.id)
    assert row.status == "revoked" and row.access_token is None
    assert "canvas disconnected" in status_line(user.id)
    assert canvas.is_active(user.id) is False

    monkeypatch.setattr(bcourses, "_fetch_ics", lambda url, **kw: "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    assert bcourses.sync_user(user.id) == {"upserted": 0, "deleted": 0}   # feed is back in charge
    assert canvas.sync_all() == 0                                          # revoked rows aren't polled


def test_transient_failure_counts_then_errors_then_heals(db, monkeypatch):
    from tests.factories import make_user
    from integrations import canvas
    user = make_user(db)
    _connect(db, user.id)

    def _boom(tok, **kw):
        raise RuntimeError("502")
    monkeypatch.setattr(canvas, "list_courses", _boom)
    canvas.sync_user(user.id); canvas.sync_user(user.id)
    assert _row(user.id).status == "connected"
    canvas.sync_user(user.id)
    assert _row(user.id).status == "error"

    monkeypatch.setattr(canvas, "list_courses", lambda tok, **kw: [])
    monkeypatch.setattr(canvas, "planner_items", lambda tok, lo, hi, **kw: [])
    assert canvas.sync_all() == 1
    assert _row(user.id).status == "connected" and _row(user.id).meta["fail_count"] == 0


def test_sync_skips_when_not_connected_or_flag_off(db, monkeypatch):
    from tests.factories import make_user
    from integrations import canvas
    user = make_user(db)
    assert canvas.sync_user(user.id) == {"skipped": "not connected"}
    monkeypatch.setattr(config, "CANVAS_ENABLED", False)
    assert canvas.sync_user(user.id) == {"skipped": "flag off"}
    assert canvas.is_active(user.id) is False


def test_pagination_follows_link_header(monkeypatch):
    from integrations import canvas

    class _R:
        def __init__(self, body, nxt):
            self.status_code, self._b, self.links = 200, body, ({"next": {"url": nxt}} if nxt else {})
        def raise_for_status(self):
            pass
        def json(self):
            return self._b
    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        if "page=2" in url:
            return _R([{"id": 2}], None)
        return _R([{"id": 1}], "https://bcourses.berkeley.edu/api/v1/courses?page=2")
    monkeypatch.setattr(canvas.requests, "get", _get)
    assert [c["id"] for c in canvas.list_courses(TOKEN)] == [1, 2]
    assert calls[0].startswith("https://bcourses.berkeley.edu/api/v1/courses")
    assert all("Bearer" not in u for u in calls)                       # token in header, not URL
