"""
Layered wake model (wake_model.py) — tier-1, clock-independent via `now=`.

Founder (2026-10-02): wearable-first wake/sleep in BOTH directions (earlier too), and for
people without a wearable detect that they're awake from them being on their phone — like
Apple Fitness sends the recap once you're up. No app: the observable signals are inbound
texts, tapbacks and workout-card opens.

    resolve_wake precedence: activity → measured_today → measured_typical → profile

Consumers under test: `_in_standing_quiet_hours` (the morning END follows a KNOWN
today-wake) and `_morning_open_signal` / `_morning_anchor_hhmm` (the briefing anchors at
it). Everything pinned to one fixed local day; the clock is always injected.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
DAY = (2026, 3, 10)   # a Tuesday


def _local(h, mi=0, *, day_offset=0):
    return datetime(*DAY, h, mi, tzinfo=PT) + timedelta(days=day_offset)


def _utc(h, mi=0, *, day_offset=0):
    return _local(h, mi, day_offset=day_offset).astimezone(timezone.utc)


def _naive(aware) -> datetime:
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _fresh(db, user):
    from models import get_session, User
    s = get_session()
    return s, s.get(User, user.id)


def _text_in(db, user_id, h, mi=0, body="hey", message_type=None):
    from models import Message
    db.add(Message(user_id=user_id, direction="in", body=body, message_type=message_type,
                   created_at=_naive(_local(h, mi))))
    db.commit()


def _text_out(db, user_id, h, mi=0, body="morning"):
    from models import Message
    db.add(Message(user_id=user_id, direction="out", body=body, message_type=None,
                   created_at=_naive(_local(h, mi))))
    db.commit()


def _active_at(db, user, h, mi=0):
    """The per-event stamp a card open / tapback leaves (no Message row)."""
    user.last_active_at = _naive(_local(h, mi))
    db.commit()


def _connect_health(db, user_id):
    from models import Integration
    db.add(Integration(user_id=user_id, provider="google_health", status="connected",
                       external_id="EXT", meta={}))
    db.commit()


def _today_sleep(db, user_id, *, wake_h, wake_m=0, bed_h=0, bed_m=30, sleep_minutes=450,
                 synced_after=timedelta(minutes=30)):
    """TODAY's wearable row: slept bed → wake (local), synced `synced_after` the wake."""
    from models import WearableDay
    wake = _local(wake_h, wake_m)
    bed = _local(bed_h, bed_m) if bed_h < 12 else _local(bed_h, bed_m, day_offset=-1)
    db.add(WearableDay(user_id=user_id, provider="google_health", day=_local(0).date().isoformat(),
                       sleep_minutes=sleep_minutes, resting_hr=55, hrv_rmssd=40.0, steps=100,
                       sleep_start=_naive(bed), sleep_end=_naive(wake),
                       synced_at=_naive(wake) + synced_after))
    db.commit()


@pytest.fixture
def model_on(monkeypatch):
    """Prod-shaped flags: standing quiet from the PROFILE window, rhythm on, wearable on."""
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_DAILY_BRIEFING_ENABLED", False)   # no weather/network in the signal
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_WEARABLE_AWARE_ENABLED", True)
    monkeypatch.setattr(config, "MEASURED_SLEEP_WINDOW_ENABLED", True)
    monkeypatch.setattr(config, "WAKE_MODEL_ENABLED", True)
    yield


def _late_riser(db, **kw):
    """Profile: bed 02:00, up 12:00 — quiet 01:30 .. 12:15 under today's logic."""
    kw.setdefault("wake_time", "12:00")
    kw.setdefault("sleep_time", "02:00")
    return make_user(db, **kw)


# ─── knobs ────────────────────────────────────────────────────────────────────

def test_knob_defaults():
    assert config.WAKE_MODEL_ENABLED is True
    assert config.WAKE_DETECT_EARLIEST_LOCAL_HOUR == 5
    assert config.WAKE_DETECT_MIN_HOURS_AFTER_SLEEP == 3
    assert config.WEARABLE_WAKE_FRESH_HOURS == 3
    assert config.WEARABLE_WAKE_MIN_SLEEP_MINUTES == 180


# ─── 1. activity wake — an inbound TEXT ───────────────────────────────────────

