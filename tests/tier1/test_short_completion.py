"""The completion sequence, shortened (founder, 2026-10-10).

His from-zero run (user 49, 02:34am): seven bubbles / 2064 chars in 53 seconds — two sleep
pegs (the burst replay, #217), the summary, the numbers, the link, the tiered rundown in
two bubbles (1255 chars, 61% of it), then "go sleep". "wayyyy too long and tedious."

Now: ONE summary bubble (facts + numbers + short closer + link) and ONE rundown bubble
(the three things that matter + the obstacle line). Everything the tiered rundown listed
under those is revealed in the moment — every such capability has a reveal_when.
"""
from __future__ import annotations

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _coach_said
from tests.tier1.test_rundown_tiers import ALL as _ALL_FLAGS


@pytest.fixture
def all_on(monkeypatch):
    for f in _ALL_FLAGS:
        monkeypatch.setattr(config, f, True)


def _done(db, **over):
    kw = dict(name="Nau", onboarding_step=2, goal="fat_loss", experience="beginner", occupation="student",
              height_ft=5, height_in=6, weight_lbs=141, age=20, gender="male", activity_level="active",
              avg_steps=10000, workout_days="4", workout_time="18:00", current_split="ppl", wake_time="11:00",
              sleep_time="02:00", sleep_estimated=True, cooking_situation="cook", diet="omnivore",
              injuries="none", existing_tools="none", biggest_obstacle="knowledge")
    kw.update(over)
    return make_user(db, **kw)


# ── the summary: one bubble ────────────────────────────────────────────────────

def test_summary_is_one_bubble_with_the_numbers_and_the_link(db):
    import onboarding_agent as oa
    from profile_page import profile_url
    u = _done(db)
    s = oa._build_confirmation_summary(u)
    assert "\n---\n" not in s, "one bubble"
    head, link = s.rsplit("\n", 1)
    assert link == profile_url(u)
    assert head.startswith("ok so far this is what i have: ur 5'6 141, training 4 days a week in the evenings, "
                           "up around 11 down around 2 (my guess, fix it anytime), tryna lose fat")
    assert ". so " in head and " cal and " in head and "g protein a day to start, we adjust in a few weeks. " in head
    assert head.endswith(oa.SUMMARY_CLOSER_SHORT)
    assert "im thinking" not in head and oa.SUMMARY_CLOSER not in head
    assert len(head) < 340, len(head)


def test_summary_keeps_the_user_pick_and_clamp_wording(db):
    import onboarding_agent as oa
    u = _done(db, calorie_target=2000, protein_target=155, targets_source="user")
    s = oa._build_confirmation_summary(u)
    assert "2000 cal and 155g protein a day to start, ur pick (i'd have said " in s
    s2 = oa._build_confirmation_summary(_done(db, phone="+15550100099", calorie_target=1800, protein_target=120),
                                        clamp_note="1500 is too low for u")
    assert "1500 is too low for u. so 1800 cal and 120g protein a day to start, we adjust in a few weeks" in s2


def test_summary_flag_off_restores_the_two_bubbles(db, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_SUMMARY_ONE_BUBBLE", False)
    s = oa._build_confirmation_summary(_done(db))
    first, second = s.split("\n---\n")
    assert first.startswith("ok so far") and second.startswith("im thinking ") and oa.SUMMARY_CLOSER in second


# ── the rundown: one bubble ────────────────────────────────────────────────────

def test_short_rundown_is_the_three_things_and_the_obstacle_line(db, all_on, monkeypatch):
    from capabilities import build_short_rundown, OBSTACLE_LINES
    monkeypatch.setattr(config, "BCOURSES_ENABLED", True)
    u = _done(db)
    r = build_short_rundown(u)
    assert r == ("quick version: text me what u eat or send a pic, say 'starting workout' for a card, and i can hook "
                 "into ur calendar + bcourses. " + OBSTACLE_LINES["knowledge"]), r
    assert len(r) < 260


def test_short_rundown_follows_the_gates(db, all_on, monkeypatch):
    from capabilities import build_short_rundown, SHORT_RUNDOWN_CLOSER
    monkeypatch.setattr(config, "BCOURSES_ENABLED", False)
    u = _done(db, biggest_obstacle=None, occupation="working")
    r = build_short_rundown(u)
    assert "i can hook into ur calendar." in r and "bcourses" not in r and r.endswith(SHORT_RUNDOWN_CLOSER)
    monkeypatch.setattr(config, "LOG_WORKOUT_TOOL_ENABLED", False)
    r = build_short_rundown(u)
    assert "starting workout" not in r and r.startswith("quick version: text me what u eat or send a pic, and i can hook")
    for f in ("LOG_MEAL_TOOL_ENABLED", "GCAL_ENABLED", "STRAVA_READ_ENABLED", "CANVAS_ENABLED", "GOOGLE_HEALTH_ENABLED"):
        monkeypatch.setattr(config, f, False)
    assert build_short_rundown(u) == "", "nothing enabled → no rundown"


def test_every_tiered_sub_line_has_a_reveal_hint_so_nothing_is_lost(db):
    """Dropping the sub-lines loses nothing: each capability that had a rundown line
    carries a reveal_when, so the coach brings it up in the moment."""
    from capabilities import CAPABILITIES
    missing = [c.id for c in CAPABILITIES if c.rundown and not c.reveal_when]
    assert missing == [], missing


# ── completion end to end: two bubbles + the link, then the setup steps ──────

def test_completion_is_summary_then_short_rundown(db, all_on, anthropic_stub, sms_capture, monkeypatch, caplog):
    import logging
    import onboarding_agent as oa
    from profile_page import profile_url
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    monkeypatch.setattr(config, "BCOURSES_ENABLED", True)
    caplog.set_level(logging.INFO)
    u = _done(db, wake_time=None, sleep_time=None, sleep_estimated=False)
    _coach_said(u, "last thing — roughly when ur up and when u crash")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ima guess up around 11 and down by 2, fix it anytime")

    assert oa.handle_onboarding_reply(u, "idk my sleep schedule is cooked") is True
    b = _bodies(sms_capture)
    assert b[0] == "ima guess up around 11 and down by 2, fix it anytime"
    assert b[1].startswith("ok so far this is what i have: ur 5'6 141") and b[1].endswith(profile_url(u)) and ". so " in b[1]
    assert b[2].startswith("quick version: text me what u eat or send a pic, say 'starting workout' for a card, and i can hook into ur calendar + bcourses. ")
    assert len(b) == 3, b
    assert sum(len(x) for x in b[1:]) < 650, "the summary + rundown together, well under the old 1741"
    assert "ONBOARDING_RUNDOWN_SENT" in caplog.text and "bubbles=1" in caplog.text


def test_rundown_style_tiered_restores_the_numbered_bubbles(db, all_on, monkeypatch, sms_capture):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_STYLE", "tiered")
    u = _done(db)
    assert oa._send_capability_rundown(u) is True
    b = _bodies(sms_capture)
    assert b[0].startswith("oh and a quick rundown of how i work\n1. i track ur calories")
