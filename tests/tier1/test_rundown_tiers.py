"""
The rundown, tiered (founder's change 10, 2026-10-09): code-authored from the registry,
numbered headline sections with one dash line per thing, two bubbles, only what's on for
this user. HEADLINE sections: food, workouts, connect, campus, nice-to-haves. SUB-LINES
come from capabilities with section= (dining macros, receipts, screenshot logging only if
they named a food app, stat cards, rsf, study rooms, hydration, reminders). Everything
with section=None is SHOW, don't tell (weigh-ins, demos, fix-a-log, day reset, check-in
level, targets, tasks, fetch_page) — and weather is learned from the morning brief.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import config
from capabilities import CAPABILITIES, build_rundown, rundown_sections, RUNDOWN_INTRO

ALL = ("LOG_MEAL_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED", "GET_DINING_MENU_TOOL_ENABLED", "RECEIPTS_ENABLED",
       "READ_IMAGE_ENABLED", "STAT_CARD_TOOL_ENABLED", "RSF_METER_ENABLED", "FIND_STUDY_SPACE_TOOL_ENABLED",
       "REMINDERS_ENABLED", "WATER_REMINDERS_ENABLED", "GCAL_ENABLED", "BCOURSES_ENABLED", "GOOGLE_HEALTH_ENABLED",
       "FOOD_LOGGER_BRIDGE_ENABLED", "WEATHER_ENABLED", "LOG_WEIGHT_TOOL_ENABLED", "TASKS_ENABLED",
       "FETCH_PAGE_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED", "SET_DAY_RESET_TOOL_ENABLED",
       "SET_CHECKIN_LEVEL_TOOL_ENABLED", "SET_TARGETS_TOOL_ENABLED")


@pytest.fixture
def all_on(monkeypatch):
    for f in ALL:
        monkeypatch.setattr(config, f, True)


def _edwin(**over):
    base = dict(id=1, name="edwin", occupation="cs student at berkeley", existing_tools="strava,fitbit",
                biggest_obstacle="knowledge", food_logger=None, cooking_situation="cook", goal="fat_loss",
                water_offer_status=None, year=None, weigh_in_opt_out=False)
    base.update(over)
    return NS(**base)


def test_the_founders_structure_for_a_berkeley_student(all_on):
    text = build_rundown(_edwin())
    b1, b2 = text.split("\n---\n")
    assert b1.split("\n")[0] == RUNDOWN_INTRO
    assert "1. i track ur calories\n- tell me what u ate (food scale = even better)\n- or a pic of the plate\n" \
           "- i know all the dining hall macros, just ask about any of them" in b1
    assert "2. i track ur workouts and weights at the gym\n- say 'starting workout'\n- a card shows up" in b1
    assert b2.startswith("3. i can connect to ur calendar and bcourses\n- just tell me u want to connect it (both, or either)")
    assert "- remind u of due dates and meetings\n- help u plan around midterms and study sessions" in b2
    assert "fitbit" not in b2, "no wearable line until Google approves the API (GOOGLE_HEALTH_OFFER_ENABLED)"
    assert "4. rsf, libraries, and study rooms\n- i know how full rsf is" in b2 and "empty library rooms" in b2
    assert "5. and lastly, nice to haves\n- hydration: say 'remind me to drink water'" in b2, "hydration first"
    assert "- any reminder u want" in b2 and "share anything from apps u already use" in b2
    assert b2.rstrip().endswith("you said not knowing what to do is the hard part — ask me anything, no dumb questions")
    # show-don't-tell never appears; weather is the brief's
    low = text.lower()
    for word in ("weigh in", "weighed", "weather", "jacket", "fix it", "roll your day", "chill with the texts",
                 "15%", "syllabus", "find out and text you", "http"):
        assert word not in low, word


def test_wearable_line_appears_once_google_approves(all_on, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", True)
    assert "- and ur fitbit: sleep, steps, heart rate" in build_rundown(_edwin())


def test_non_student_without_apps_gets_the_smaller_version(all_on):
    text = build_rundown(_edwin(occupation="works at a startup", existing_tools="none", biggest_obstacle=""))
    assert "3. i can connect to ur google calendar\n- just tell me u want to connect it\n- i plan workouts around ur schedule" in text
    assert "bcourses" not in text and "midterms" not in text and "fitbit" not in text
    assert "share anything from apps" not in text
    assert not text.rstrip().endswith("—") and "you said" not in text


def test_sections_follow_the_flags_and_renumber(all_on, monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", False)
    monkeypatch.setattr(config, "BCOURSES_ENABLED", False)
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    monkeypatch.setattr(config, "STRAVA_READ_ENABLED", False)
    monkeypatch.setattr(config, "CANVAS_ENABLED", False)
    monkeypatch.setattr(config, "RSF_METER_ENABLED", False)
    monkeypatch.setattr(config, "FIND_STUDY_SPACE_TOOL_ENABLED", False)
    text = build_rundown(_edwin())
    keys = [k for k, _t, _l in rundown_sections(_edwin())]
    assert keys == ["food", "workouts", "extras"]
    assert "3. and lastly, nice to haves" in text and "4." not in text and "connect" not in text and "rsf" not in text
    monkeypatch.setattr(config, "GET_DINING_MENU_TOOL_ENABLED", False)
    assert "dining hall" not in build_rundown(_edwin())


def test_screenshot_logging_only_when_they_named_a_food_app(all_on):
    assert "screenshot ur diary" not in build_rundown(_edwin())
    assert "screenshot ur diary" in build_rundown(_edwin(existing_tools="myfitnesspal"))
    assert "screenshot ur diary" in build_rundown(_edwin(food_logger="mfp"))


def test_nothing_enabled_means_no_rundown(monkeypatch):
    for f in ALL:
        monkeypatch.setattr(config, f, False)
    for f in ("STRAVA_READ_ENABLED", "CANVAS_ENABLED"):
        monkeypatch.setattr(config, f, False)
    assert build_rundown(_edwin()) == ""


def test_every_sub_line_belongs_to_a_section_and_show_tier_has_a_reveal_hint():
    keys = {k for k, *_ in __import__("capabilities").RUNDOWN_SECTIONS}
    for c in CAPABILITIES:
        if c.section:
            assert c.section in keys and c.rundown, c.id
        else:
            assert c.reveal_when, f"{c.id}: show-don't-tell needs a reveal moment"
    by_id = {c.id: c for c in CAPABILITIES}
    assert by_id["weigh_ins"].reveal_when.startswith("never on day one")
    assert by_id["weather"].reveal_when.startswith("never as a pitch — the morning brief shows it")
    assert by_id["weather"].section is None and by_id["weigh_ins"].section is None


def test_bubbles_split_on_the_separator():
    from sms import split_bubbles
    text = "oh and a quick rundown of how i work\n1. a\n- x\n2. b\n- y\n---\n3. c\n- z"
    assert split_bubbles(text) == ["oh and a quick rundown of how i work\n1. a\n- x\n2. b\n- y", "3. c\n- z"]