def test_activity_text_wake_lifts_quiet_and_anchors_but_no_briefing(db, model_on):
    """Profile wake 12:00; they text at 11:00. resolve_wake = activity 11:00; quiet is NOT
    active at 11:20 (it would be until 12:15 under the old logic); the morning anchor is
    11:00; and since the signal was a TEXT, MORNING OPEN is None — they already talked,
    the coach replies reactively."""
    from heartbeat import _in_standing_quiet_hours, _morning_anchor_hhmm, _morning_open_signal
    from wake_model import resolve_wake
    user = _late_riser(db)
    _text_in(db, user.id, 11, 0, "up, what's the plan")
    s, u = _fresh(db, user)
    try:
        now = _utc(11, 20)
        info = resolve_wake(u, s, now=now)
        assert info is not None and info.source == "activity" and info.local_hm == (11, 0)
        assert info.detail.startswith("text at 11:00")
        assert _in_standing_quiet_hours(u, now=now, session=s) is False
        assert _in_standing_quiet_hours(u, now=now, session=None) is True     # no session → today's logic
        assert _in_standing_quiet_hours(u, now=_utc(11, 10), session=s) is True   # wake+15 buffer still holds
        local = now.astimezone(PT)
        assert _morning_anchor_hhmm(u, s, local, now=now) == (11, 0)
        assert _morning_open_signal(u, s, now=now) is None                   # talked since wake
    finally:
        s.close()


# ─── 2. activity wake — a CARD OPEN (the Apple-Fitness case) ─────────────────

def test_card_open_wake_lifts_quiet_and_the_briefing_lands_after_pickup(db, model_on):
    """Profile 11:30; they open the workout card at 10:40 (last_active_at), no texts.
    Quiet lifts at ~10:55 and MORNING OPEN at 10:50 is owed — the briefing lands right
    after they pick up the phone."""
    from heartbeat import _in_standing_quiet_hours, _morning_open_signal, guardrail_reason
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _active_at(db, user, 10, 40)
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(10, 50))
        assert info is not None and info.source == "activity" and info.local_hm == (10, 40)
        assert _in_standing_quiet_hours(u, now=_utc(10, 50), session=s) is True    # within wake+15
        assert _in_standing_quiet_hours(u, now=_utc(10, 56), session=s) is False   # lifted at 10:55
        assert _in_standing_quiet_hours(u, now=_utc(11, 0), session=None) is True  # old logic: till 11:45
        sig = _morning_open_signal(u, s, now=_utc(10, 50))
        assert sig and "MORNING OPEN" in sig and "10:40am wake" in sig
        assert "wake source: activity" in sig
        assert guardrail_reason(u, s, now=_utc(10, 50)) == "quiet_hours_standing"   # 15-min grace
        assert guardrail_reason(u, s, now=_utc(11, 0)) is None                      # briefing can land
    finally:
        s.close()


def test_card_open_text_after_pickup_counts_as_talked(db, model_on):
    """Card open 10:40 (anchor), then a text at 10:50 — since_dt follows the resolved
    anchor, so the 10:50 text is 'since wake' (not 'before wake' vs the 11:30 profile)."""
    from heartbeat import _morning_open_signal
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _active_at(db, user, 10, 40)
    _text_in(db, user.id, 10, 50, "morning")
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(11, 0)) is None
    finally:
        s.close()


def test_activity_can_only_lift_quiet_never_extend_it(db, model_on):
    """Activity is an upper bound on the wake, not a measurement: a 7am waker whose first
    phone activity is a 09:00 card open is NOT re-quieted until 09:15. The anchor does
    follow (briefing after pickup) unless something was already sent since the profile wake."""
    from heartbeat import _in_standing_quiet_hours, _morning_open_signal
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _active_at(db, user, 9, 0)
    s, u = _fresh(db, user)
    try:
        assert _in_standing_quiet_hours(u, now=_utc(9, 5), session=s) is False
        sig = _morning_open_signal(u, s, now=_utc(9, 10))
        assert sig and "9:00am wake" in sig
    finally:
        s.close()
    _text_out(db, user.id, 7, 30)   # the 07:30 morning text already went out
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(9, 10)) is None
    finally:
        s.close()


# ─── 3. a tapback counts the same way ────────────────────────────────────────

def test_reaction_counts_as_activity(db, model_on):
    """A stored inbound reaction row is activity ('tapback'); and the per-event stamp the
    reaction branch leaves behaves identically to a card open."""
    from heartbeat import _in_standing_quiet_hours, _morning_open_signal
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _text_in(db, user.id, 10, 40, "👍", message_type="reaction")
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(11, 0))
        assert info and info.source == "activity" and info.local_hm == (10, 40) and "tapback" in info.detail
        assert _in_standing_quiet_hours(u, now=_utc(11, 0), session=s) is False
        # a reaction is not "talked" (reactions are excluded from every silence gate) → still owed
        assert _morning_open_signal(u, s, now=_utc(11, 0)) is not None
    finally:
        s.close()


