"""TDEE uses REAL wearable steps when TDEE_WEARABLE_STEPS_ENABLED is on.

The activity multiplier historically read the STATIC onboarding user.avg_steps, which is
never refreshed. When a user has a connected wearable with recent step data, the flag lets
the calculator prefer a trailing average of REAL steps (wearable_read.recent_step_avg).

FLAG DEFAULT OFF — this changes users' calorie targets, so it stays off until the founder
reviews. These tests assert:
  • flag OFF  → avg_steps unchanged (today's behaviour), even with wearable rows present;
  • flag ON + recent wearable steps → the multiplier uses the real steps;
  • flag ON but NO wearable data → fail-open to avg_steps.
The consumption side only — never exercises the google_health SYNC pipeline (rows seeded
directly, same shape the sync writes).
"""
from __future__ import annotations

from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import pytest

import config
from macro_calculator import calculate_targets
from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")

# An adult profile whose STATIC avg_steps says sedentary (level 0, ×1.2)...
SEDENTARY_STEPS = 4000
# ...while the wearable shows very-active days (level 3, ×1.725).
ACTIVE_STEPS = 12000


def _today() -> date:
    return datetime.now(TZ).date()


def _d(offset: int) -> str:
    return (_today() + timedelta(days=offset)).isoformat()


def _connect(db, user_id, *, status="connected"):
    from models import Integration
    db.add(Integration(user_id=user_id, provider="google_health", status=status,
                       external_id="EXT", meta={}))
    db.commit()


def _seed_steps(db, user_id, steps, days=7):
    from models import WearableDay
    for i in range(1, days + 1):
        db.add(WearableDay(user_id=user_id, provider="google_health", day=_d(-i), steps=steps))
    db.commit()


def _adult(db, **kw):
    base = dict(height_ft=5, height_in=10, weight_lbs=175, age=30, gender="male",
                goal="general_fitness", workout_days="3", avg_steps=SEDENTARY_STEPS)
    base.update(kw)
    return make_user(db, **base)


@pytest.fixture(autouse=True)
def _ghealth_on(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    yield


def test_flag_off_uses_static_avg_steps_even_with_wearable_rows(db, monkeypatch):
    monkeypatch.setattr(config, "TDEE_WEARABLE_STEPS_ENABLED", False)
    user = _adult(db)
    _connect(db, user.id)
    _seed_steps(db, user.id, ACTIVE_STEPS)
    db.expire_all()
    from models import User
    t = calculate_targets(db.get(User, user.id), db)
    # avg_steps=4000 → sedentary; wearable rows are IGNORED with the flag off.
    assert t["activity_level"] == 0


def test_flag_on_uses_real_wearable_steps(db, monkeypatch):
    monkeypatch.setattr(config, "TDEE_WEARABLE_STEPS_ENABLED", True)
    user = _adult(db)
    _connect(db, user.id)
    _seed_steps(db, user.id, ACTIVE_STEPS)
    db.expire_all()
    from models import User
    t = calculate_targets(db.get(User, user.id), db)
    # real 12k steps → very active, overriding the static 4k avg_steps.
    assert t["activity_level"] == 3


def test_flag_on_but_no_wearable_data_falls_back_to_avg_steps(db, monkeypatch):
    monkeypatch.setattr(config, "TDEE_WEARABLE_STEPS_ENABLED", True)
    user = _adult(db)
    _connect(db, user.id)
    # no WearableDay rows seeded
    db.expire_all()
    from models import User
    t = calculate_targets(db.get(User, user.id), db)
    assert t["activity_level"] == 0   # fail-open to static avg_steps


def test_flag_on_too_few_days_falls_back_to_avg_steps(db, monkeypatch):
    monkeypatch.setattr(config, "TDEE_WEARABLE_STEPS_ENABLED", True)
    user = _adult(db)
    _connect(db, user.id)
    _seed_steps(db, user.id, ACTIVE_STEPS, days=2)   # < STEP_AVG_MIN_DAYS
    db.expire_all()
    from models import User
    t = calculate_targets(db.get(User, user.id), db)
    assert t["activity_level"] == 0   # not enough days → static avg_steps


def test_no_session_is_todays_behaviour(db, monkeypatch):
    """calculate_targets(user) with no session is unchanged even with the flag on."""
    monkeypatch.setattr(config, "TDEE_WEARABLE_STEPS_ENABLED", True)
    user = _adult(db)
    _connect(db, user.id)
    _seed_steps(db, user.id, ACTIVE_STEPS)
    db.expire_all()
    from models import User
    t = calculate_targets(db.get(User, user.id))   # no session passed
    assert t["activity_level"] == 0
