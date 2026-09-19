"""
Capability registry (capabilities.py) → the post-onboarding rundown bubble and the
coach loop's contextual reveals. The maintenance rule is enforced here: every tool
the coach loop can offer must be claimed by a registry entry (or be in
INTERNAL_TOOLS), so a new feature cannot ship without a line in the rundown.
"""

from __future__ import annotations

import re
from types import SimpleNamespace as NS

import pytest

from tests.factories import make_user

FLAGS = ("REMEMBER_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
         "LOG_MEAL_TOOL_ENABLED", "GET_DINING_MENU_TOOL_ENABLED", "WEB_SEARCH_TOOL_ENABLED",
         "MEAL_HISTORY_TOOL_ENABLED", "DINING_MATCH_TOOL_ENABLED", "USDA_LOOKUP_TOOL_ENABLED",
         "LOG_EVENT_TOOL_ENABLED", "SET_TARGETS_TOOL_ENABLED", "HEARTBEAT_ENABLED", "READ_IMAGE_ENABLED")

FOUNDER = dict(height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male",
               goal="fat_loss,muscle_building", workout_days="4-5", avg_steps=10000,
               occupation="student", activity_level="active", workout_time="14:00",
               current_split="ppl", cooking_situation="buys groceries and cooks", diet="omnivore",
               injuries="none", wake_time="11:30", sleep_time="02:00", existing_tools="strava,fitbit",
               name="Nau", biggest_obstacle="consistency")


@pytest.fixture
def all_on(monkeypatch):
    import config
    for f in FLAGS:
        monkeypatch.setattr(config, f, True)


# ─── the maintenance rule ───────────────────────────────────────────────────

def test_every_loop_tool_is_claimed_by_a_capability(all_on):
    """Parse agent_loop's tool assembly for every *_TOOL it can offer, resolve the
    tool names, and require each to be in some Capability.tools or INTERNAL_TOOLS.
    A new tool without a registry line fails here — that is the point."""
    import agent_loop, agent_tools
    from capabilities import CAPABILITIES, INTERNAL_TOOLS
    src = open(agent_loop.__file__).read()
    consts = set(re.findall(r"from agent_tools import ([A-Z_, ]+)", src))
    names = set()
    for group in consts:
        for c in group.split(","):
            c = c.strip()
            if c.endswith("_TOOL") and hasattr(agent_tools, c):
                names.add(getattr(agent_tools, c)["name"])
    assert names, "no tools found in agent_loop — the parser broke"
    claimed = {t for cap in CAPABILITIES for t in cap.tools} | INTERNAL_TOOLS
    missing = names - claimed
    assert not missing, f"coach-loop tools with no capability entry (add one to capabilities.py): {sorted(missing)}"


def test_registry_entries_are_well_formed(all_on):
    from capabilities import CAPABILITIES
    ids = [c.id for c in CAPABILITIES]
    assert len(ids) == len(set(ids))
    u = NS(id=1, **FOUNDER)
    for c in CAPABILITIES:
        assert c.what and c.how and not c.what.endswith(".")
        assert isinstance(c.enabled(u), bool) and 0 <= int(c.relevance(u)) <= 10


# ─── rundown content ────────────────────────────────────────────────────────

def test_rundown_leads_with_the_founders_top_three_and_closes_on_the_obstacle(all_on):
    from capabilities import rundown_context, available
    u = NS(id=1, **FOUNDER)
    lead = [c.id for c in available(u)[:3]]
    assert lead[:2] == ["log_meals", "log_workouts"]
    assert "dining_halls" not in lead          # he cooks; dining halls rank low
    ctx = rundown_context(u)
    assert ctx.startswith("LEAD WITH")
    assert "CLOSE ON: you said staying consistent is the hard part" in ctx


def test_rundown_never_names_a_disabled_capability(all_on, monkeypatch):
    import config
    from capabilities import rundown_context
    monkeypatch.setattr(config, "GET_DINING_MENU_TOOL_ENABLED", False)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", False)
    u = NS(id=1, **dict(FOUNDER, cooking_situation="dining hall meal plan"))
    ctx = rundown_context(u)
    assert "dining hall" not in ctx and "log what you eat" not in ctx and "photo" not in ctx


def test_dining_hall_user_gets_dining_in_the_lead(all_on):
    from capabilities import available
    u = NS(id=1, **dict(FOUNDER, cooking_situation="dining hall meal plan", goal="general_fitness"))
    assert "dining_halls" in [c.id for c in available(u)[:3]]


