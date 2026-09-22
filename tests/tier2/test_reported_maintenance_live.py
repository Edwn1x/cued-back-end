"""
Tier-2 (live) anchor for the reported-maintenance path (calculator changes,
2026-09-22). Binary, parametrized x3 per the founder rule.

  A 16-year-old at 172.6 lb / 5'2" (computed maintenance 1921, target 1750 under the
  teen −10% rule) says her app has her maintaining at 2200 and asks for 2100. Around
  the computed 1750 the 15% band tops out at 2010, so 2100 was a refusal; with her
  maintenance the band centres on 2200 × 0.9 = 2000 and 2100 is allowed. The loop must
  call set_targets WITH `maintenance`, and the reply quotes 2100 as set — never 2010,
  never a refusal.
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
               calorie_target=1750, protein_target=137, calorie_target_computed=1750,
               protein_target_computed=137, targets_source="computed",
               existing_tools="mynetdiary")


@pytest.mark.parametrize("run", [1, 2, 3])
def test_app_maintenance_lets_her_pick_2100(db, monkeypatch, run):
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
            u, "mynetdiary has had me maintaining at like 2200 for months so 1750 feels low, "
               "can we do 2100?", "freeform")
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
    assert cal == 2100 and src == "user", f"target not set to her pick: {cal}/{src} — reply: {reply!r}"
    assert re.search(r"\b2,?100\b", low), f"reply doesn't confirm 2100: {reply!r}"
    assert not re.search(r"\b2,?010\b", low), f"reply still offers the old band edge 2010: {reply!r}"
