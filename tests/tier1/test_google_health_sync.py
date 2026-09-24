"""Part 2a — Google Health sync into wearable_days / weight_logs, the webhook route +
notification fan-out, and the `## WEARABLE (fitbit)` context block (mocked API)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_HEALTH_WEBHOOK_SECRET", "Bearer hook-secret")
    monkeypatch.setattr(config, "GOOGLE_HEALTH_SYNC_DAYS", 2)
    monkeypatch.setattr(config, "GOOGLE_HEALTH_BACKFILL_DAYS", 5)
    yield


def _today() -> date:
    return datetime.now(TZ).date()


def _d(offset: int) -> str:
    return (_today() + timedelta(days=offset)).isoformat()


def _utc(local_iso: str) -> datetime:
    return datetime.fromisoformat(local_iso).replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def _rfc(local_iso: str) -> str:
    return _utc(local_iso).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connected(db, user_id, *, external_id="111111256096816351", status="connected", meta=None, updated_at=None):
    from models import get_session, Integration
    s = get_session()
    try:
        row = Integration(user_id=user_id, provider="google_health", status=status, external_id=external_id,
                          meta=meta or {})
        if updated_at:
            row.updated_at = updated_at
        s.add(row)
        s.commit()
    finally:
        s.close()


class _API:
    """A fake google_health client surface: records the windows asked for, serves canned data."""

    def __init__(self):
        self.windows = []
        self.steps = {_d(-1): 8123, _d(0): 4210}
        self.calories = {_d(0): 2410}
        self.azm = {_d(0): 32}
        self.rhr = {_d(-1): 55, _d(0): 59}
        self.hrv = {_d(0): 34.0, _d(-1): 41.0}
        off = f"{int(datetime.now(TZ).utcoffset().total_seconds())}s"
        self.sleep = [
            {"name": "s-main", "type": "MAIN_SLEEP", "summary": {"minutesAsleep": "372", "minutesAwake": "35"},
             "interval": {"startTime": _rfc(f"{_d(-1)}T23:48:00"), "endTime": _rfc(f"{_d(0)}T06:31:00"),
                          "startUtcOffset": off, "endUtcOffset": off}},
            {"name": "s-nap", "type": "NAP", "summary": {"minutesAsleep": "40", "minutesAwake": "2"},   # a nap, same day
             "interval": {"startTime": _rfc(f"{_d(0)}T14:00:00"), "endTime": _rfc(f"{_d(0)}T14:45:00"),
                          "startUtcOffset": off, "endUtcOffset": off}},
        ]
        self.weights = [{"name": "users/1/dataTypes/weight/dataPoints/w777", "sample_time": _rfc(f"{_d(0)}T07:10:00"), "lbs": 171.2}]
        self.fail_with = None

    def install(self, monkeypatch):
        from integrations import google_health as gh, base

        def _guard():
            if self.fail_with:
                raise self.fail_with

        def steps(tok, s, e):
            _guard(); self.windows.append((s.isoformat(), e.isoformat())); return dict(self.steps)
        monkeypatch.setattr(gh, "get_steps_by_day", steps)
        monkeypatch.setattr(gh, "get_calories_by_day", lambda tok, s, e: dict(self.calories))
        monkeypatch.setattr(gh, "get_active_zone_minutes_by_day", lambda tok, s, e: dict(self.azm))
        monkeypatch.setattr(gh, "get_resting_hr_by_day", lambda tok, s, e: dict(self.rhr))
        monkeypatch.setattr(gh, "get_hrv_by_day", lambda tok, s, e: dict(self.hrv))
        monkeypatch.setattr(gh, "get_sleep_sessions", lambda tok, s: list(self.sleep))
        monkeypatch.setattr(gh, "get_weight_samples", lambda tok, s: list(self.weights))
        monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
        return self


def test_sync_writes_days_main_sleep_only_and_weight(db, monkeypatch):
    from integrations import google_health_sync as ghs
    from models import WearableDay, WeightLog, User
    user = make_user(db, weight_lbs=175.0, protein_target=150, targets_source="calculator")
    _connected(db, user.id)
    api = _API().install(monkeypatch)

    out = ghs.sync_user(user.id)
    assert out["days"] == 2 and out["new_weights"] == 1
    assert api.windows[0] == (_d(-4), _d(0))          # first sync → backfill window (5 days)

    db.expire_all()
    rows = {r.day: r for r in db.query(WearableDay).filter_by(user_id=user.id).all()}
    t = rows[_d(0)]
    assert t.steps == 4210 and t.resting_hr == 59 and t.hrv_rmssd == 34.0
    assert t.sleep_minutes == 372 and t.sleep_efficiency == 91          # MAIN_SLEEP, not the nap; 372/(372+35)
    assert t.calories_out == 2410 and t.active_minutes == 32
    assert t.sleep_start == _utc(f"{_d(-1)}T23:48:00") and t.sleep_end == _utc(f"{_d(0)}T06:31:00")
    assert rows[_d(-1)].steps == 8123 and rows[_d(-1)].sleep_minutes is None

    w = db.query(WeightLog).filter_by(user_id=user.id).all()
    assert len(w) == 1 and w[0].weight_lbs == 171.2 and w[0].notes == "ghealth:users/1/dataTypes/weight/dataPoints/w777"
    assert db.get(User, user.id).weight_lbs == 171.2                    # latest-wins like log_weight


def test_second_sync_is_idempotent_and_uses_the_short_window(db, monkeypatch):
    from integrations import google_health_sync as ghs
    from models import WearableDay, WeightLog, Integration
    user = make_user(db)
    _connected(db, user.id)
    api = _API().install(monkeypatch)
    ghs.sync_user(user.id)
    api.steps[_d(0)] = 6000                        # the watch synced more steps
    api.windows.clear()

    out = ghs.sync_user(user.id)
    assert api.windows == [(_d(-1), _d(0))]
    db.expire_all()
    assert db.query(WearableDay).filter_by(user_id=user.id).count() == 2
    assert db.query(WearableDay).filter_by(user_id=user.id, day=_d(0)).one().steps == 6000
    assert db.query(WeightLog).filter_by(user_id=user.id).count() == 1      # w777 not re-inserted
    row = db.query(Integration).filter_by(user_id=user.id, provider="google_health").one()
    assert row.meta.get("first_sync_done") is True and row.meta.get("last_sync_at")
    assert out["new_weights"] == 0


def test_api_failure_counts_then_errors_and_heals(db, monkeypatch):
    from integrations import google_health_sync as ghs, google_health as gh
    from models import Integration
    user = make_user(db)
    _connected(db, user.id)
    api = _API().install(monkeypatch)
    api.fail_with = gh.HealthAPIError(500, "/x", "boom")
    for _ in range(3):
        assert "error" in ghs.sync_user(user.id)
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="google_health").one()
    assert row.status == "error" and row.meta["fail_count"] == 3
    api.fail_with = None
    assert ghs.sync_all() == 1        # error rows are still polled …
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="google_health").one()
    assert row.status == "connected" and row.meta["fail_count"] == 0      # … and heal


def test_401_marks_revoked_so_the_coach_can_reoffer(db, monkeypatch):
    from integrations import google_health_sync as ghs, google_health as gh, base
    from models import Integration
    user = make_user(db)
    _connected(db, user.id)
    api = _API().install(monkeypatch)
    api.fail_with = gh.HealthAPIError(401, "/x", "UNAUTHENTICATED")
    assert ghs.sync_user(user.id) == {"error": "revoked"}
    db.expire_all()
    assert db.query(Integration).filter_by(user_id=user.id, provider="google_health").one().status == "revoked"
    assert base.status_line(user.id) == "google_health disconnected"


def test_flag_off_is_a_noop(db, monkeypatch):
    from integrations import google_health_sync as ghs
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    user = make_user(db)
    _connected(db, user.id)
    assert ghs.sync_user(user.id) == {"skipped": "flag off"}
    assert ghs.sync_all() == 0


# ─── webhook ─────────────────────────────────────────────────────────────────

def test_notifications_fan_out_by_health_user_id_single_and_batched(db, monkeypatch):
    from integrations import google_health_sync as ghs
    a = make_user(db); b = make_user(db); c = make_user(db)
    _connected(db, a.id, external_id="AAA")
    _connected(db, b.id, external_id="BBB")
    _connected(db, c.id, external_id="CCC", status="revoked")
    synced = []
    monkeypatch.setattr(ghs, "sync_user", lambda uid, **kw: synced.append(uid))
    one = {"data": {"version": "1", "healthUserId": "AAA", "operation": "UPSERT", "dataType": "steps", "intervals": []}}
    assert ghs.handle_notifications(one, run_async=False) == [a.id]
    batched = [one, {"data": {"healthUserId": "AAA", "dataType": "sleep"}},
               {"data": {"healthUserId": "BBB", "dataType": "weight"}},
               {"data": {"healthUserId": "CCC", "dataType": "weight"}},      # revoked → skipped
               {"healthUserId": "NOPE", "dataType": "weight"}]               # unknown, bare shape → skipped
    assert ghs.handle_notifications(batched, run_async=False) == sorted([a.id, b.id])
    assert sorted(synced) == sorted([a.id, a.id, b.id])                       # once per user per delivery
    assert ghs.handle_notifications("garbage", run_async=False) == []


def test_webhook_verification_handshake(client):
    hdr = {"Authorization": "Bearer hook-secret"}
    assert client.post("/oauth/google_health/webhook", json={"type": "verification"}, headers=hdr).status_code == 201
    assert client.post("/oauth/google_health/webhook", json={"type": "verification"}).status_code == 401
    assert client.post("/oauth/google_health/webhook", json={"type": "verification"},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_webhook_notification_acks_204_and_kicks_sync(client, db, monkeypatch):
    from integrations import google_health_sync as ghs
    user = make_user(db)
    _connected(db, user.id, external_id="AAA")
    kicked = []
    monkeypatch.setattr(ghs, "sync_user", lambda uid, **kw: kicked.append(uid))
    orig = ghs.handle_notifications                       # run inline so the assertion doesn't race the thread
    monkeypatch.setattr(ghs, "handle_notifications", lambda p, run_async=True: orig(p, run_async=False))
    hdr = {"Authorization": "Bearer hook-secret"}
    r = client.post("/oauth/google_health/webhook", headers=hdr,
                    json={"data": {"healthUserId": "AAA", "dataType": "sleep", "operation": "UPSERT"}})
    assert r.status_code == 204 and kicked == [user.id]
    assert client.post("/oauth/google_health/webhook", headers=hdr, data="not json").status_code == 204   # never a retry storm
    monkeypatch.setattr(config, "GOOGLE_HEALTH_WEBHOOK_SECRET", "")
    assert client.post("/oauth/google_health/webhook", headers=hdr, json={"type": "verification"}).status_code == 401


# ─── context block ───────────────────────────────────────────────────────────

def _day(db, user_id, day, **f):
    from models import WearableDay
    db.add(WearableDay(user_id=user_id, provider="google_health", day=day, **f))
    db.commit()


def test_wearable_context_renders_night_steps_and_baseline_deviation(db):
    from integrations.google_health_sync import wearable_context
    from models import User
    user = make_user(db)
    _connected(db, user.id, updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=12))
    for i in range(1, 7):
        _day(db, user.id, _d(-i), steps=9000, resting_hr=55, hrv_rmssd=41.0, sleep_minutes=400)
    _day(db, user.id, _d(0), steps=4210, resting_hr=60, hrv_rmssd=33.0, sleep_minutes=372,
         sleep_start=_utc(f"{_d(-1)}T23:48:00"), sleep_end=_utc(f"{_d(0)}T06:31:00"))
    db.expire_all()
    ctx = wearable_context(db.get(User, user.id), db)
    assert ctx.startswith("## WEARABLE (fitbit)")
    assert "last night: 6h12m (11:48pm–6:31am), 7-day avg 6h" in ctx
    assert "steps today: 4,210 so far · 7-day avg 9,000" in ctx
    assert "resting HR: 60 (7-day avg 56)" in ctx and "HRV 33ms (7-day avg 40ms)" in ctx
    assert "HR and HRV worse than their baseline" in ctx
    assert "synced 12 min ago" in ctx and "Never diagnose" in ctx


def test_wearable_context_stale_connection_and_not_connected(db):
    from integrations.google_health_sync import wearable_context
    from models import User
    user = make_user(db)
    assert wearable_context(db.get(User, user.id), db) == ""          # no row → nothing
    _connected(db, user.id)
    _day(db, user.id, _d(-6), steps=9000, sleep_minutes=400)          # only an old day
    db.expire_all()
    assert "nothing synced in the last few days" in wearable_context(db.get(User, user.id), db)


def test_wearable_block_reaches_the_loop_context(db, monkeypatch):
    from agent_loop import build_loop_context
    from models import User
    user = make_user(db)
    _connected(db, user.id)
    _day(db, user.id, _d(0), steps=4210, sleep_minutes=372)
    db.expire_all()
    u = db.get(User, user.id)
    ctx = build_loop_context(u, db)
    assert "## WEARABLE (fitbit)" in ctx and "## INTEGRATIONS\ngoogle_health connected" in ctx
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    assert "## WEARABLE" not in build_loop_context(u, db)