# ─── the bubble at completion ───────────────────────────────────────────────

def _is_field_extract(kw):
    return "Extract any fitness coaching profile data" in str(kw["messages"][0]["content"])


def test_completion_sends_the_rundown_as_a_second_bubble(db, all_on, anthropic_stub, sms_capture, monkeypatch):
    import config, onboarding_agent
    from models import get_session, Message
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    user = make_user(db, onboarding_step=2, **FOUNDER)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="here's what i'm working with ... sound right?",
                      message_type="onboarding"))
        s.commit()
    finally:
        s.close()
    seen = []

    def _handler(kw):
        if _is_field_extract(kw):
            return "{}"
        ins = kw["messages"][0]["content"]
        seen.append(ins)
        if "quick rundown" in ins:
            return "oh and quick rundown of how i work — text me what you eat and i log it..."
        return "locked in. profile: https://x"
    anthropic_stub.reply_with(_handler)

    assert onboarding_agent.handle_onboarding_reply(user, "sounds good") is True
    bodies = [b for _, b in sms_capture]
    assert len(bodies) == 2, bodies
    assert bodies[0].startswith("locked in") and bodies[1].startswith("oh and quick rundown")
    rundown_ins = next(i for i in seen if "quick rundown" in i)
    assert "LEAD WITH" in rundown_ins and "No bullet points" in rundown_ins
    assert "No feature previews" in seen[0]   # the kickoff itself stays feature-free


def test_rundown_flag_off_sends_only_the_kickoff(db, all_on, anthropic_stub, sms_capture, monkeypatch):
    import config, onboarding_agent
    from models import get_session, Message
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    user = make_user(db, onboarding_step=2, **FOUNDER)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="... sound right?", message_type="onboarding"))
        s.commit()
    finally:
        s.close()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_field_extract(kw) else "locked in")
    assert onboarding_agent.handle_onboarding_reply(user, "sounds good") is True
    assert len(sms_capture) == 1


# ─── contextual reveals in the coach loop ───────────────────────────────────

def test_unused_is_derived_from_evidence(db, all_on):
    from capabilities import unused
    from models import get_session, Meal, Message
    from sms import IMAGE_MARKER
    from datetime import datetime, timezone
    user = make_user(db, onboarding_step=3, **FOUNDER)
    s = get_session()
    try:
        ids = [c.id for c in unused(user, s)]
        assert ids[:2] == ["log_meals", "log_workouts"]      # nothing used yet, most relevant first
        assert len(ids) <= 3
        s.add(Meal(user_id=user.id, description="eggs", calories=300,
                   eaten_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        s.add(Message(user_id=user.id, direction="in", body=f"yo {IMAGE_MARKER}", message_type="freeform",
                      channel="imessage", provider_sid="spc-1", delivery_status="delivered",
                      created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        s.commit()
        ids = [c.id for c in unused(user, s)]
        assert "log_meals" not in ids and "food_photos" not in ids
        assert "log_workouts" in ids
    finally:
        s.close()


def test_loop_context_carries_the_block_only_after_onboarding(db, all_on):
    from agent_loop import build_loop_context
    from models import get_session, User
    done = make_user(db, onboarding_step=3, **FOUNDER)
    mid = make_user(db, onboarding_step=2, **FOUNDER)
    s = get_session()
    try:
        ctx_done = build_loop_context(s.get(User, done.id), s)
        ctx_mid = build_loop_context(s.get(User, mid.id), s)
    finally:
        s.close()
    assert "## THINGS THEY HAVEN'T USED YET" in ctx_done
    assert "bring it up when: they mention eating something without logging it" in ctx_done
    assert "THINGS THEY HAVEN'T USED YET" not in ctx_mid


def test_voice_has_the_one_at_a_time_reveal_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "Reveal what you can do one moment at a time, never as a list" in v
    assert "A feature you weren't given there doesn't exist for this user" in v


def test_web_search_tool_blocks_known_spam_hosts():
    """Source-quality hardening (2026-09-18 wrong-RSF-hours incident): the web_search
    tool hard-blocks the SEO-spam proxy hosts that fed a wrong closing time."""
    from agent_tools import WEB_SEARCH_TOOL
    blocked = WEB_SEARCH_TOOL.get("blocked_domains") or []
    assert "phplive-aws.uccs.edu" in blocked and "sbc-hc-proxy.stanford.edu" in blocked
