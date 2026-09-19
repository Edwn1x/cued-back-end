"""
Series §2 — RSF crowd meter + virtual line. Label thresholds incl. line_on,
expected() None under 14 days, window on a fixture day, no poll and no beat when
closed, one gym beat per day, D1 contains the Waitwell URL, poller stops after 5
failures, join against a recorded fixture returns a ticket, a changed shape (and
the Cloudflare challenge) raise QueueUnavailable and the beat sends D1, never
two open tickets, summon text, opt-in gate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")
FOUNDER = dict(name="Nau Ruiz", onboarding_step=3, current_split="ppl", split_pointer_day="pull",
               confirmed_training_days="mon,tue,wed,thu,fri,sat,sun", workout_time="17:00", user_timezone="America/Los_Angeles")


def _utc(dt_local):
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)


def _reading(db, pct, minutes_ago=1, est_wait=None, at=None):
    from models import get_session, GymOccupancy
    base = (at.astimezone(timezone.utc).replace(tzinfo=None) if at else datetime.now(timezone.utc).replace(tzinfo=None))
    s = get_session()
    try:
        s.add(GymOccupancy(facility="rsf_weights", ts=base - timedelta(minutes=minutes_ago),
                           pct=pct, est_wait_min=est_wait, raw={}))
        s.commit()
    finally:
        s.close()


@pytest.fixture
def rsf_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "RSF_METER_ENABLED", True)
    monkeypatch.setattr(config, "RSF_BEATS_ENABLED", True)
    monkeypatch.setattr(config, "RSF_QUEUE_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


class _Resp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code, self._p, self.headers, self.text = status, payload, headers or {}, text

    def json(self):
        if self._p is None:
            raise ValueError("no json")
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


# ─── 2.3 labels / now / expected / window ───────────────────────────────────

def test_label_thresholds_and_line_on(db):
    import occupancy
    assert [occupancy.label_for(p) for p in (0, 24, 25, 49, 50, 74, 75, 94, 95, 100)] == \
        ["dead", "dead", "light", "light", "busy", "busy", "packed", "packed", "line", "line"]
    _reading(db, 38)
    r = occupancy.now()
    assert r["pct"] == 38 and r["label"] == "light" and r["line_on"] is False
    assert occupancy.context_line(r).startswith("rsf weight room: 38% (light), as of ")
    _reading(db, 96)
    assert occupancy.now()["line_on"] is True
    _reading(db, 80, est_wait=12)
    assert occupancy.now()["line_on"] is True and occupancy.now()["est_wait_min"] == 12


def test_now_is_none_when_stale_or_empty(db):
    import occupancy
    assert occupancy.now() is None
    _reading(db, 50, minutes_ago=45)
    assert occupancy.now() is None and occupancy.context_line(None) == ""


def test_expected_needs_14_days_then_returns_the_median(db):
    import occupancy
    from models import get_session, GymOccupancy
    s = get_session()
    try:
        base = datetime(2026, 9, 14, 12, 0, tzinfo=TZ)   # a monday noon local
        for d in range(20):
            for pct in (30, 40, 50):
                ts = base - timedelta(days=d)
                s.add(GymOccupancy(facility="rsf_weights", ts=_utc(ts), pct=pct + (d % 3), raw={}))
            if d == 10:
                assert occupancy.expected(0, 12, at=_utc(base) + timedelta(minutes=1)) is None
        s.commit()
    finally:
        s.close()
    exp = occupancy.expected(0, 12, at=_utc(base) + timedelta(minutes=1))     # mondays at 12
    assert exp is not None and 39 <= exp <= 43
    assert occupancy.expected(2, 3, at=_utc(base) + timedelta(minutes=1)) is None   # no 3am rows


def test_window_picks_the_lowest_expected_block_still_ahead(db, monkeypatch):
    import occupancy
    table = {(2, 13): 70, (2, 14): 65, (2, 15): 30, (2, 16): 20, (2, 17): 60, (2, 18): 80, (2, 19): 85}
    monkeypatch.setattr(occupancy, "expected", lambda wd, h, at=None: table.get((wd, h)))
    at = datetime(2026, 9, 16, 12, 40, tzinfo=TZ)   # wednesday 12:40
    w = occupancy.window(hours_ahead=6, at=at.astimezone(timezone.utc))
    assert w is not None and w["start"].hour == 15 and w["start"].minute in (0, 30) and w["expected_pct"] <= 30


# ─── 2.1 poller ─────────────────────────────────────────────────────────────

def test_hours_parse_and_is_open():
    from integrations import rsf
    html = "<p>Monday–Friday 7 a.m.–11 p.m.</p><p>Saturday 8 a.m.–6 p.m.</p><p>Sunday 8 a.m.–11 p.m.</p>"
    assert rsf.parse_hours(html) == {0: (7, 23), 1: (7, 23), 2: (7, 23), 3: (7, 23), 4: (7, 23), 5: (8, 18), 6: (8, 23)}
    assert rsf.is_open(datetime(2026, 9, 14, 12, 0, tzinfo=TZ))            # mon noon
    assert not rsf.is_open(datetime(2026, 9, 14, 6, 30, tzinfo=TZ))        # before 7
    assert not rsf.is_open(datetime(2026, 9, 19, 19, 0, tzinfo=TZ))        # sat 7pm (closes 6)
    assert not rsf.is_open(datetime(2026, 12, 25, 12, 0, tzinfo=TZ))       # christmas


def test_poller_writes_a_row_skips_when_closed_and_stops_after_5_failures(db, rsf_on, monkeypatch, caplog):
    import logging
    from integrations import rsf
    from models import get_session, GymOccupancy
    rsf._state.update(failures=0, stopped_for=None, token=None)
    monkeypatch.setattr(rsf, "fetch_reading", lambda: {"count": 57, "capacity": 150, "pct": 38, "name": "Weight Rooms", "raw": {"current_count": 57}})
    assert rsf.poll_once(datetime(2026, 9, 14, 12, 0, tzinfo=TZ))["pct"] == 38
    assert rsf.poll_once(datetime(2026, 9, 14, 2, 0, tzinfo=TZ)) is None          # closed → no request, no row
    s = get_session()
    try:
        assert s.query(GymOccupancy).count() == 1
    finally:
        s.close()

    def _boom():
        raise RuntimeError("503")
    monkeypatch.setattr(rsf, "fetch_reading", _boom)
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            assert rsf.poll_once(datetime(2026, 9, 14, 12, 0, tzinfo=TZ)) is None
    assert rsf._state["stopped_for"] == datetime(2026, 9, 14).date()
    assert any("RSF_POLL_STOPPED_FOR_DAY" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(rsf, "fetch_reading", lambda: {"count": 1, "capacity": 150, "pct": 1, "name": "", "raw": {}})
    assert rsf.poll_once(datetime(2026, 9, 14, 13, 0, tzinfo=TZ)) is None          # still stopped today
    assert rsf.poll_once(datetime(2026, 9, 15, 13, 0, tzinfo=TZ))["pct"] == 1       # next day resumes
    rsf._state.update(failures=0, stopped_for=None)


def test_poller_is_off_without_the_flag(db, monkeypatch):
    import config
    from integrations import rsf
    monkeypatch.setattr(config, "RSF_METER_ENABLED", False)
    monkeypatch.setattr(rsf, "fetch_reading", lambda: (_ for _ in ()).throw(AssertionError("no request when off")))
    assert rsf.poll_once(datetime(2026, 9, 14, 12, 0, tzinfo=TZ)) is None


def test_fetch_reading_parses_the_density_shape(monkeypatch):
    from integrations import rsf
    calls = []

    def fake_post(url, headers=None, timeout=None):
        calls.append(("post", url, headers.get("User-Agent")))
        return _Resp(200, {"access_token": "tok"})

    def fake_get(url, headers=None, timeout=None):
        calls.append(("get", url, headers.get("Authorization")))
        return _Resp(200, {"dedicated_space": {"current_count": 141, "capacity": 150, "name": "Weight Rooms"}})
    monkeypatch.setattr(rsf.requests, "post", fake_post)
    monkeypatch.setattr(rsf.requests, "get", fake_get)
    rsf._state["token"] = None
    r = rsf.fetch_reading()
    assert r["pct"] == 94 and r["count"] == 141 and r["capacity"] == 150
    assert calls[0][1].endswith("/oauth/wayfinding/exchange") and calls[0][2].startswith("Cued/1.0 (contact: ")
    assert calls[1][2] == "Bearer tok" and "dsp_956223069054042646" in calls[1][1]
    rsf.fetch_reading()
    assert sum(1 for c in calls if c[0] == "post") == 1     # token cached, one request pair per poll at most


# ─── 2.6 waitwell client ────────────────────────────────────────────────────

def test_join_against_a_recorded_fixture_returns_a_ticket_and_never_two_open(db, rsf_on, monkeypatch):
    from integrations.waitwell import client as ww
    from models import get_session, QueueTicket
    user = make_user(db, **FOUNDER)
    posts = []
    monkeypatch.setattr(ww, "transport_post", lambda url, data: posts.append((url, data)) or _Resp(200, {"ticket_id": "t-1", "position": 7, "est_wait_min": 25}))
    t = ww.join(user.id, "Nau", user.phone)
    assert t.ticket_id == "t-1" and t.position == 7 and t.est_wait_min == 25
    assert posts == [(ww.JOIN_URL, {"name": "Nau", "phone": user.phone})]
    t2 = ww.join(user.id, "Nau", user.phone)      # idempotent: the open ticket, no second POST
    assert t2.ticket_id == "t-1" and len(posts) == 1
    s = get_session()
    try:
        assert s.query(QueueTicket).filter_by(user_id=user.id, status="open").count() == 1
    finally:
        s.close()


@pytest.mark.parametrize("resp,reason", [
    (_Resp(403, None, {"cf-mitigated": "challenge"}, "Just a moment"), "cloudflare_challenge"),
    (_Resp(500, None), "http_500"),
    (_Resp(200, {"unexpected": True}), "changed_shape"),
    (_Resp(200, None, {}, "<html>"), "non_json"),
])
def test_changed_shape_or_challenge_raises_queue_unavailable_and_alerts_once(db, rsf_on, monkeypatch, caplog, resp, reason):
    import logging
    from integrations.waitwell import client as ww
    ww._alerted_on["date"] = None
    user = make_user(db, **FOUNDER)
    monkeypatch.setattr(ww, "transport_post", lambda url, data: resp)
    with caplog.at_level(logging.INFO):
        with pytest.raises(ww.QueueUnavailable, match=reason):
            ww.join(user.id, "Nau", user.phone)
    assert any("QUEUE_UNAVAILABLE_ALERT" in r.getMessage() for r in caplog.records)


def test_join_is_disabled_by_flag_and_once_per_day(db, monkeypatch):
    import config
    from integrations.waitwell import client as ww
    from models import get_session, QueueTicket
    monkeypatch.setattr(config, "RSF_QUEUE_ENABLED", False)
    user = make_user(db, **FOUNDER)
    with pytest.raises(ww.QueueUnavailable, match="queue_disabled"):
        ww.join(user.id, "Nau", user.phone)
    monkeypatch.setattr(config, "RSF_QUEUE_ENABLED", True)
    s = get_session()
    try:
        s.add(QueueTicket(user_id=user.id, ticket_id="old", status="left", joined_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)))
        s.commit()
    finally:
        s.close()
    with pytest.raises(ww.QueueUnavailable, match="already_joined_today"):
        ww.join(user.id, "Nau", user.phone)


def test_summon_text_and_leave(db, rsf_on, monkeypatch, sms_capture):
    from integrations.waitwell import client as ww
    import gym_beats
    from models import get_session, QueueTicket
    user = make_user(db, **FOUNDER)
    monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(200, {"ticket_id": "t-9", "position": 3, "est_wait_min": 10}))
    ww.join(user.id, "Nau", user.phone)
    monkeypatch.setattr(ww, "transport_get", lambda url: _Resp(200, {"position": 0, "summoned": True}))
    assert gym_beats.poll_open_tickets() == 1
    assert sms_capture[-1][1] == "you're up at rsf. 10 min to get to the weight room door."
    s = get_session()
    try:
        assert s.query(QueueTicket).filter_by(user_id=user.id).one().status == "summoned"
    finally:
        s.close()
    assert ww.open_ticket(user.id) is None
    assert ww.leave("t-9") is False       # already summoned, nothing open to leave


# ─── 2.5 beats + 2.7 opt-in ─────────────────────────────────────────────────

def _now_local(h, m=0, day=14):
    return datetime(2026, 9, day, h, m, tzinfo=TZ).astimezone(timezone.utc)


def test_dead_beat_only_with_a_planned_untrained_day_and_once(db, rsf_on, monkeypatch):
    import gym_beats
    from models import get_session, User, Message
    user = make_user(db, **FOUNDER)
    _reading(db, 18)
    s = get_session()
    try:
        u = s.get(User, user.id)
        b = gym_beats.propose(u, s, _now_local(15))
        assert b and b.kind == "dead" and b.text == "gym's dead right now. quick legs?"
        s.add(Message(user_id=user.id, direction="out", body=b.text, message_type=b.message_type,
                      created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        s.commit()
        assert gym_beats.propose(u, s, _now_local(16)) is None            # one gym beat per day
        rest = make_user(db, **dict(FOUNDER, confirmed_training_days="mon", phone="+15550002222"))
        assert gym_beats.propose(s.get(User, rest.id), s, _now_local(15, day=15)) is None   # tuesday: not planned
        assert gym_beats.propose(u, s, _now_local(2)) is None             # closed → nothing
    finally:
        s.close()


def test_line_beat_asks_opt_in_once_then_d1_then_d2_on_yes(db, rsf_on, monkeypatch, sms_capture):
    import gym_beats
    from integrations.waitwell import client as ww
    from models import get_session, User, Message
    user = make_user(db, **FOUNDER)          # lifts at 17:00
    _reading(db, 96)
    s = get_session()
    try:
        u = s.get(User, user.id)
        b = gym_beats.propose(u, s, _now_local(15, 30))
        assert b.kind == "optin_ask" and b.text == gym_beats.OPTIN_ASK
        # pinned to the fixture day: with created_at=now this test rotted once the real
        # date passed 09-15 (the day-15 propose below saw "a gym beat already sent today")
        s.add(Message(user_id=user.id, direction="out", body=b.text, message_type="gym_optin_ask",
                      created_at=_utc(datetime(2026, 9, 14, 15, 30, tzinfo=TZ))))
        s.commit()
    finally:
        s.close()
    # 'no' → D1 that time
    assert gym_beats.handle_text(user.id, "nah").startswith("no stress — here's the link") and ww.PUBLIC_URL in gym_beats.handle_text(user.id, "nah")
    # later: 'handle the line for me' flips it on
    assert gym_beats.handle_text(user.id, "handle the line for me").startswith("bet — when rsf's packed")
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.queue_opt_in is True
        # opted in, no join possible (challenge) → D1 with the URL; a fresh day so the one-per-day gate is clear
        _reading(db, 96, at=_now_local(15, 29, day=15))
        monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(403, None, {"cf-mitigated": "challenge"}, "Just a moment"))
        b = gym_beats.propose(u, s, _now_local(15, 30, day=15))
        assert b.kind == "line_d1" and ww.PUBLIC_URL in b.text and b.text.startswith("rsf is at 96%, line's on. join now → ")
        assert "leave in about 10 min" in b.text
        # opted in, join works → D2 with real numbers
        monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(200, {"ticket_id": "t-2", "position": 9, "est_wait_min": 25}))
        b = gym_beats.propose(u, s, _now_local(15, 30, day=15))
        assert b.kind == "line_d2"
        assert b.text == "line at rsf is 25 min. put you in the virtual queue — you're up at 3:55, leave in about 15 min."
        assert gym_beats.propose(u, s, _now_local(15, 40, day=15)) is None   # open ticket → no second beat
    finally:
        s.close()


def test_d2_uses_the_walk_time_from_a_fresh_location_signal(db, rsf_on, monkeypatch):
    import gym_beats
    from integrations.waitwell import client as ww
    from models import get_session, User, Signal
    user = make_user(db, **dict(FOUNDER, queue_opt_in=True))
    _reading(db, 97)
    s = get_session()
    try:
        s.add(Signal(user_id=user.id, kind="location", ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5),
                     payload={"place": "moffitt", "walk_min_to_rsf": 6}))
        s.commit()
        monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(200, {"ticket_id": "t-3", "position": 9, "est_wait_min": 25}))
        b = gym_beats.propose(s.get(User, user.id), s, _now_local(15, 30))
        assert b.text == "line at rsf is 25 min. put you in the virtual queue — you're up at 3:55, leave by 3:49."
    finally:
        s.close()


def test_sweep_respects_guardrails_and_sends(db, rsf_on, monkeypatch, sms_capture):
    import gym_beats, heartbeat
    from models import get_session, Message
    user = make_user(db, **FOUNDER)
    _reading(db, 18)
    monkeypatch.setattr(gym_beats, "_local", lambda u, at=None: datetime(2026, 9, 14, 15, 0, tzinfo=TZ))
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s: "quiet_hours")
    assert gym_beats.sweep() == 0 and sms_capture == []
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s: None)
    assert gym_beats.sweep() == 1
    assert sms_capture[-1][1] == "gym's dead right now. quick legs?"
    assert gym_beats.sweep() == 0         # one per day


def test_not_going_leaves_the_line_and_the_context_line_appears_when_the_gym_comes_up(db, rsf_on, monkeypatch):
    import gym_beats
    from integrations.waitwell import client as ww
    from agent_loop import _gym_mentioned
    user = make_user(db, **dict(FOUNDER, queue_opt_in=True))
    monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(200, {"ticket_id": "t-4", "position": 2, "est_wait_min": 8}))
    ww.join(user.id, "Nau", user.phone)
    assert gym_beats.handle_text(user.id, "not going") == "ok, dropped you from the line — ignore their texts."
    assert ww.open_ticket(user.id) is None
    assert gym_beats.handle_text(user.id, "what should i eat") is None
    assert _gym_mentioned("is rsf busy rn", user) and not _gym_mentioned("what should i eat", user)


# ─── 2.8 'heading out' → the link, in code ──────────────────────────────────

@pytest.fixture
def meter_only(monkeypatch):
    """The prod shape today: meter on, proactive beats and the queue OFF."""
    import config
    monkeypatch.setattr(config, "RSF_METER_ENABLED", True)
    monkeypatch.setattr(config, "RSF_BEATS_ENABLED", False)
    monkeypatch.setattr(config, "RSF_QUEUE_ENABLED", False)


def _open_now(monkeypatch):
    import gym_beats
    monkeypatch.setattr(gym_beats, "is_open", lambda now_local: True)


@pytest.mark.parametrize("phrase", ["heading out", "heading to the gym", "omw to rsf", "leaving for the gym now",
                                    "bouta lift", "Heading to RSF!", "walking to rsf now", "about to go lift"])
def test_heading_out_with_the_line_on_sends_the_form_link_in_code(db, meter_only, monkeypatch, phrase):
    import gym_beats
    from integrations.waitwell import client as ww
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    _reading(db, 97)
    b = gym_beats.heading_out(user.id, phrase)
    assert b is not None and b.kind == "line_d1" and b.message_type == "gym_line_d1"
    assert b.text.startswith("rsf is at 97%, line's on. join now → ")
    assert ww.JOIN_URL in b.text and ww.JOIN_URL.endswith("/join/48")     # the FORM, one tap — not the landing page
    assert "leave in about 10 min" in b.text                                # no location signal → the default framing


@pytest.mark.parametrize("phrase", ["heading out, had a bagel", "heading out later tonight after class idk", "is the gym packed",
                                    "not going to the gym", "leaving the gym", "heading home", "going to the gym tomorrow", "gym was dead"])
def test_heading_out_ignores_mixed_or_non_departure_texts(db, meter_only, monkeypatch, phrase):
    import gym_beats
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    _reading(db, 97)
    assert gym_beats.heading_out(user.id, phrase) is None


def test_bare_heading_out_needs_a_training_day_but_a_gym_word_does_not(db, meter_only, monkeypatch):
    import gym_beats
    from integrations.waitwell import client as ww
    _open_now(monkeypatch)
    _reading(db, 97)
    # 'mon' only — on any other weekday a bare 'heading out' is class, errands, anything
    user = make_user(db, **dict(FOUNDER, confirmed_training_days="mon"))
    is_monday = gym_beats._local(user).weekday() == 0
    bare = gym_beats.heading_out(user.id, "heading out")
    assert (bare is not None) == is_monday
    assert gym_beats.heading_out(user.id, "omw") is None or is_monday
    # naming the gym is unambiguous on any day
    b = gym_beats.heading_out(user.id, "heading to the gym")
    assert b is not None and ww.JOIN_URL in b.text
    # FOUNDER trains every day → bare 'heading out' is the gym
    every = make_user(db, **dict(FOUNDER, phone="+15550003333"))
    assert gym_beats.heading_out(every.id, "heading out") is not None


def test_heading_out_is_silent_unless_the_line_is_on_now(db, meter_only, monkeypatch):
    import gym_beats, config
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    assert gym_beats.heading_out(user.id, "heading out") is None            # no reading at all
    _reading(db, 61)
    assert gym_beats.heading_out(user.id, "heading out") is None            # busy, no line → the model turn handles it
    _reading(db, 97, minutes_ago=45)
    assert gym_beats.heading_out(user.id, "heading out") is None            # stale line reading → never act on it
    _reading(db, 97)
    monkeypatch.setattr(gym_beats, "is_open", lambda now_local: False)
    assert gym_beats.heading_out(user.id, "heading out") is None            # closed
    _open_now(monkeypatch)
    monkeypatch.setattr(config, "RSF_METER_ENABLED", False)
    assert gym_beats.heading_out(user.id, "heading out") is None            # flag off


def test_heading_out_opted_in_tries_the_queue_then_falls_back_to_the_link(db, meter_only, monkeypatch):
    import gym_beats, config
    from integrations.waitwell import client as ww
    from models import get_session, User
    user = make_user(db, **FOUNDER)
    s = get_session()
    try:
        s.get(User, user.id).queue_opt_in = True; s.commit()
    finally:
        s.close()
    _open_now(monkeypatch)
    _reading(db, 97)
    # queue flag off (prod today) → join raises by construction → D1
    b = gym_beats.heading_out(user.id, "heading out")
    assert b.kind == "line_d1" and ww.JOIN_URL in b.text
    # queue on but the site challenges → still D1
    monkeypatch.setattr(config, "RSF_QUEUE_ENABLED", True)
    monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(403, None, {"cf-mitigated": "challenge"}, "Just a moment"))
    assert gym_beats.heading_out(user.id, "omw to rsf").kind == "line_d1"
    # a real transport → D2 with the numbers, and a second 'heading out' says nothing new (open ticket)
    monkeypatch.setattr(ww, "transport_post", lambda url, data: _Resp(200, {"ticket_id": "t-8", "position": 4, "est_wait_min": 20}))
    b = gym_beats.heading_out(user.id, "heading out")
    assert b.kind == "line_d2" and b.text.startswith("line at rsf is 20 min. put you in the virtual queue")
    assert gym_beats.heading_out(user.id, "heading out") is None


def test_heading_out_runs_before_the_model_and_counts_as_the_days_gym_beat(db, meter_only, monkeypatch, driver, anthropic_stub, sms_capture):
    import gym_beats, config
    from integrations.waitwell import client as ww
    from models import get_session, User
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    _reading(db, 97)
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(AssertionError("model must not run for 'heading out' with the line on")))
    driver.send(user, "heading to the gym")
    assert sms_capture and ww.JOIN_URL in sms_capture[-1][1]
    # the send was logged as gym_line_d1 → the proactive sweep won't repeat it today
    monkeypatch.setattr(config, "RSF_BEATS_ENABLED", True)
    s = get_session()
    try:
        assert gym_beats.gym_beat_sent_today(s, s.get(User, user.id), gym_beats._local(s.get(User, user.id)))
        assert gym_beats.propose(s.get(User, user.id), s) is None
    finally:
        s.close()


def test_rsf_context_block_carries_the_link_only_when_the_line_is_on(db, meter_only):
    import gym_beats, occupancy
    from integrations.waitwell import client as ww
    assert gym_beats.rsf_context_block(None) == ""
    _reading(db, 40)
    blk = gym_beats.rsf_context_block(occupancy.now())
    assert "rsf weight room: 40% (light)" in blk and ww.JOIN_URL not in blk
    _reading(db, 96)
    blk = gym_beats.rsf_context_block(occupancy.now())
    assert "the virtual line is ON" in blk and ww.JOIN_URL in blk and "one exception to the no-links rule" in blk