def test_inbound_tapback_route_stamps_last_active(db, client, monkeypatch):
    """/internal/inbound with a reaction payload (acknowledged, no Message row) stamps
    users.last_active_at — its only trace for the wake model."""
    from models import User
    SECRET = "s3cret"
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    user = make_user(db)
    assert user.last_active_at is None
    payload = {"phone": user.phone, "text": "", "provider_message_id": "spc-react-wake", "chat_guid": "x",
               "service": "iMessage", "line_phone": "+1628", "timestamp": "2026-03-10T18:00:00.000Z",
               "attachments": [], "reaction": {"emoji": "👍", "target_id": "spc-nothing"}}
    r = client.post("/internal/inbound", data=json.dumps(payload),
                    headers={"X-Internal-Secret": SECRET}, content_type="application/json")
    assert r.status_code == 200 and r.get_json()["reaction"] is True
    db.expire_all()
    u = db.get(User, user.id)
    assert u.last_active_at is not None
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - u.last_active_at).total_seconds()) < 120


def test_log_incoming_stamps_last_active(db):
    from models import User
    from sms import log_incoming
    user = make_user(db)
    log_incoming(user.id, "hey", channel="imessage")
    db.expire_all()
    u = db.get(User, user.id)
    assert u.last_active_at is not None
    assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - u.last_active_at).total_seconds()) < 120


def test_card_open_stamps_last_active_every_time_and_keeps_first_open_marker(db, client):
    from models import User
    from tests.factories import TEMPLATE_ANCHORS
    from workouts.plan import build_session
    from card_page import card_token
    user = make_user(db, name="Nau", current_split="ppl", preferred_channel="sms", height_ft=5, height_in=6,
                     weight_lbs=139, age=20, gender="male", goal="fat_loss,muscle_building",
                     lift_anchors=TEMPLATE_ANCHORS)
    ws = build_session(user, "push", now=datetime(2026, 3, 9, 22, 0))
    tok = card_token(user.id, ws.id)
    hdr = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    assert client.get("/card/api/session", headers=hdr).status_code == 200
    db.expire_all()
    u = db.get(User, user.id)
    first_open, first_active = u.card_opened_at, u.last_active_at
    assert first_open is not None and first_active is not None
    # pin last_active_at back in time, open again: the per-event stamp moves, the
    # first-ever marker does not.
    u.last_active_at = first_active - timedelta(hours=5)
    db.commit()
    assert client.get("/card/api/session", headers=hdr).status_code == 200
    db.expire_all()
    u = db.get(User, user.id)
    assert u.card_opened_at == first_open
    assert u.last_active_at > first_active - timedelta(hours=5)


# ─── 4. the still-up guard ────────────────────────────────────────────────────

def test_3am_text_is_not_a_wake(db, model_on):
    """Bed 02:00, up 12:00; a text at 03:00 is 'still up', not 'woke up' (fails the 5am floor
    and the 3h-after-sleep rule) → quiet still holds at 03:30 and nothing re-anchors."""
    from heartbeat import _in_standing_quiet_hours, _morning_anchor_hhmm
    from wake_model import resolve_wake
    user = _late_riser(db)
    _text_in(db, user.id, 3, 0, "cant sleep lol")
    s, u = _fresh(db, user)
    try:
        now = _utc(3, 30)
        info = resolve_wake(u, s, now=now)
        assert info is not None and info.source == "profile" and info.local_hm == (12, 0)
        assert _in_standing_quiet_hours(u, now=now, session=s) is True
        assert _morning_anchor_hhmm(u, s, now.astimezone(PT), now=now) == (12, 0)
    finally:
        s.close()


def test_guard_skips_the_3am_text_and_takes_the_next_plausible_activity(db, model_on):
    from wake_model import resolve_wake
    user = _late_riser(db)
    _text_in(db, user.id, 3, 0, "cant sleep")
    _text_in(db, user.id, 10, 30, "ok actually up now")
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(11, 0))
        assert info and info.source == "activity" and info.local_hm == (10, 30)
    finally:
        s.close()


