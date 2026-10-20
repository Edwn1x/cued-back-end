"""Live 2026-10-17 16:47 PT (founder): "Yeah ima head to the gym in a bit" → no heading-out
match (no "ima", leading "Yeah"); he found the RSF line on arrival. The detector now knows the
"ima/imma/i'm gonna/bout to" family and a leading affirmation."""
from __future__ import annotations

import pytest

from tests.factories import make_user
from tests.tier1.test_rsf_meter_line import FOUNDER, meter_only, _open_now, _reading  # noqa: F401


@pytest.mark.parametrize("phrase", [
    "Yeah ima head to the gym in a bit",
    "imma hit the gym rn",
    "ok gonna go lift",
    "alr omw to rsf",
    "i'm gonna head over to the gym",
    "bout to go to the gym",
    "yea ima go lift",
])
def test_new_phrasings_match_the_detector(db, meter_only, monkeypatch, phrase):
    import gym_beats
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    _reading(db, 97)
    b = gym_beats.heading_out(user.id, phrase)
    assert b is not None and b.kind == "line_d1", phrase


@pytest.mark.parametrize("phrase", [
    "ima eat first then maybe gym later idk",
    "yeah the gym was packed",
    "ima skip the gym today",
    "imma go to class",
])
def test_non_departures_still_fall_through(db, meter_only, monkeypatch, phrase):
    import gym_beats
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    _reading(db, 97)
    assert gym_beats.heading_out(user.id, phrase) is None, phrase


def test_with_the_line_off_the_live_text_is_still_a_normal_turn(db, meter_only, monkeypatch):
    """The 10-17 reading was 53% — the detector matching would still (correctly) say nothing."""
    import gym_beats
    user = make_user(db, **FOUNDER)
    _open_now(monkeypatch)
    _reading(db, 53)
    assert gym_beats.heading_out(user.id, "Yeah ima head to the gym in a bit") is None


# ─── the meter is the floor, not the door ─────────────────────────────────────

def test_context_block_says_the_meter_cannot_see_the_door_when_the_line_is_off():
    import gym_beats
    from datetime import datetime, timezone
    reading = {"pct": 43, "label": "light", "line_on": False, "est_wait_min": None,
               "as_of": datetime(2026, 10, 19, 2, 43, tzinfo=timezone.utc).replace(tzinfo=None)}
    block = gym_beats.rsf_context_block(reading)
    assert "43%" in block
    assert "cannot see the entrance line" in block and "never 'no line'" in block


def test_context_block_with_the_line_on_still_carries_the_join_link():
    import gym_beats
    from integrations.waitwell.client import JOIN_URL
    from datetime import datetime, timezone
    reading = {"pct": 97, "label": "packed", "line_on": True, "est_wait_min": None,
               "as_of": datetime(2026, 10, 19, 2, 43, tzinfo=timezone.utc).replace(tzinfo=None)}
    block = gym_beats.rsf_context_block(reading)
    assert JOIN_URL in block and "cannot see the entrance line" not in block
