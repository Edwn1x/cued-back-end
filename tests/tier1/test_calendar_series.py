"""Google Calendar — REPEATING events + ALL-DAY + bulk delete (founder 2026-10-27: "it should
be able to control everything in google calendar"). Sibling of test_calendar_writeback.py
(create) and test_calendar_edit.py (move/delete one). Google mocked (no network).

Covers:
  - repeat rules: day parsing, snap-to-first-matching-day, UNTIL in UTC, weekdays/daily;
  - create with repeat POSTs recurrence + timeZone nodes, mirrors the REAL instances
    (events.instances) with series_id, keyed like the sync;
  - create all_day POSTs {date} nodes with an exclusive end;
  - delete of a repeating event with no scope ASKS (nothing staged); scope='series' confirms
    then DELETEs the master once and drops every local occurrence (stored series_id AND the
    legacy '<master>_<ts>' prefix); other series untouched;
  - scope='all_matching' counts series once, skips bCourses, deletes masters + one-offs after
    one confirm;
  - move scope='series' GETs the master, PATCHes new time / BYDAY with timeZone, re-mirrors;
  - series_id fallback derivation from the instance id; sync carries recurringEventId;
  - lookup_events / UPCOMING mark repeating rows.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import pytest

import config

TZ = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
    yield


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _token(monkeypatch, value="AT"):
    from integrations import base
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: value)


def _connected(db, user_id, external_id="nau@gmail.com"):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=user_id, provider="gcal", status="connected", meta={}, external_id=external_id))
        s.commit()
    finally:
        s.close()


def _local(days_ahead, hour, minute=0):
    loc = (datetime.now(ZoneInfo(TZ)) + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return loc.astimezone(timezone.utc).replace(tzinfo=None)


def _inst_id(master, start_utc):
    return f"{master}_{start_utc.strftime('%Y%m%dT%H%M%SZ')}"


def _series(user_id, title, master, first_days_ahead, hour, *, n=4, step=7, cal="nau@gmail.com", stored=True):
    """n weekly occurrences; stored=False mimics rows synced before series_id existed."""
    from events import upsert_external_events
    rows = []
    for k in range(n):
        s = _local(first_days_ahead + k * step, hour)
        rows.append({"external_id": f"{cal}:{_inst_id(master, s)}", "title": title, "occurred_at": s,
                     "ends_at": s + timedelta(hours=1), "all_day": False, "series_id": master if stored else None})
    upsert_external_events(user_id, source="gcal", rows=rows)
    return rows


def _single(user_id, title, days_ahead, hour, *, source="gcal", gid=None):
    from events import upsert_external_event
    s = _local(days_ahead, hour)
    ext = f"nau@gmail.com:{gid or title.replace(' ', '')}" if source == "gcal" else f"{source}:{title}"
    return upsert_external_event(user_id, source=source, external_id=ext, title=title, occurred_at=s,
                                 ends_at=s + timedelta(hours=1))


def _active_titles(user_id):
    from models import get_session, Event, active
    s = get_session()
    try:
        return sorted(e.title for e in active(s, Event, user_id=user_id).all())
    finally:
        s.close()


def _google(monkeypatch, *, master=None, instances=None):
    """Route the gcal client's HTTP: records every call; GET .../instances → `instances`,
    GET master → `master`, POST → created id 'ser1', PATCH → echo, DELETE → 204."""
    from integrations import gcal
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url, params))
        if url.endswith("/instances"):
            return _Resp(200, {"items": instances or []})
        return _Resp(200, master or {})

    def _post(url, headers=None, json=None, timeout=None):
        calls.append(("POST", url, json))
        return _Resp(200, {"id": "ser1", **(json or {})})

    def _patch(url, headers=None, json=None, timeout=None):
        calls.append(("PATCH", url, json))
        return _Resp(200, {**(master or {}), **(json or {})})

    def _delete(url, headers=None, timeout=None):
        calls.append(("DELETE", url, None))
        return _Resp(204)

    monkeypatch.setattr(gcal.requests, "get", _get)
    monkeypatch.setattr(gcal.requests, "post", _post)
    monkeypatch.setattr(gcal.requests, "patch", _patch)
    monkeypatch.setattr(gcal.requests, "delete", _delete)
    return calls


def _instances_for(master, first_days_ahead, hour, codes_days_ahead=(0, 2, 4), weeks=2):
    out = []
    for w in range(weeks):
        for d in codes_days_ahead:
            s = _local(first_days_ahead + d + 7 * w, hour)
            out.append({"id": _inst_id(master, s), "recurringEventId": master, "summary": "gym",
                        "start": {"dateTime": s.replace(tzinfo=timezone.utc).isoformat()},
                        "end": {"dateTime": (s + timedelta(hours=1)).replace(tzinfo=timezone.utc).isoformat()}})
    return out


# ─── repeat rules ────────────────────────────────────────────────────────────

def test_parse_days_accepts_names_lists_and_letters():
    from agent_tools import _parse_days
    assert _parse_days(["mon", "wed", "fri"]) == ["MO", "WE", "FR"]
    assert _parse_days("tue/thu") == ["TU", "TH"]
    assert _parse_days("Monday, Wednesday and Friday") == ["MO", "WE", "FR"]
    assert _parse_days("MWF") == ["MO", "WE", "FR"]
    assert _parse_days(["fri", "mon"]) == ["MO", "FR"]          # week order
    assert _parse_days(["blursday"]) == []


def test_repeat_rule_snaps_start_and_puts_until_in_utc():
    from agent_tools import _repeat_rule
    tz = ZoneInfo(TZ)
    sat = date(2026, 10, 31)                                   # a Saturday
    rule, human, until, first, codes = _repeat_rule("weekly", ["mon", "wed"], sat, "2026-12-12", tz, all_day=False)
    assert first == date(2026, 11, 2) and codes == ["MO", "WE"]
    assert rule.startswith("RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=2026121")   # local 23:59:59 on 12-12 → UTC next day
    assert rule.endswith("Z") and human == "every mon/wed" and until == "through Dec 12"
    rule, human, until, first, codes = _repeat_rule("weekdays", None, sat, None, tz, all_day=False)
    assert first == date(2026, 11, 2) and codes == ["MO", "TU", "WE", "TH", "FR"] and until == "(no end date)"
    rule, human, until, first, _ = _repeat_rule("daily", None, sat, "2026-11-05", tz, all_day=True)
    assert rule == "RRULE:FREQ=DAILY;UNTIL=20261105" and first == sat
    rule, human, until, first, _ = _repeat_rule("weekly", None, sat, None, tz, all_day=False)
    assert "BYDAY=SA" in rule                                  # default = the start day
    assert _repeat_rule(None, None, sat, None, tz, all_day=False)[0] is None
    with pytest.raises(ValueError):
        _repeat_rule("monthly", None, sat, None, tz, all_day=False)
    with pytest.raises(ValueError):
        _repeat_rule("daily", None, sat, "2026-10-01", tz, all_day=False)   # until before start


def test_rewrite_byday_and_describe():
    from agent_tools import _rewrite_byday, _describe_rrule
    rec = ["EXDATE;TZID=America/Los_Angeles:20261110T170000", "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261209T075959Z"]
    out = _rewrite_byday(rec, ["WE", "FR"])
    assert out[0] == rec[0]
    assert out[1] == "RRULE:FREQ=WEEKLY;UNTIL=20261209T075959Z;BYDAY=WE,FR"
    assert _describe_rrule(rec) == "every tue through Dec 9"
    assert _describe_rrule(["RRULE:FREQ=DAILY;COUNT=5"]) == "every day (5x)"
    assert _describe_rrule(None) == "repeating"


def test_series_id_falls_back_to_instance_id_pattern():
    from agent_tools import _series_id_of
    assert _series_id_of("m1", "x:whatever") == "m1"
    assert _series_id_of(None, "c_abc@group.calendar.google.com:3qaj62_20261028T000000Z") == "3qaj62"
    assert _series_id_of(None, "nau@gmail.com:3qaj62_20261028") == "3qaj62"          # all-day instance
    assert _series_id_of(None, "nau@gmail.com:plainid") is None


def test_sync_row_carries_recurring_event_id(db):
    from integrations.gcal_sync import event_row
    from events import upsert_external_events
    from tests.factories import make_user
    from models import get_session, Event
    u = make_user(db, user_timezone=TZ)
    ge = {"id": "m1_20261028T230000Z", "recurringEventId": "m1", "summary": "ELENG 105 DIS",
          "start": {"dateTime": "2026-10-28T16:00:00-07:00"}, "end": {"dateTime": "2026-10-28T17:00:00-07:00"}}
    row = event_row("c@group", ge, ZoneInfo(TZ))
    assert row["series_id"] == "m1" and row["external_id"] == "c@group:m1_20261028T230000Z"
    upsert_external_events(u.id, source="gcal", rows=[row])
    s = get_session()
    try:
        assert s.query(Event).filter_by(user_id=u.id).one().series_id == "m1"
    finally:
        s.close()
    # a second pass that only adds the series_id to an existing row counts as an update, not unchanged
    row2 = dict(row, series_id=None)
    c = upsert_external_events(u.id, source="gcal", rows=[row2])
    assert c["updated"] == 1


# ─── create: repeating + all-day ─────────────────────────────────────────────

def test_create_repeating_posts_rrule_and_mirrors_real_instances(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from models import get_session, Event, active
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    # pick the next Monday as "date" so the snap is deterministic
    today = datetime.now(ZoneInfo(TZ)).date()
    mon = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    calls = _google(monkeypatch, instances=_instances_for("ser1", (mon - today).days, 16))

    args = {"summary": "gym", "starts_at": "16:00", "duration_minutes": 60, "date": mon.isoformat(),
            "repeat": "weekly", "repeat_days": ["mon", "wed", "fri"], "repeat_until": (mon + timedelta(days=45)).isoformat()}
    staged = handle_create_calendar_event(u.id, args, message_id="m1")
    assert staged.startswith("not created yet") and "every mon/wed/fri 4:00pm–5:00pm through" in staged
    assert calls == []
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: added 'gym' every mon/wed/fri 4:00pm–5:00pm"), out
    assert "6 on the calendar" in out

    post = [c for c in calls if c[0] == "POST"][0]
    body = post[2]
    assert body["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=")
    assert body["start"]["timeZone"] == TZ and body["end"]["timeZone"] == TZ
    assert body["start"]["dateTime"].startswith(mon.isoformat() + "T16:00:00")
    inst_call = [c for c in calls if c[0] == "GET" and c[1].endswith("/instances")][0]
    assert "/calendars/nau%40gmail.com/events/ser1/instances" in inst_call[1]

    s = get_session()
    try:
        rows = active(s, Event, user_id=u.id).order_by(Event.occurred_at).all()
        assert len(rows) == 6
        assert all(r.series_id == "ser1" for r in rows)
        assert rows[0].external_id.startswith("nau@gmail.com:ser1_")       # keyed like the sync
        assert all(r.title == "gym" for r in rows)
    finally:
        s.close()


def test_create_all_day_posts_date_nodes(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from models import get_session, Event, active
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    calls = _google(monkeypatch)
    args = {"summary": "flight home", "all_day": True, "date": "tomorrow"}
    staged = handle_create_calendar_event(u.id, args, message_id="m1")
    assert "(all day)" in staged and "am or" not in staged
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: added 'flight home'") and "(all day)" in out
    body = [c for c in calls if c[0] == "POST"][0][2]
    tmr = (datetime.now(ZoneInfo(TZ)) + timedelta(days=1)).date()
    assert body["start"] == {"date": tmr.isoformat()}
    assert body["end"] == {"date": (tmr + timedelta(days=1)).isoformat()}
    s = get_session()
    try:
        e = active(s, Event, user_id=u.id).one()
        assert e.all_day and e.series_id is None
    finally:
        s.close()


def test_create_without_time_and_not_all_day_is_an_error(db):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    u = make_user(db, user_timezone=TZ)
    assert handle_create_calendar_event(u.id, {"summary": "x"}).startswith("error: starts_at required")


# ─── delete: series ──────────────────────────────────────────────────────────

def test_delete_repeating_without_scope_asks_and_stages_nothing(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event, _get_pending_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    calls = _google(monkeypatch)
    out = handle_delete_calendar_event(u.id, {"query": "eleng 105 dis"}, message_id="m1")
    assert "repeating event" in out and "scope='one' or scope='series'" in out
    assert _get_pending_calendar_event(u.id) is None and calls == []
    # a confirmed=true with no scope is STILL just the question — never a write
    out = handle_delete_calendar_event(u.id, {"query": "eleng 105 dis", "confirmed": True}, message_id="m2")
    assert "repeating event" in out and calls == []


def test_delete_series_deletes_master_once_and_drops_every_local_occurrence(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17, n=3, stored=True)
    _series(u.id, "ELENG 105 DIS", "dis1", 22, 17, n=2, stored=False)     # legacy rows: no series_id column value
    _series(u.id, "ELENG 105 LEC", "lec1", 2, 10, n=3)
    calls = _google(monkeypatch)
    staged = handle_delete_calendar_event(u.id, {"query": "eleng 105 dis", "scope": "series"}, message_id="m1")
    assert staged.startswith("not deleted yet") and "EVERY occurrence of 'ELENG 105 DIS'" in staged
    out = handle_delete_calendar_event(u.id, {"query": "eleng 105 dis", "scope": "series", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: deleted every occurrence of 'ELENG 105 DIS'") and "(5 were on the calendar" in out
    dels = [c for c in calls if c[0] == "DELETE"]
    assert len(dels) == 1 and dels[0][1].endswith("/calendars/nau%40gmail.com/events/dis1")
    assert _active_titles(u.id) == ["ELENG 105 LEC"] * 3


def test_delete_one_occurrence_of_a_series_only_touches_that_instance(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    rows = _series(u.id, "ELENG 105 DIS", "dis1", 1, 17, n=3)
    calls = _google(monkeypatch)
    handle_delete_calendar_event(u.id, {"query": "eleng", "date": "tomorrow", "scope": "one"}, message_id="m1")
    out = handle_delete_calendar_event(u.id, {"query": "eleng", "date": "tomorrow", "scope": "one", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: deleted 'ELENG 105 DIS'")
    dels = [c for c in calls if c[0] == "DELETE"]
    inst = rows[0]["external_id"].split(":", 1)[1]
    assert len(dels) == 1 and dels[0][1].endswith("/events/" + inst.replace(":", "%3A"))
    assert _active_titles(u.id) == ["ELENG 105 DIS"] * 2


def test_delete_all_matching_counts_series_once_skips_bcourses_and_confirms_once(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event, _get_pending_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17, n=4)
    _series(u.id, "ELENG 105 LEC", "lec1", 2, 10, n=4)
    _single(u.id, "ELENG 105 midterm", 9, 19, gid="mid1")
    _single(u.id, "ELENG 105 HW3 due", 5, 23, source="bcourses")
    _single(u.id, "gym", 1, 16, gid="gym1")
    calls = _google(monkeypatch)

    staged = handle_delete_calendar_event(u.id, {"query": "eleng 105", "scope": "all_matching"}, message_id="m1")
    assert staged.startswith("not deleted yet"), staged
    assert "2 repeating events (every occurrence)" in staged and "1 one-off" in staged
    assert "skipping 1 bCourses/Canvas item" in staged
    assert calls == []
    assert _get_pending_calendar_event(u.id)["kind"] == "delete_all"

    out = handle_delete_calendar_event(u.id, {"query": "eleng 105", "scope": "all_matching", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: deleted everything matching 'eleng 105'"), out
    assert "3 events (9 occurrences" in out and "left 1 bCourses/Canvas item alone" in out
    dels = sorted(c[1].rsplit("/", 1)[1] for c in calls if c[0] == "DELETE")
    assert dels == ["dis1", "lec1", "mid1"]                      # each series ONCE at its master
    assert _active_titles(u.id) == ["ELENG 105 HW3 due", "gym"]
    assert _get_pending_calendar_event(u.id) is None


def test_delete_all_matching_requires_query_and_is_honest_when_only_course_items_match(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    u = make_user(db, user_timezone=TZ)
    assert handle_delete_calendar_event(u.id, {"scope": "all_matching"}).startswith("error")
    _single(u.id, "ELENG 105 HW3 due", 5, 23, source="bcourses")
    out = handle_delete_calendar_event(u.id, {"scope": "all_matching", "query": "eleng"})
    assert out.startswith("nothing deletable matches") and "bCourses" in out
    assert _active_titles(u.id) == ["ELENG 105 HW3 due"]


# ─── move: series ────────────────────────────────────────────────────────────

def _master(first_days_ahead, hour, byday="TU"):
    s = _local(first_days_ahead, hour)
    loc = s.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ))
    return {"id": "dis1", "summary": "ELENG 105 DIS",
            "start": {"dateTime": loc.isoformat(), "timeZone": TZ},
            "end": {"dateTime": (loc + timedelta(hours=1)).isoformat(), "timeZone": TZ},
            "recurrence": [f"RRULE:FREQ=WEEKLY;BYDAY={byday};UNTIL=20261209T075959Z"]}


def test_move_repeating_without_scope_asks(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    calls = _google(monkeypatch, master=_master(-20, 17))
    out = handle_move_calendar_event(u.id, {"query": "eleng", "new_starts_at": "18:00"}, message_id="m1")
    assert "repeating event" in out and "scope='one' or scope='series'" in out and calls == []


def test_move_series_patches_master_time_and_remirrors(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    from models import get_session, Event, active
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17, n=4)
    master = _master(-20, 17)
    new_inst = [{"id": _inst_id("dis1", _local(1 + 7 * k, 18)), "recurringEventId": "dis1", "summary": "ELENG 105 DIS",
                 "start": {"dateTime": _local(1 + 7 * k, 18).replace(tzinfo=timezone.utc).isoformat()},
                 "end": {"dateTime": (_local(1 + 7 * k, 18) + timedelta(hours=1)).replace(tzinfo=timezone.utc).isoformat()}}
                for k in range(5)]
    calls = _google(monkeypatch, master=master, instances=new_inst)

    args = {"query": "eleng", "scope": "series", "new_starts_at": "18:00"}
    staged = handle_move_calendar_event(u.id, args, message_id="m1")
    assert staged.startswith("not moved yet") and "EVERY occurrence of 'ELENG 105 DIS' (every tue through Dec 9)" in staged
    assert "5:00pm–6:00pm → 6:00pm–7:00pm" in staged and "6am or 6pm?" in staged
    assert [c for c in calls if c[0] == "PATCH"] == []
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: every 'ELENG 105 DIS' is now 6:00pm–7:00pm every tue through Dec 9"), out
    assert "(5 over the next two months)" in out

    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[1].endswith("/events/dis1")
    body = patch[2]
    assert set(body) == {"start", "end"}                          # no title/recurrence change sent
    assert body["start"]["timeZone"] == TZ
    m_loc = datetime.fromisoformat(master["start"]["dateTime"])
    assert body["start"]["dateTime"].startswith(m_loc.date().isoformat() + "T18:00:00")   # same first date, new time
    s = get_session()
    try:
        rows = active(s, Event, user_id=u.id).order_by(Event.occurred_at).all()
        assert len(rows) == 5 and all(r.series_id == "dis1" for r in rows)
        assert all(r.occurred_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ)).hour == 18 for r in rows)
    finally:
        s.close()


def test_move_series_new_days_rewrites_byday_and_snaps_master_start(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    # master starts on a Tuesday 20 days ago → find the actual Tuesday
    today = datetime.now(ZoneInfo(TZ)).date()
    back = today - timedelta(days=20)
    tue = back + timedelta(days=(1 - back.weekday()) % 7)
    master = _master((tue - today).days, 17, byday="TU")
    calls = _google(monkeypatch, master=master, instances=[])
    args = {"query": "eleng", "scope": "series", "new_repeat_days": ["wed", "fri"]}
    staged = handle_move_calendar_event(u.id, args, message_id="m1")
    assert "every tue through Dec 9 → every wed/fri through Dec 9" in staged, staged
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: every 'ELENG 105 DIS' is now 5:00pm–6:00pm every wed/fri through Dec 9"), out
    body = [c for c in calls if c[0] == "PATCH"][0][2]
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;UNTIL=20261209T075959Z;BYDAY=WE,FR"]
    assert body["start"]["dateTime"].startswith((tue + timedelta(days=1)).isoformat() + "T17:00:00")   # Tue → first Wed
    assert body["start"]["timeZone"] == TZ


def test_move_series_rejects_new_date_and_nothing_to_change(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    _google(monkeypatch, master=_master(-20, 17))
    out = handle_move_calendar_event(u.id, {"query": "eleng", "scope": "series", "new_date": "tomorrow"}, message_id="m1")
    assert "not new_date" in out
    out = handle_move_calendar_event(u.id, {"query": "eleng", "scope": "series", "new_starts_at": "17:00"}, message_id="m1")
    assert out.startswith("nothing to change")


def test_move_series_readonly_grant_is_honest(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    from integrations import gcal
    _token(monkeypatch)
    u = make_user(db, user_timezone=TZ)
    _connected(db, u.id)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    _google(monkeypatch, master=_master(-20, 17))
    monkeypatch.setattr(gcal.requests, "patch", lambda *a, **k: _Resp(403, {"error": {
        "message": "Request had insufficient authentication scopes.", "errors": [{"reason": "insufficientPermissions"}]}}))
    args = {"query": "eleng", "scope": "series", "new_starts_at": "18:00"}
    handle_move_calendar_event(u.id, args, message_id="m1")
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert "read-only" in out and not out.startswith("ok")
    assert _active_titles(u.id) == ["ELENG 105 DIS"] * 4           # mirror untouched


# ─── surfaces ────────────────────────────────────────────────────────────────

def test_lookup_and_upcoming_mark_repeating(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_tools import handle_lookup_events
    monkeypatch.setattr(config, "LOOKUP_EVENTS_TOOL_ENABLED", True)
    u = make_user(db, user_timezone=TZ, onboarding_step=3)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17, n=2)
    _single(u.id, "dentist", 2, 15, gid="d1")
    out = handle_lookup_events(u.id, {"query": "eleng"})
    assert out.count("(repeats)") == 2
    assert "(repeats)" not in handle_lookup_events(u.id, {"query": "dentist"})

    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(system=kw.get("system")) or "hey")
    from agent_loop import run_agent_loop
    run_agent_loop(u, "yo", "freeform")
    sys_txt = seen.get("system")
    sys_txt = sys_txt if isinstance(sys_txt, str) else "\n".join(b.get("text", "") for b in (sys_txt or []) if isinstance(b, dict))
    assert "ELENG 105 DIS" in sys_txt and "(repeats)" in sys_txt
    assert "dentist" in sys_txt and "dentist — " in sys_txt
