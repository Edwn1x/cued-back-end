"""
Tier-2 (live) anchor for the reported-maintenance path (calculator changes,
2026-09-22). Binary, parametrized x3 per the founder rule.

  A 16-year-old at 172.6 lb / 5'2" (computed maintenance 1921, target 1400) says her
  app has her maintaining at 2200 and asks for 1700. Before this change 1700 was
  outside the 15% band and the coach could only offer 1610. Now the loop must call
  set_targets WITH `maintenance`, code centres the band on 1700, and the reply
  quotes 1700 as set — never 1610, never a refusal.
Run: pytest tests/tier2/test_reported_maintenance_live.py --run-tier2 -s
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.tier2

AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", current_split="none",
               workout_days="3-4", workout_time="18:00", wake_time="06:50", sleep_time="00:00",
               height_ft=5, height_in=2, weight_lbs=172.6, age=16, gender="female", goal="fat_loss",
               avg_steps=4000, user_timezone="America/Los_Angeles",
               calorie_target=1400, protein_target=122, calorie_target_computed=1400,
               protein_target_computed=122, targets_source="computed",
               existing_tools="mynetdiary")


@pytest.mark.parametrize("run", [1, 2, 3])
def test_app_maintenance_lets_her_pick_1700(db, monkeypatch, run):
    import config
    from agent_loop import run_agent_loop
    from models import get_session, User
    from tests.factories import make_user

    for f in ("SINGLE_AGENT_LOOP_ENABLED", "SET_TARGETS_TOOL_ENABLED", "LOG_MEAL_TOOL_ENABLED",
              "MANAGE_LOG_TOOL_ENABLED", "REMEMBER_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    user = make_user(db, **AISLINN)
    s = get_session()
    try:
        u = s.get(User, user.id)
        reply = run_agent_loop(
            u, "mynetdiary has had me maintaining at like 2200 for months. 1400 feels way too low, "
               "can we do 1700?", "freeform")
    finally:
        s.close()
    s = get_session()
    try:
        u = s.get(User, user.id)
        cal, src, rep = u.calorie_target, u.targets_source, u.reported_maintenance
    finally:
        s.close()
    low = (reply or "").lower()
    assert rep == 2200, f"maintenance not stored (set_targets called without it?): {rep} — reply: {reply!r}"
    assert cal == 1700 and src == "user", f"target not set to her pick: {cal}/{src} — reply: {reply!r}"
    assert re.search(r"\b1,?700\b", low), f"reply doesn't confirm 1700: {reply!r}"
    assert not re.search(r"\b1,?610\b", low), f"reply still offers the old band edge 1610: {reply!r}"