def test_guard_hours_after_sleep_with_early_bedtime(db, model_on):
    """Bed 23:00: a 05:30 text is >= 5am and 6.5h after bed → a wake. Bed 03:30: the same
    05:30 text is only 2h after bed → still up."""
    from wake_model import resolve_wake
    early = make_user(db, wake_time="08:00", sleep_time="23:00")
    late = make_user(db, wake_time="12:00", sleep_time="03:30")
    for usr in (early, late):
        _text_in(db, usr.id, 5, 30, "hey")
    s, e = _fresh(db, early)
    try:
        assert resolve_wake(e, s, now=_utc(6)).source == "activity"
        l = s.get(type(e), late.id)
        assert resolve_wake(l, s, now=_utc(6)).source == "profile"
    finally:
        s.close()


# ─── 5. measured_today — the watch, BOTH directions ──────────────────────────

def test_measured_today_early_and_fresh_lifts_quiet_early(db, model_on):
    """Watch: slept 00:30 → 08:30 (450 min), synced 09:00; profile 11:30. Quiet lifts at
    ~08:45 (under the old max() it held until 11:45) and the anchor is 08:30."""
    from heartbeat import _in_standing_quiet_hours, _morning_anchor_hhmm, _morning_open_signal, guardrail_reason
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, sleep_minutes=450)
    s, u = _fresh(db, user)
    try:
        now = _utc(9, 0)
        info = resolve_wake(u, s, now=now)
        assert info and info.source == "measured_today" and info.local_hm == (8, 30)
        assert _in_standing_quiet_hours(u, now=_utc(8, 40), session=s) is True    # wake+15
        assert _in_standing_quiet_hours(u, now=now, session=s) is False           # lifted at 08:45
        assert _in_standing_quiet_hours(u, now=now, session=None) is True         # old logic: held to 11:45
        assert _morning_anchor_hhmm(u, s, now.astimezone(PT), now=now) == (8, 30)
        sig = _morning_open_signal(u, s, now=now)
        assert sig and "8:30am wake" in sig and "wake source: measured_today" in sig
        assert guardrail_reason(u, s, now=now) is None
    finally:
        s.close()


def test_measured_today_early_briefing_not_repeated_when_anchor_falls_back(db, model_on):
    """The fresh signal ages out (synced_at keeps moving); at 12:00 the anchor falls back
    to the 11:30 profile window — but the 09:00 briefing counts as 'talked since the
    earliest known wake', so no second morning text."""
    from heartbeat import _morning_open_signal
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, sleep_minutes=450, synced_after=timedelta(hours=3, minutes=20))
    _text_out(db, user.id, 9, 0, "morning — push day, lecture at 2")
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(12, 0)) is None
    finally:
        s.close()


def test_measured_today_stale_is_ignored(db, model_on):
    """Same wake but the row synced 20h later → not a real-time read → typical/profile,
    i.e. the pre-existing extend-only path (quiet holds until 11:45; anchor 11:30)."""
    from heartbeat import _in_standing_quiet_hours, _morning_anchor_hhmm
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, sleep_minutes=450, synced_after=timedelta(hours=20))
    s, u = _fresh(db, user)
    try:
        now = _utc(9, 0)
        info = resolve_wake(u, s, now=now)
        assert info and info.source == "profile" and info.local_hm == (11, 30)
        assert _in_standing_quiet_hours(u, now=now, session=s) is True
        assert _morning_anchor_hhmm(u, s, now.astimezone(PT), now=now) == (11, 30)
    finally:
        s.close()


def test_measured_today_nap_is_ignored(db, model_on):
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, bed_h=7, bed_m=50, sleep_minutes=40)
    s, u = _fresh(db, user)
    try:
        assert resolve_wake(u, s, now=_utc(9)).source == "profile"
        assert _in_standing_quiet_hours(u, now=_utc(9), session=s) is True
    finally:
        s.close()


def test_measured_today_late_and_fresh_holds_quiet_and_moves_the_anchor(db, model_on):
    """The #156 sleep-in case still works: slept in to 13:00 → quiet holds till ~13:15,
    anchor 13:00."""
    from heartbeat import _in_standing_quiet_hours, _morning_anchor_hhmm, _morning_open_signal
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=13, wake_m=0, bed_h=3, bed_m=0, sleep_minutes=600)
    s, u = _fresh(db, user)
    try:
        assert resolve_wake(u, s, now=_utc(13, 30)).local_hm == (13, 0)
        assert _in_standing_quiet_hours(u, now=_utc(12, 30), session=s) is True
        assert _in_standing_quiet_hours(u, now=_utc(13, 10), session=s) is True
        assert _in_standing_quiet_hours(u, now=_utc(13, 20), session=s) is False
        assert _morning_anchor_hhmm(u, s, _utc(13, 30).astimezone(PT), now=_utc(13, 30)) == (13, 0)
        sig = _morning_open_signal(u, s, now=_utc(13, 30))
        assert sig and "1:00pm wake" in sig
        assert _morning_open_signal(u, s, now=_utc(12, 0)) is None   # before the measured wake
    finally:
        s.close()


