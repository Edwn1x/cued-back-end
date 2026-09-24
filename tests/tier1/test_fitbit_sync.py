"""Part 2a — Fitbit sync into wearable_days / weight_logs, the push-notification fan-out,
the subscriber routes, and the `## WEARABLE (fitbit)` context block (mocked API)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "FITBIT_ENABLED", True)
    monkeypatch.setattr(config, "FITBIT_SUBSCRIBER_VERIFY_CODE", "verify-me")
    monkeypatch.setattr(config, "FITBIT_SYNC_DAYS", 2)
    monkeypatch.setattr(config, "FITBIT_BACKFILL_DAYS", 5)
    yield


def _today() -> date:
    return datetime.now(TZ).date()


def _d(offset: int) -> str:
    return (_today() + timedelta(days=offset)).isoformat()


def _connected(db, user_id, *, external_id="GGNJL9", status="connected", meta=None, updated_at=None):
    from models import get_session, Integration
    s = get_session()
    try:
        row = Integration(user_id=user_id, provider="fitbit", status=status, external_id=external_id,
                          meta=meta or {})
        if updated_at:
            row.updated_at = updated_at
        s.add(row)
        s.commit()
    finally:
        s.close()


class _API:
    """A fake fitbit module surface: records the windows asked for, serves canned data."""

    def __init__(self):
        self.windows = []
        self.weight_days = []
        self.steps = {_d(-1): 8123, _d(0): 4210}
        self.rhr = {_d(-1): 55, _d(0): 59}
        self.hrv = {_d(0): 34.0, _d(-1): 41.0}
        self.sleep = [
            {"dateOfSleep": _d(0), "isMainSleep": True, "minutesAsleep": 372, "efficiency": 91,
             "startTime": f"{_d(-1)}T23:48:00.000", "endTime": f"{_d(0)}T06:31:00.000"},
            {"dateOfSleep": _d(0), "isMainSleep": False, "minutesAsleep": 40,      # a nap
             "startTime": f"{_d(0)}T14:00:00.000", "endTime": f"{_d(0)}T14:45:00.000"},
        ]
        self.summary = {"caloriesOut": 2410, "veryActiveMinutes": 12, "fairlyActiveMinutes": 20, "steps": 4210}
        self.weights = {_d(0): [{"logId": 777, "weight": 171.2, "date": _d(0), "time": "07:10:00", "source": "Aria"}]}
        self.fail_with = None

    def install(self, monkeypatch):
        from integrations import fitbit, base

        def _guard():
            if self.fail_with:
                raise self.fail_with

        def steps(tok, s, e):
            _guard(); self.windows.append((s, e)); return dict(self.steps)
        monkeypatch.setattr(fitbit, "get_steps_series", steps)
        monkeypatch.setattr(fitbit, "get_resting_hr_series", lambda tok, s, e: dict(self.rhr))
        monkeypatch.setattr(fitbit, "get_hrv_series", lambda tok, s, e: dict(self.hrv))
        monkeypatch.setattr(fitbit, "get_sleep_range", lambda tok, s, e: list(self.sleep))
        monkeypatch.setattr(fitbit, "get_activity_summary", lambda tok, d: dict(self.summary))

        def weights(tok, d):
            self.weight_days.append(d); return list(self.weights.get(d, []))
        monkeypatch.setattr(fitbit, "get_weight_logs", weights)
        monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
        return self


def test_sync_writes_days_main_sleep_only_and_weight(db, monkeypatch):
    from integrations import fitbit_sync
    from models import WearableDay, WeightLog, User
    user = make_user(db, weight_lbs=175.0, protein_target=150, targets_source="calculator")
    _connected(db, user.id)
    api = _API().install(monkeypatch)

    out = fitbit_sync.sync_user(user.id)
    assert out["days"] == 2 and out["new_weights"] == 1
    # first sync → backfill window (5 days), not the 2-day steady state
    assert api.windows[0] == (_d(-4), _d(0))
    assert api.weight_days == [_d(-4), _d(-3), _d(-2), _d(-1), _d(0)]

    db.expire_all()
    rows = {r.day: r for r in db.query(WearableDay).filter_by(user_id=user.id).all()}
    t = rows[_d(0)]
    assert t.steps == 4210 and t.resting_hr == 59 and t.hrv_rmssd == 34.0
    assert t.sleep_minutes == 372 and t.sleep_efficiency == 91          # the MAIN sleep, not the nap
    assert t.calories_out == 2410 and t.active_minutes == 32
    # 23:48 local → naive UTC
    assert t.sleep_start == datetime.fromisoformat(f"{_d(-1)}T23:48:00").replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)
    assert rows[_d(-1)].steps == 8123 and rows[_d(-1)].sleep_minutes is None

    w = db.query(WeightLog).filter_by(user_id=user.id).all()
    assert len(w) == 1 and w[0].weight_lbs == 171.2 and w[0].notes == "fitbit:777"
    u = db.get(User, user.id)
    assert u.weight_lbs == 171.2                                          # latest-wins like log_weight


def test_second_sync_is_idempotent_and_uses_the_short_window(db, monkeypatch):
    from integrations import fitbit_sync, base
    from models import WearableDay, WeightLog, Integration
    user = make_user(db)
    _connected(db, user.id)
    api = _API().install(monkeypatch)
    fitbit_sync.sync_user(user.id)
    api.steps[_d(0)] = 6000                       # the watch synced more steps
    api.windows.clear(); api.weight_days.clear()

    out = fitbit_sync.sync_user(user.id)
    assert api.windows == [(_d(-1), _d(0))] and api.weight_days == [_d(-1), _d(0)]
    db.expire_all()
    assert db.query(WearableDay).filter_by(user_id=user.id).count() == 2
    assert db.query(WearableDay).filter_by(user_id=user.id, day=_d(0)).one().steps == 6000
    assert db.query(WeightLog).filter_by(user_id=user.id).count() == 1     # logId 777 not re-inserted
    row = db.query(Integration).filter_by(user_id=user.id, provider="fitbit").one()
    assert row.meta.get("first_sync_done") is True and row.meta.get("last_sync_at")
    assert out["new_weights"] == 0


def test_api_failure_counts_then_errors_and_heals(db, monkeypatch):
    from integrations import fitbit_sync, fitbit
    from models import Integration
    user = make_user(db)
    _connected(db, user.id)
    api = _API().install(monkeypatch)
    api.fail_with = fitbit.FitbitAPIError(500, "/x", "boom")
    for _ in range(3):
        assert "error" in fitbit_sync.sync_user(user.id)
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="fitbit").one()
    assert row.status == "error" and row.meta["fail_count"] == 3
    api.fail_with = None
    assert fitbit_sync.sync_all() == 1        # error rows are still polled …
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="fitbit").one()
    assert row.status == "connected" and row.meta["fail_count"] == 0      # … and heal


def test_401_marks_revoked_so_the_coach_can_reoffer(db, monkeypatch):
    from integrations import fitbit_sync, fitbit, base
    from models import Integration
    user = make_user(db)
    _connected(db, user.id)
    api = _API().install(monkeypatch)
    api.fail_with = fitbit.FitbitAPIError(401, "/x", "invalid_token")
    assert fitbit_sync.sync_user(user.id) == {"error": "revoked"}
    db.expire_all()
    assert db.query(Integration).filter_by(user_id=user.id, provider="fitbit").one().status == "revoked"
    assert base.status_line(user.id) == "fitbit disconnected"


def test_flag_off_is_a_noop(db, monkeypatch):
    from integrations import fitbit_sync
    monkeypatch.setattr(config, "FITBIT_ENABLED", False)
    user = make_user(db)
    _connected(db, user.id)
    assert fitbit_sync.sync_user(user.id) == {"skipped": "flag off"}
    assert fitbit_sync.sync_all() == 0


# ─── push notifications ──────────────────────────────────────────────────────

def test_notifications_fan_out_by_owner_id(db, monkeypatch):
    from integrations import fitbit_sync
    a = make_user(db); b = make_user(db); c = make_user(db)
    _connected(db, a.id, external_id="AAA")
    _connected(db, b.id, external_id="BBB")
    _connected(db, c.id, external_id="CCC", status="revoked")
    synced = []
    monkeypatch.setattr(fitbit_sync, "sync_user", lambda uid, **kw: synced.append(uid))
    payload = [{"collectionType": "sleep", "date": _d(0), "ownerId": "AAA", "ownerType": "user", "subscriptionId": f"{a.id}-sleep"},
               {"collectionType": "activities", "date": _d(0), "ownerId": "AAA", "ownerType": "user", "subscriptionId": f"{a.id}-activities"},
               {"collectionType": "body", "date": _d(0), "ownerId": "BBB", "ownerType": "user", "subscriptionId": f"{b.id}-body"},
               {"collectionType": "body", "date": _d(0), "ownerId": "CCC", "ownerType": "user", "subscriptionId": "x"},
               {"collectionType": "body", "date": _d(0), "ownerId": "NOPE", "ownerType": "user", "subscriptionId": "y"}]
    kicked = fitbit_sync.handle_notifications(payload, run_async=False)
    assert kicked == sorted([a.id, b.id]) and sorted(synced) == sorted([a.id, b.id])   # once each; revoked/unknown skipped
    assert fitbit_sync.handle_notifications({"not": "a list"}, run_async=False) == []


def test_subscriber_verify_route(client):
    assert client.get("/oauth/fitbit/subscriber?verify=verify-me").status_code == 204
    assert client.get("/oauth/fitbit/subscriber?verify=wrong").status_code == 404
    assert client.get("/oauth/fitbit/subscriber").status_code == 404


def test_subscriber_post_acks_204_and_kicks_sync(client, db, monkeypatch):
    from integrations import fitbit_sync
    user = make_user(db)
    _connected(db, user.id, external_id="AAA")
    kicked = []
    monkeypatch.setattr(fitbit_sync, "sync_user", lambda uid, **kw: kicked.append(uid))
    # run the fan-out inline so the assertion doesn't race the daemon thread
    orig = fitbit_sync.handle_notifications
    monkeypatch.setattr(fitbit_sync, "handle_notifications", lambda p, run_async=True: orig(p, run_async=False))
    r = client.post("/oauth/fitbit/subscriber", json=[{"collectionType": "sleep", "date": _d(0), "ownerId": "AAA",
                                                        "ownerType": "user", "subscriptionId": f"{user.id}-sleep"}])
    assert r.status_code == 204 and kicked == [user.id]
    # garbage body still 204s (never let Fitbit disable the subscriber)
    assert client.post("/oauth/fitbit/subscriber", data="not json").status_code == 204


# ─── context block ───────────────────────────────────────────────────────────

def _day(db, user_id, day, **f):
    from models import WearableDay
    db.add(WearableDay(user_id=user_id, provider="fitbit", day=day, **f))
    db.commit()


def test_wearable_context_renders_night_steps_and_baseline_deviation(db):
    from integrations.fitbit_sync import wearable_context
    from models import User
    user = make_user(db)
    _connected(db, user.id, updated_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=12))
    for i in range(1, 7):
        _day(db, user.id, _d(-i), steps=9000, resting_hr=55, hrv_rmssd=41.0, sleep_minutes=400)
    start = datetime.fromisoformat(f"{_d(-1)}T23:48:00").replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime.fromisoformat(f"{_d(0)}T06:31:00").replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)
    _day(db, user.id, _d(0), steps=4210, resting_hr=60, hrv_rmssd=33.0, sleep_minutes=372,
         sleep_start=start, sleep_end=end)
    db.expire_all()
    ctx = wearable_context(db.get(User, user.id), db)
    assert ctx.startswith("## WEARABLE (fitbit)")
    assert "last night: 6h12m (11:48pm–6:31am), 7-day avg 6h" in ctx
    assert "steps today: 4,210 so far · 7-day avg 9,000" in ctx
    assert "resting HR: 60 (7-day avg 56)" in ctx and "HRV 33ms (7-day avg 40ms)" in ctx
    assert "HR and HRV worse than their baseline" in ctx
    assert "synced 12 min ago" in ctx and "Never diagnose" in ctx


def test_wearable_context_stale_connection_and_not_connected(db):
    from integrations.fitbit_sync import wearable_context
    from models import User
    user = make_user(db)
    assert wearable_context(db.get(User, user.id), db) == ""          # no row → nothing
    _connected(db, user.id)
    _day(db, user.id, _d(-6), steps=9000, sleep_minutes=400)          # only an old day
    db.expire_all()
    ctx = wearable_context(db.get(User, user.id), db)
    assert "nothing synced in the last few days" in ctx


def test_wearable_block_reaches_the_loop_context(db, monkeypatch):
    from agent_loop import build_loop_context
    from models import User
    user = make_user(db)
    _connected(db, user.id)
    _day(db, user.id, _d(0), steps=4210, sleep_minutes=372)
    db.expire_all()
    u = db.get(User, user.id)
    ctx = build_loop_context(u, db)
    assert "## WEARABLE (fitbit)" in ctx and "## INTEGRATIONS\nfitbit connected" in ctx
    monkeypatch.setattr(config, "FITBIT_ENABLED", False)
    assert "## WEARABLE" not in build_loop_context(u, db)
