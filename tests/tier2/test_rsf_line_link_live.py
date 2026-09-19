"""
Tier-2 (live) — RSF line §2.8 backstop. The deterministic path (short 'heading out'
→ link in code) is proven in tier-1; this checks the MODEL path for mixed texts:
with the line on, the RSF context block names the exact join link as the one
exception to the no-links rule — does the coach actually give it? And with the
line off, does it stay link-free? Run: pytest --run-tier2 -s tests/tier2/test_rsf_line_link_live.py
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2


def _reading(pct):
    from models import get_session, GymOccupancy
    s = get_session()
    try:
        s.add(GymOccupancy(facility="rsf_weights", ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
                           pct=pct, est_wait_min=None, raw={}))
        s.commit()
    finally:
        s.close()


def _enable(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "RSF_METER_ENABLED", "LOG_MEAL_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


@pytest.mark.parametrize("text", ["heading out to the gym, had a bagel and a coffee first",
                                  "ok walking over to rsf now, legs day. also i slept like 5 hours lol"])
def test_mixed_departure_with_the_line_on_gets_the_exact_link(db, monkeypatch, text):
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from integrations.waitwell.client import JOIN_URL
    user = make_user(db, name="Sam", confirmed_training_days="mon,tue,wed,thu,fri,sat,sun", workout_time="17:00")
    _reading(97)
    reply = run_agent_loop(user, text, "freeform")
    print(f"\n[LINE ON] {text!r}\n  -> {reply!r}")
    assert JOIN_URL in reply, f"expected the exact join link in the reply: {reply!r}"


def test_departure_with_the_line_off_stays_link_free(db, monkeypatch):
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    user = make_user(db, name="Sam", confirmed_training_days="mon,tue,wed,thu,fri,sat,sun", workout_time="17:00")
    _reading(52)
    reply = run_agent_loop(user, "heading out to the gym, had a bagel and a coffee first", "freeform")
    print(f"\n[LINE OFF] -> {reply!r}")
    assert "waitwell" not in reply.lower() and "http" not in reply.lower(), reply