def test_activity_outranks_measured_today(db, model_on):
    """Precedence: a plausible activity today wins over the watch."""
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, sleep_minutes=450)
    _text_in(db, user.id, 9, 15, "up")
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(10))
        assert info.source == "activity" and info.local_hm == (9, 15)
    finally:
        s.close()


# ─── 6. fallbacks: typical / profile / flag off ──────────────────────────────

def test_no_wearable_no_activity_is_profile_and_byte_identical(db, model_on):
    from heartbeat import _in_standing_quiet_hours, _morning_open_signal
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(11, 40))
        assert info and info.source == "profile" and info.local_hm == (11, 30)
        for h, mi in ((1, 20), (1, 40), (9, 0), (11, 40), (11, 50), (12, 30), (20, 0)):
            assert (_in_standing_quiet_hours(u, now=_utc(h, mi), session=s)
                    == _in_standing_quiet_hours(u, now=_utc(h, mi), session=None)), (h, mi)
        assert _in_standing_quiet_hours(u, now=_utc(11, 40), session=s) is True
        assert _in_standing_quiet_hours(u, now=_utc(11, 50), session=s) is False
        sig = _morning_open_signal(u, s, now=_utc(11, 45))
        assert sig and "11:30am wake" in sig and "wake source" not in sig   # profile: no note
    finally:
        s.close()


def test_measured_typical_layer_is_unchanged(db, model_on):
    """Four steady nights at 09:00 (stale rows, so no measured_today) → typical 09:00
    REPLACES the profile hours in the quiet window exactly as #146 did."""
    from models import WearableDay
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, user.id)
    for i in range(0, 4):
        wake = _local(9, 0, day_offset=-i)
        bed = _local(1, 0, day_offset=-i)
        db.add(WearableDay(user_id=user.id, provider="google_health", day=wake.date().isoformat(),
                           sleep_minutes=480, sleep_start=_naive(bed), sleep_end=_naive(wake),
                           synced_at=_naive(wake) + timedelta(hours=20)))
    db.commit()
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(10))
        assert info and info.source == "measured_typical" and info.local_hm == (9, 0)
        assert _in_standing_quiet_hours(u, now=_utc(9, 30), session=s) is False   # typical: 00:30 .. 09:15
        assert _in_standing_quiet_hours(u, now=_utc(9, 5), session=s) is True
    finally:
        s.close()


def test_flag_off_is_byte_identical_to_today(db, model_on, monkeypatch):
    """WAKE_MODEL_ENABLED off: the card open at 10:40 changes nothing — quiet holds until
    11:45, anchor stays 11:30, resolve_wake is None."""
    from heartbeat import _in_standing_quiet_hours, _morning_anchor_hhmm, _morning_open_signal
    from wake_model import resolve_wake
    monkeypatch.setattr(config, "WAKE_MODEL_ENABLED", False)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _active_at(db, user, 10, 40)
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, sleep_minutes=450)
    s, u = _fresh(db, user)
    try:
        assert resolve_wake(u, s, now=_utc(11)) is None
        assert _in_standing_quiet_hours(u, now=_utc(11), session=s) is True
        assert _in_standing_quiet_hours(u, now=_utc(11, 50), session=s) is False
        assert _morning_anchor_hhmm(u, s, _utc(11).astimezone(PT), now=_utc(11)) == (11, 30)
        assert _morning_open_signal(u, s, now=_utc(11)) is None
        sig = _morning_open_signal(u, s, now=_utc(11, 45))
        assert sig and "wake source" not in sig
    finally:
        s.close()


def test_model_exception_fails_open(db, model_on, monkeypatch):
    import wake_model
    from heartbeat import _in_standing_quiet_hours, _morning_open_signal

    def boom(*a, **k):
        raise RuntimeError("wake model exploded")
    monkeypatch.setattr(wake_model, "_activity_wake", boom)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _active_at(db, user, 10, 40)
    s, u = _fresh(db, user)
    try:
        assert wake_model.resolve_wake(u, s, now=_utc(11)) is None
        assert _in_standing_quiet_hours(u, now=_utc(11), session=s) is True
        assert _morning_open_signal(u, s, now=_utc(11, 45)) is not None
    finally:
        s.close()


