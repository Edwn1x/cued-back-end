"""
Campus libraries (integrations/campus_libraries.py + the find_study_space /
send_study_room_link tools). HTTP is MOCKED throughout: the grid fixture mirrors the
LibCal `slots[]` shape read live 2026-10-08 (booked = className s-lc-eq-checkout, free =
no className), the hours fixture is a trimmed copy of lib.berkeley.edu/hours.

Pinned here: hours-text parsing incl. overnight closes and "24 hours", the open-at
check, the need filters, free-slot → run merging (booked slots break a run, unknown
itemIds are never offered), capacity/duration/start clipping, the failure envelope
(flag off / HTTP error / non-JSON → None, 5 failures → stop for the day, stale hours
beat nothing), the tool's honest lines, the sleep-window note, the link bubble, and
the loop/prompt/capability wiring.
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pytest

import config
from integrations import campus_libraries as cl
from tests.factories import make_user

DAY = date(2026, 3, 10)                      # a Tuesday
NOW = datetime(2026, 3, 10, 14, 20)          # campus-local naive
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures", "library_hours.html")


def _slot(eid, h0, h1, booked=False, day=DAY):
    s = {"start": f"{day.isoformat()} {h0:02d}:00:00", "end": f"{day.isoformat()} {h1:02d}:00:00",
         "itemId": eid, "checksum": "x"}
    if booked:
        s["className"] = cl.BOOKED_CLASS
    return s


def _grid():
    return [
        _slot(62878, 14, 15), _slot(62878, 15, 16), _slot(62878, 16, 17, booked=True),
        _slot(62878, 17, 18), _slot(62878, 18, 19),                       # Egret (4)
        _slot(62884, 15, 16), _slot(62884, 16, 17), _slot(62884, 17, 18),  # Room 415 (8)
        _slot(62886, 14, 15),                                              # Hemlock (4)
        _slot(101038, 14, 15), _slot(101038, 15, 16),                      # hidden item, not in directory
    ]


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    cl.reset_state()
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    monkeypatch.setattr(cl, "_now_local", lambda: NOW)
    yield
    cl.reset_state()


@pytest.fixture
def hours_html():
    return open(FIXTURE, encoding="utf-8").read()


# ─── hours page ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("9 a.m.-11 p.m.", (540, 1380)),
    ("9 a.m.-2 a.m.", (540, 1560)),          # overnight close expressed past midnight
    ("1 p.m.-4:45 p.m.", (780, 1005)),
    ("24 hours", (0, 1440)),
    ("", (None, None)),
    ("Closed", (None, None)),
])
def test_hours_text_parsing(text, expected):
    assert cl.parse_hours_text(text) == expected


def test_hours_page_parses_names_hours_tags_and_maps(hours_html):
    libs = cl.parse_hours_page(hours_html)
    assert set(libs) == {"Moffitt Library", "Doe Library", "Main (Gardner) Stacks",
                         "Engineering & Mathematical Sciences Library"}
    mof = libs["Moffitt Library"]
    assert mof.is_24h and mof.short == "Moffitt" and "Snacks allowed" in mof.tags
    assert mof.maps_url and mof.maps_url.startswith("https://")
    stacks = libs["Main (Gardner) Stacks"]
    assert (stacks.open_min, stacks.close_min) == (540, 1560)
    assert stacks.is_open_at(datetime(2026, 3, 10, 1, 0))      # 1am: still inside 9am–2am
    assert not stacks.is_open_at(datetime(2026, 3, 10, 3, 0))
    assert not stacks.is_open_at(datetime(2026, 3, 10, 8, 30))  # not open yet
    assert not libs["Doe Library"].is_open_at(datetime(2026, 3, 10, 1, 0))   # 8am–9pm never spills
    assert not libs["Doe Library"].is_open_at(datetime(2026, 3, 10, 21, 30))   # closes 9pm
    assert "Snacks allowed" not in libs["Doe Library"].tags


def test_open_libraries_filters_by_time_and_need(hours_html):
    hours = cl.parse_hours_page(hours_html)
    late = cl.open_libraries(DAY, datetime(2026, 3, 10, 23, 30), need="late", hours=hours)
    assert [l.short for l in late] == ["Moffitt", "Main Stacks"]       # 24h first, then latest close
    snacks = cl.open_libraries(DAY, datetime(2026, 3, 10, 15, 0), need="snacks", hours=hours)
    assert {l.short for l in snacks} == {"Moffitt", "Engineering Library"}
    tech = cl.open_libraries(DAY, datetime(2026, 3, 10, 15, 0), need="tech", hours=hours)
    assert {l.short for l in tech} == {"Moffitt", "Engineering Library"}
    everything = cl.open_libraries(DAY, datetime(2026, 3, 10, 15, 0), hours=hours)
    assert len(everything) == 4
    assert "open 24 hours" in cl.format_open(everything, at_local=datetime(2026, 3, 10, 15, 0))


def test_fetch_hours_caches_asks_for_the_day_and_fails_open(monkeypatch, hours_html):
    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        return _Resp(200, text=hours_html)

    monkeypatch.setattr(cl.requests, "get", _get)
    assert cl.fetch_hours(DAY)["Moffitt Library"].is_24h
    assert calls[0][1]["hours_date_select"] == "2026-03-10"
    assert cl.fetch_hours(DAY) and len(calls) == 1                  # cached
    # a later outage returns the stale day rather than nothing
    monkeypatch.setattr(cl.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_HOURS_TTL_S", -1)
    assert cl.fetch_hours(DAY) is not None
    assert cl.fetch_hours(date(2026, 3, 11)) is None                # never fetched → None
    # a redesign that parses thin is a None, not a 2-library answer
    cl.reset_state()
    monkeypatch.setattr(cl.requests, "get", lambda *a, **k: _Resp(200, text="<html><li class='x'></li></html>"))
    assert cl.fetch_hours(DAY) is None
    # flag off → no network
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", False)
    assert cl.fetch_hours(DAY) is None


# ─── availability grid ───────────────────────────────────────────────────────

def test_merge_runs_breaks_on_booked_and_skips_unknown_rooms():
    runs = {(r.room.eid, r.start.hour, r.end.hour) for r in cl.merge_runs(_grid())}
    assert runs == {(62878, 14, 16), (62878, 17, 19), (62884, 15, 18), (62886, 14, 15)}


def test_open_runs_clips_to_start_and_ranks_best_fit():
    runs = cl.open_runs(8868, DAY, start=NOW, min_minutes=60, min_capacity=1, now=NOW, slots=_grid())
    # 14:20 rounds up to 15:00; Hemlock's 14–15 slot has nothing left and drops out
    assert [(r.room.name, r.start.hour, r.end.hour) for r in runs] == [
        ("Egret, Room 409", 15, 16), ("Room 415", 15, 18), ("Egret, Room 409", 17, 19)]
    big = cl.open_runs(8868, DAY, start=NOW, min_capacity=5, now=NOW, slots=_grid())
    assert [r.room.name for r in big] == ["Room 415"]
    long = cl.open_runs(8868, DAY, start=NOW, min_minutes=120, now=NOW, slots=_grid())
    assert [(r.room.name, r.start.hour) for r in long] == [("Room 415", 15), ("Egret, Room 409", 17)]
    later = cl.open_runs(8868, DAY, start=datetime(2026, 3, 10, 17, 0), now=NOW, slots=_grid())
    assert [(r.room.name, r.start.hour, r.end.hour) for r in later] == [("Egret, Room 409", 17, 19), ("Room 415", 17, 18)]
    text = cl.format_runs(runs, day=DAY, today=DAY)
    assert text.startswith("Moffitt rooms today:") and "Egret, Room 409 (4th floor, fits 4) free 3pm–4pm [eid 62878]" in text
    assert cl.format_runs([], day=DAY, today=DAY) == "no rooms free for that window"
    # LibCal ends the last slot at 00:00 next day (or 23:59): say midnight, not 12am / 11:59pm
    edge = [{"start": "2026-03-10 22:00:00", "end": "2026-03-10 23:00:00", "itemId": 62891, "checksum": "x"},
            {"start": "2026-03-10 23:00:00", "end": "2026-03-11 00:00:00", "itemId": 62891, "checksum": "x"},
            {"start": "2026-03-10 23:00:00", "end": "2026-03-10 23:59:00", "itemId": 62878, "checksum": "x"}]
    late = cl.open_runs(8868, DAY, start=datetime(2026, 3, 10, 22, 0), min_minutes=30, now=NOW, slots=edge)
    text = cl.format_runs(late, day=DAY, today=DAY)
    assert "Palm, Room 517 (5th floor, fits 4) free 10pm–midnight" in text and "11pm–midnight" in text


def test_fetch_grid_posts_the_libcal_form_and_caches(monkeypatch):
    calls = []

    def _post(url, data=None, timeout=None, headers=None):
        calls.append(dict(data))
        return _Resp(200, payload={"slots": _grid(), "bookings": [], "windowEnd": False})

    monkeypatch.setattr(cl.requests, "post", _post)
    assert len(cl.fetch_grid(8868, DAY)) == len(_grid())
    assert calls[0]["lid"] == 8868 and calls[0]["start"] == "2026-03-10" and calls[0]["end"] == "2026-03-11"
    cl.fetch_grid(8868, DAY)
    assert len(calls) == 1
    assert cl.open_runs(8868, DAY, now=NOW) is not None


def test_fetch_grid_failure_envelope(monkeypatch):
    monkeypatch.setattr(cl.requests, "post", lambda *a, **k: _Resp(200, payload=None, text="<html>Whoops"))
    assert cl.fetch_grid(8868, DAY) is None                         # non-JSON (outside booking window)
    monkeypatch.setattr(cl.requests, "post", lambda *a, **k: _Resp(200, payload={"slots": "nope"}))
    assert cl.fetch_grid(8868, DAY) is None                         # changed shape
    monkeypatch.setattr(cl.requests, "post", lambda *a, **k: _Resp(503))
    assert cl.fetch_grid(8868, DAY) is None
    assert cl.open_runs(8868, DAY, now=NOW) is None                 # None, never an empty list
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", False)
    assert cl.fetch_grid(8868, DAY) is None


def test_five_failures_stop_for_the_day(monkeypatch):
    n = []
    monkeypatch.setattr(cl.requests, "post", lambda *a, **k: n.append(1) or _Resp(500))
    for _ in range(5):
        assert cl.fetch_grid(8868, DAY) is None
    assert cl._state["stopped_for"] == NOW.date() and len(n) == 5
    assert cl.fetch_grid(8868, DAY) is None and len(n) == 5         # no further requests today


# ─── room directory ──────────────────────────────────────────────────────────

SPACES_HTML = """
resources.push({
    id: "eid_62878",
    title: "Egret,\\u0020Room\\u0020409 (Capacity 4)",
    url: "/space/62878",
    eid: 62878,
    gid: 16363,
    lid: 8868,
    grouping: "Moffitt\\u0020Library\\u00204th\\u0020Floor\\u0020Study\\u0020Rooms",
    capacity: 4,
});
resources.push({
    id: "eid_99999",
    title: "Osprey,\\u0020Room\\u0020437 (Capacity 6)",
    url: "/space/99999",
    eid: 99999,
    gid: 16363,
    lid: 8868,
    grouping: "Moffitt\\u0020Library\\u00204th\\u0020Floor\\u0020Study\\u0020Rooms",
    capacity: 6,
});
"""


def test_spaces_page_parses_titles_with_commas_and_refresh_unions_over_seeds(monkeypatch):
    parsed = cl.parse_spaces_page(SPACES_HTML, lid=8868)
    assert parsed[62878].name == "Egret, Room 409" and parsed[62878].capacity == 4 and parsed[62878].group == "4th floor"
    assert parsed[99999].name == "Osprey, Room 437" and parsed[99999].capacity == 6
    monkeypatch.setattr(cl.requests, "get", lambda *a, **k: _Resp(200, text=SPACES_HTML))
    assert cl.refresh_rooms()
    assert 99999 in cl.rooms() and 62893 in cl.rooms()              # new room added, seeds kept
    monkeypatch.setattr(cl.requests, "get", lambda *a, **k: _Resp(500))
    assert not cl.refresh_rooms() and 99999 in cl.rooms()           # a miss never shrinks it
    assert cl.room(62878).url == "https://berkeley.libcal.com/space/62878"


def test_seed_directory_is_the_live_moffitt_set():
    assert len(cl.SEED_ROOMS) == 15
    assert {r.capacity for r in cl.SEED_ROOMS.values()} == {4, 8}
    assert cl.SEED_ROOMS[62891].name == "Palm, Room 517"


# ─── the tools ───────────────────────────────────────────────────────────────

@pytest.fixture
def tool_on(monkeypatch, hours_html):
    monkeypatch.setattr(config, "FIND_STUDY_SPACE_TOOL_ENABLED", True)
    # the same day-shape for whichever day the tool asks about
    monkeypatch.setattr(cl, "fetch_grid", lambda lid, day: [
        dict(s, start=s["start"].replace(DAY.isoformat(), day.isoformat()),
             end=s["end"].replace(DAY.isoformat(), day.isoformat())) for s in _grid()])
    hours = cl.parse_hours_page(hours_html)
    monkeypatch.setattr(cl, "fetch_hours", lambda day: hours)


def test_find_study_space_is_gated_by_its_flag(monkeypatch, db):
    import agent_tools
    monkeypatch.setattr(config, "FIND_STUDY_SPACE_TOOL_ENABLED", False)
    assert agent_tools.dispatch_tool("find_study_space", {}, 1).startswith("error:")
    assert agent_tools.dispatch_tool("send_study_room_link", {"eid": 62878}, 1).startswith("error:")


def test_find_study_space_returns_rooms_hours_and_the_rules(db, tool_on):
    import agent_tools
    user = make_user(db)
    out = agent_tools.dispatch_tool("find_study_space", {"start": "14:20", "group_size": 1}, user.id)
    assert out.startswith("ok: Moffitt rooms today:")
    assert "Egret, Room 409 (4th floor, fits 4) free 3pm–4pm [eid 62878]" in out
    assert "Moffitt (open 24 hours; tech lending, snacks ok)" in out
    assert "send_study_room_link" in out and "CalNet" in out and "no crowd data" in out
    assert "note:" not in out                                        # 3pm isn't sleep time
    assert "find_study_space" in agent_tools.READ_ONLY_TOOLS


def test_find_study_space_group_need_and_date(db, tool_on):
    import agent_tools
    user = make_user(db)
    out = agent_tools.dispatch_tool("find_study_space", {"group_size": 6, "need": "snacks"}, user.id)
    assert "Room 415 (Van Houten, fits 8)" in out and "Egret" not in out
    assert "[need=snacks]" in out and "Engineering Library" in out and "Doe" not in out
    out = agent_tools.dispatch_tool("find_study_space", {"date": "2026-03-11", "start": "09:00"}, user.id)
    assert "Moffitt rooms tomorrow:" in out
    assert agent_tools.dispatch_tool("find_study_space", {"date": "2026-03-01"}, user.id).startswith("error:")
    assert agent_tools.dispatch_tool("find_study_space", {"date": "soon"}, user.id).startswith("error:")


def test_find_study_space_flags_a_sleep_window(db, tool_on):
    import agent_tools
    user = make_user(db, sleep_time="23:00", wake_time="07:00")
    out = agent_tools.dispatch_tool("find_study_space", {"date": "2026-03-11", "start": "01:00"}, user.id)
    assert "note: 1:00am is inside their usual sleep hours (bed 23:00, up 07:00)" in out
    out = agent_tools.dispatch_tool("find_study_space", {"date": "2026-03-11", "start": "10:00"}, user.id)
    assert "note:" not in out


def test_find_study_space_is_honest_when_a_source_is_down(db, tool_on, monkeypatch):
    import agent_tools
    user = make_user(db)
    monkeypatch.setattr(cl, "fetch_grid", lambda lid, day: None)
    out = agent_tools.dispatch_tool("find_study_space", {}, user.id)
    assert out.startswith("ok:") and "couldn't read the Moffitt booking grid" in out and "open at" in out
    monkeypatch.setattr(cl, "fetch_hours", lambda day: None)
    out = agent_tools.dispatch_tool("find_study_space", {}, user.id)
    assert out.startswith("error:") and "couldn't reach the library systems" in out
    monkeypatch.setattr(cl, "fetch_grid", lambda lid, day: [])
    out = agent_tools.dispatch_tool("find_study_space", {}, user.id)
    # an EMPTY grid is "not bookable yet" (live 2026-10-08: 3 weeks out → 0 slots), never "all full"
    assert "no slots for" in out and "outside the booking window" in out and "couldn't pull the hours page" in out
    # all rooms genuinely taken → honest "nothing free", with a way forward
    monkeypatch.setattr(cl, "fetch_grid", lambda lid, day: [_slot(62878, 15, 16, booked=True)])
    out = agent_tools.dispatch_tool("find_study_space", {"start": "15:00"}, user.id)
    assert "nothing at Moffitt free for 60 min from 3pm" in out


def test_send_study_room_link_texts_one_bubble_with_the_url(db, tool_on, sms_capture):
    import agent_tools
    from models import Message
    user = make_user(db)
    out = agent_tools.dispatch_tool("send_study_room_link", {"eid": 62878}, user.id)
    assert out.startswith("ok:") and "calnet" in out.lower()
    assert len(sms_capture) == 1
    phone, body = sms_capture[0]
    assert phone == user.phone and "https://berkeley.libcal.com/space/62878" in body and "Egret, Room 409" in body
    db.expire_all()
    row = db.query(Message).filter_by(user_id=user.id, direction="out", message_type="study_room_link").first()
    assert row is not None
    assert agent_tools.dispatch_tool("send_study_room_link", {"eid": 101038}, user.id).startswith("error:")
    assert agent_tools.dispatch_tool("send_study_room_link", {"eid": "x"}, user.id).startswith("error:")
    assert len(sms_capture) == 1


# ─── wiring ──────────────────────────────────────────────────────────────────

def test_loop_offers_the_tools_only_behind_the_flag():
    import agent_loop
    src = open(agent_loop.__file__).read()
    assert "if config.FIND_STUDY_SPACE_TOOL_ENABLED:" in src
    assert "FIND_STUDY_SPACE_TOOL" in src and "SEND_STUDY_ROOM_LINK_TOOL" in src


def test_prompts_and_capability_carry_the_rules():
    voice = open("prompts/voice.md").read()
    assert "find_study_space" in voice and "send_study_room_link" in voice
    assert "NO crowd data" in voice and "CalNet" in voice and "2am study session" in voice
    from capabilities import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.id == "study_rooms")
    assert set(cap.tools) == {"find_study_space", "send_study_room_link"}


def test_scheduler_registers_the_weekly_room_refresh():
    import scheduler
    src = open(scheduler.__file__).read()
    assert "library_rooms_refresh" in src and "refresh_rooms" in src
