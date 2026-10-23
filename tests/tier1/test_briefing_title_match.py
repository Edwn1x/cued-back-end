"""Live 2026-10-21 12:59 PT: BRIEFING_DROPPED_ITEM item=due_today detail='CS 70 HW Due' on a
briefing that (per the matcher's own docstring) should match 'CS 70 hw'. Fusing both
letter→digit and digit→letter gaps at once turned "cs 70 hw" into "cs70hw", so the fused
candidate "cs70" never sat on a boundary. Each fusion direction is now tried on its own."""
import pytest


@pytest.mark.parametrize("text", [
    "cs70 hw due at 4",
    "cs 70 hw due at 4 today",
    "CS 70 HW due today at 4pm, then discussion",
    "cs70 hw lands wed",
    "the cs 70 homework is due at 4",
])
def test_spaced_and_fused_course_codes_both_match(text):
    import heartbeat
    assert heartbeat._mentions_title(text, "CS 70 HW Due"), text


@pytest.mark.parametrize("text", ["hw due today at 4", "nothing due today", "61c lecture at 11"])
def test_generic_paraphrases_still_count_as_drops(text):
    import heartbeat
    assert not heartbeat._mentions_title(text, "CS 70 HW Due"), text


# ─── the due-TODAY guarantee ──────────────────────────────────────────────────

def _parts(**over):
    base = {"weather": "67° and overcast", "due_today": ["CS 70 HW Due"], "due_today_times": {"CS 70 HW Due": "4:00pm"},
            "due_soon": [], "gym_window": None, "nutrition": None}
    base.update(over)
    return base


def test_live_weather_only_briefing_gets_the_due_today_bubble_appended(caplog):
    import logging, heartbeat
    with caplog.at_level(logging.INFO):
        out = heartbeat._enforce_briefing("67° and overcast out", _parts(), 51)
    assert out == "67° and overcast out\n---\nalso: cs 70 hw due today by 4:00pm"
    assert "BRIEFING_DUE_TODAY_APPENDED" in caplog.text


def test_mentioned_due_today_item_is_left_alone():
    import heartbeat
    msg = "67° and overcast out. cs70 hw due at 4, gym gap til 3"
    assert heartbeat._enforce_briefing(msg, _parts(), 51) == msg


def test_all_day_item_has_no_time_clause():
    import heartbeat
    parts = _parts(due_today=["Homework 4 [CS61C]"], due_today_times={})
    out = heartbeat._enforce_briefing("67° and overcast out", parts, 51)
    assert out.endswith("\n---\nalso: homework 4 [cs61c] due today")


def test_weather_prepend_and_due_today_append_compose():
    import heartbeat
    out = heartbeat._enforce_briefing("nothing much today, chill", _parts(), 51)
    assert out.startswith("67° and overcast") and out.endswith("also: cs 70 hw due today by 4:00pm")


def test_due_today_guarantee_flag_off(monkeypatch):
    import config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_DUE_TODAY_GUARANTEE_ENABLED", False)
    assert heartbeat._enforce_briefing("67° and overcast out", _parts(), 51) == "67° and overcast out"