# ─── 7. the floor window path (QUIET_HOURS_FROM_PROFILE_ENABLED off = prod default) ──

def test_floor_window_path_follows_the_model_too(db, model_on, monkeypatch):
    from heartbeat import _in_standing_quiet_hours
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", False)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_START_HOUR", 21)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_END_HOUR", 8)
    # profile wake 12:00 extends the 8am floor to noon; a text at 11:00 lifts it at 11:15
    texter = _late_riser(db)
    _text_in(db, texter.id, 11, 0)
    # profile 11:30; a fresh measured 08:30 wake lifts at 08:45
    watch = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_health(db, watch.id)
    _today_sleep(db, watch.id, wake_h=8, wake_m=30, sleep_minutes=450)
    s, t = _fresh(db, texter)
    try:
        assert _in_standing_quiet_hours(t, now=_utc(11, 20), session=None) is True
        assert _in_standing_quiet_hours(t, now=_utc(11, 20), session=s) is False
        w = s.get(type(t), watch.id)
        assert _in_standing_quiet_hours(w, now=_utc(9), session=None) is True
        assert _in_standing_quiet_hours(w, now=_utc(9), session=s) is False
        assert _in_standing_quiet_hours(w, now=_utc(8, 40), session=s) is True
        assert _in_standing_quiet_hours(w, now=_utc(22), session=s) is True       # evening START unchanged
    finally:
        s.close()


# ─── 8. hygiene ───────────────────────────────────────────────────────────────

def test_resolve_wake_performs_no_db_writes(db, model_on):
    from sqlalchemy import text
    from wake_model import resolve_wake
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _active_at(db, user, 10, 40)
    _text_in(db, user.id, 10, 50)
    _connect_health(db, user.id)
    _today_sleep(db, user.id, wake_h=8, wake_m=30, sleep_minutes=450)

    def snapshot():
        from models import get_session
        s = get_session()
        try:
            users = [tuple(r) for r in s.execute(text("SELECT * FROM users WHERE id=:i"), {"i": user.id})]
            msgs = [tuple(r) for r in s.execute(text("SELECT * FROM messages WHERE user_id=:i ORDER BY id"), {"i": user.id})]
            wd = [tuple(r) for r in s.execute(text("SELECT * FROM wearable_days WHERE user_id=:i ORDER BY id"), {"i": user.id})]
            return users, msgs, wd
        finally:
            s.close()

    before = snapshot()
    s, u = _fresh(db, user)
    try:
        for h in (3, 9, 11, 14, 22):
            resolve_wake(u, s, now=_utc(h))
        s.commit()
    finally:
        s.close()
    assert snapshot() == before


def test_156_live_case_still_passes_the_guardrail(db, model_on, monkeypatch):
    """Regression guard for #156: wake 11:30, now 12:04, Discussion 12:30, nothing since
    wake — the brief is owed and the tick reaches the model (no wearable, no activity →
    profile; the model changes nothing here)."""
    from events import upsert_external_event, calendar_block_soon
    from heartbeat import guardrail_reason, _morning_open_signal
    monkeypatch.setattr(config, "CALENDAR_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_BRIEFING_GUARANTEE_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_HARD_BLOCK_MINUTES", 20)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    upsert_external_event(user.id, source="gcal", external_id="disc", title="Discussion",
                          occurred_at=_naive(_local(12, 30)), ends_at=_naive(_local(14, 0)), all_day=False)
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert calendar_block_soon(u.id, now=now) is True
        sig = _morning_open_signal(u, s, now=now)
        assert sig and "MORNING OPEN" in sig and "11:30am wake" in sig and "wake source" not in sig
        assert guardrail_reason(u, s, now=now) is None
    finally:
        s.close()


def test_wake_resolved_is_logged_for_today_sources(db, model_on, caplog):
    import logging
    from heartbeat import _in_standing_quiet_hours
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _active_at(db, user, 10, 40)
    s, u = _fresh(db, user)
    try:
        with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
            _in_standing_quiet_hours(u, now=_utc(11), session=s)
        assert any("HEARTBEAT_WAKE_RESOLVED" in r.getMessage() and "source=activity" in r.getMessage()
                   and "hm=10:40" in r.getMessage() for r in caplog.records)
    finally:
        s.close()
