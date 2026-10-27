"""gcal sync — a full pull PRUNES that calendar's window (2026-10-27, user 77: three coach-
created office-hour blocks he MOVED onto his "Class schedule" calendar stayed in the mirror as
ghosts on the main calendar — a move leaves no status=cancelled tombstone, and nothing pruned).

Covers:
  - after a full pull, active rows for THAT calendar inside the pulled window that the pull
    didn't return are soft-deleted; rows on another calendar, rows outside the window, and
    rows the pull did return are untouched;
  - an empty pull does NOT prune (a transient empty 200 must not wipe the mirror);
  - an incremental pull (sync token) does not prune — tombstones cover it;
  - prune's calendar prefix is LIKE-escaped (calendar ids carry '_' and '%' is possible).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

import config


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    yield


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connected(db, user_id):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=user_id, provider="gcal", status="connected", meta={}, external_id="nau@gmail.com"))
        s.commit()
    finally:
        s.close()


def _seed(user_id, ext, title, start):
    from events import upsert_external_event
    return upsert_external_event(user_id, source="gcal", external_id=ext, title=title,
                                 occurred_at=start.astimezone(timezone.utc).replace(tzinfo=None),
                                 ends_at=(start + timedelta(hours=1)).astimezone(timezone.utc).replace(tzinfo=None))


def _state(user_id):
    from models import get_session, Event
    s = get_session()
    try:
        return {e.external_id: (e.deleted_at is not None) for e in s.query(Event).filter_by(user_id=user_id).all()}
    finally:
        s.close()


MAIN = "nau@gmail.com"
CLASS = "c_5i95_class@group.calendar.google.com"


def test_full_pull_prunes_only_that_calendars_window(db, monkeypatch):
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    u = make_user(db)
    _connected(db, u.id)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": MAIN}, {"id": CLASS}])

    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=3)
    far = now + timedelta(days=90)                      # outside the 60-day pull window
    kept = _seed(u.id, f"{MAIN}:keep1", "phys lec", soon)
    ghost = _seed(u.id, f"{MAIN}:ghost1", "INDENG OH (moved away)", soon + timedelta(hours=2))
    other_cal = _seed(u.id, f"{CLASS}:oh_1", "INDENG OH", soon + timedelta(hours=2))
    outside = _seed(u.id, f"{MAIN}:far1", "finals", far)
    past = _seed(u.id, f"{MAIN}:past1", "yesterday", now - timedelta(days=1))

    def _events(tok, cid, sync_token=None, time_min=None, time_max=None):
        assert sync_token is None                       # a full pull (no token stored)
        if cid == MAIN:
            return ([{"id": "keep1", "status": "confirmed", "summary": "phys lec",
                      "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=1))}}], None)
        return ([{"id": "oh_1", "status": "confirmed", "summary": "INDENG OH",
                  "start": {"dateTime": _iso(soon + timedelta(hours=2))},
                  "end": {"dateTime": _iso(soon + timedelta(hours=3))}}], None)
    monkeypatch.setattr(gcal, "list_events", _events)

    res = gcal_sync.sync_user(u.id)
    assert res["deleted"] == 1
    st = _state(u.id)
    assert st[f"{MAIN}:ghost1"] is True                 # the moved-away copy is gone
    assert st[f"{MAIN}:keep1"] is False
    assert st[f"{CLASS}:oh_1"] is False                 # another calendar: judged only by its own pull
    assert st[f"{MAIN}:far1"] is False                  # outside the pulled window: not re-fetched, not judged
    assert st[f"{MAIN}:past1"] is False                 # before the window


def test_empty_pull_does_not_prune(db, monkeypatch):
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    u = make_user(db)
    _connected(db, u.id)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": MAIN}])
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    _seed(u.id, f"{MAIN}:a", "a", soon)
    _seed(u.id, f"{MAIN}:b", "b", soon + timedelta(hours=1))
    monkeypatch.setattr(gcal, "list_events", lambda tok, cid, **kw: ([], None))
    res = gcal_sync.sync_user(u.id)
    assert res["deleted"] == 0 and res["pruned"] == 0
    assert all(v is False for v in _state(u.id).values())


def test_incremental_pull_does_not_prune(db, monkeypatch):
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    from models import get_session, Integration
    u = make_user(db)
    _connected(db, u.id)
    s = get_session()
    try:
        i = s.query(Integration).filter_by(user_id=u.id, provider="gcal").one()
        i.meta = {"gcal_sync": {MAIN: "TOKEN1"}}
        s.commit()
    finally:
        s.close()
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": MAIN}])
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    _seed(u.id, f"{MAIN}:a", "a", soon)
    seen = []
    monkeypatch.setattr(gcal, "list_events", lambda tok, cid, **kw: seen.append(kw) or (
        [{"id": "new1", "status": "confirmed", "summary": "new", "start": {"dateTime": _iso(soon)},
          "end": {"dateTime": _iso(soon + timedelta(hours=1))}}], "TOKEN2"))
    res = gcal_sync.sync_user(u.id)
    assert seen[0].get("sync_token") == "TOKEN1"
    assert res["pruned"] == 0 and _state(u.id)[f"{MAIN}:a"] is False


def test_prune_prefix_is_like_escaped(db):
    from tests.factories import make_user
    from events import prune_external_events
    u = make_user(db)
    soon = datetime.now(timezone.utc) + timedelta(days=3)
    lo = (soon - timedelta(days=1)).replace(tzinfo=None)
    hi = (soon + timedelta(days=1)).replace(tzinfo=None)
    a = _seed(u.id, "c_ab@group:1", "a", soon)          # the calendar we pulled ('_' must be literal)
    b = _seed(u.id, "cXab@group:1", "b", soon)          # would match 'c_ab' if '_' were a wildcard
    c = _seed(u.id, "100%:1", "c", soon)
    d = _seed(u.id, "100x:1", "d", soon)
    assert prune_external_events(u.id, source="gcal", keep=set(), lo=lo, hi=hi, external_prefix="c_ab@group:") == 1
    assert prune_external_events(u.id, source="gcal", keep=set(), lo=lo, hi=hi, external_prefix="100%:") == 1
    st = _state(u.id)
    assert st["c_ab@group:1"] is True and st["cXab@group:1"] is False
    assert st["100%:1"] is True and st["100x:1"] is False
