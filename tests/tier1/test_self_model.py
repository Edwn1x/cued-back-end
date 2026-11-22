"""
Self-knowledge (self_model.py) + the update_profile tool: the coach knows what it can do for
this user on this channel, what its outputs look like, who else writes in the thread, where a
stated fact must live — and has the pen for the profile columns.
"""

from __future__ import annotations

import pytest

from tests.factories import make_user

SARAH = dict(name="Sarah", age=19, gender="female", height_ft=5, height_in=3, weight_lbs=115.0,
             experience="none", equipment="full_gym", goal="general_fitness", workout_days="2",
             workout_time="14:00", wake_time="08:00", sleep_time="03:00", activity_level="active — walking all over campus",
             reported_intake=2500, preferred_channel="imessage", onboarding_step=3)


@pytest.fixture
def on(monkeypatch):
    import config
    for f in ("SELF_MODEL_ENABLED", "UPDATE_PROFILE_TOOL_ENABLED", "WATER_REMINDERS_ENABLED", "REMINDERS_ENABLED",
              "LOG_MEAL_TOOL_ENABLED", "CARD_SETUP_ENABLED", "START_WORKOUT_TOOL_ENABLED", "IMESSAGE_CHANNEL_ENABLED"):
        monkeypatch.setattr(config, f, True, raising=False)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


# ─── the block ──────────────────────────────────────────────────────────────────────

def test_block_names_every_enabled_capability_and_what_is_off(db, on, monkeypatch):
    import sms
    from self_model import context_block
    from capabilities import available, CAPABILITIES
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    u = make_user(db, **SARAH)
    blk = context_block(_u(u.id), db)
    assert blk and blk.startswith("## WHAT YOU ARE FOR SARAH RIGHT NOW")
    for c in available(u):
        assert f"- {c.what}" in blk
    water = next(c for c in CAPABILITIES if c.id == "water_reminders")
    assert water.what in blk                          # the capability denied twice live (user 56)
    assert "never say you can't do one of these" in blk
    assert "NOT on for them" in blk or len(available(u)) == len(CAPABILITIES)


def test_imessage_outputs_say_sticker_and_card_sms_says_link_and_mms(db, on, monkeypatch):
    import sms
    from self_model import context_block
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    u = make_user(db, **SARAH)
    blk = context_block(_u(u.id), db)
    assert "ANIMATED water sticker" in blk and "[water-break gif]" in blk and "[workout card:" in blk
    assert "Never say you can't do animations" in blk
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "sms")
    u2 = make_user(db, **dict(SARAH, preferred_channel="sms", phone="+15550009001"))
    blk2 = context_block(_u(u2.id), db)
    assert "they're on SMS" in blk2 and "comes as a link" in blk2 and "no stickers, polls or tapbacks" in blk2
    assert "ANIMATED water sticker" not in blk2


def test_block_explains_team_lines_and_where_facts_live(db, on, monkeypatch):
    import sms
    from self_model import context_block, TEAM_LABEL
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    u = make_user(db, **SARAH)
    blk = context_block(_u(u.id), db)
    assert TEAM_LABEL in blk and "never apologize for them" in blk
    assert "The team reads this thread" in blk and "tell the founders" in blk
    assert "update_profile" in blk and "`remember` alone is a note" in blk
    # the old setup agent has no tools → the compact form says code stores it, not the tool
    blk2 = context_block(_u(u.id), db, tools_available=False)
    assert "update_profile" not in blk2 and "code stores it" in blk2


def test_block_counts_what_code_sent_today(db, on, monkeypatch):
    import sms
    from datetime import datetime, timezone
    from models import Message
    from self_model import context_block
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    u = make_user(db, **SARAH)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for mt in ("water_reminder", "water_reminder", "water_reminder", "reminder", "freeform"):
        db.add(Message(user_id=u.id, direction="out", body="x", message_type=mt, channel="imessage", created_at=now))
    db.commit()
    blk = context_block(_u(u.id), db)
    assert "SENT TO THEM TODAY BY CODE" in blk
    assert "3× water sticker" in blk and "1× reminder text" in blk
    assert "freeform" not in blk


def test_flag_off_renders_nothing(db, on, monkeypatch):
    import config
    from self_model import context_block, onboarding_block, history_label
    from types import SimpleNamespace as NS
    monkeypatch.setattr(config, "SELF_MODEL_ENABLED", False)
    u = make_user(db, **SARAH)
    assert context_block(_u(u.id), db) is None and onboarding_block(_u(u.id)) is None
    assert history_label(NS(message_type="admin")) == "Coach"


# ─── where it lands ───────────────────────────────────────────────────────────────

def test_loop_context_labels_team_typed_lines_and_carries_the_block(db, on, monkeypatch):
    import sms
    from datetime import datetime, timezone
    from models import Message
    from agent_loop import build_loop_context
    from self_model import TEAM_LABEL
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    u = make_user(db, **SARAH)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(Message(user_id=u.id, direction="in", body="wheres the animation", message_type="freeform", channel="imessage", created_at=now))
    db.add(Message(user_id=u.id, direction="out", body="the real pings come with the animation, like this 👇",
                   message_type="admin", channel="imessage", created_at=now))
    db.add(Message(user_id=u.id, direction="out", body="how's legs going", message_type="freeform", channel="imessage", created_at=now))
    db.commit()
    ctx = build_loop_context(_u(u.id), db)
    assert f"{TEAM_LABEL}: the real pings come with the animation" in ctx
    assert "Coach: how's legs going" in ctx
    assert "## WHAT YOU ARE FOR SARAH RIGHT NOW" in ctx


def test_setup_prompt_carries_the_compact_block(db, on, monkeypatch):
    import sms
    import onboarding_agent as oa
    from capabilities import CAPABILITIES
    monkeypatch.setattr(sms, "_resolve_channel", lambda uid: "imessage")
    u = make_user(db, **dict(SARAH, onboarding_step=2))
    prompt = oa._build_system_prompt(_u(u.id))
    assert "## WHAT CUED DOES" in prompt and "NEVER say cued can't" in prompt
    water = next(c for c in CAPABILITIES if c.id == "water_reminders")
    assert water.what in prompt


def test_update_profile_is_on_the_loops_tool_list_and_claimed(on):
    from capabilities import CAPABILITIES
    assert any("update_profile" in c.tools for c in CAPABILITIES)
    from agent_tools import _HANDLERS, UPDATE_PROFILE_TOOL
    assert _HANDLERS["update_profile"] and UPDATE_PROFILE_TOOL["name"] == "update_profile"


# ─── the pen ───────────────────────────────────────────────────────────────────────

def test_update_profile_writes_steps_and_moves_the_target(db, on):
    """Sarah, 2026-11-20: 'like about 10.5k' went to memory; the target stayed 1750 while the
    calculator says 1950 with the steps. The tool writes the column and recomputes."""
    from agent_tools import handle_update_profile
    from macro_calculator import recompute_targets
    u = make_user(db, **SARAH)
    recompute_targets(u.id)
    before = _u(u.id)
    assert before.avg_steps is None and before.calorie_target
    r = handle_update_profile(u.id, {"avg_steps": 10500})
    after = _u(u.id)
    assert r.startswith("ok: avg_steps=10500")
    assert after.avg_steps == 10500
    assert after.calorie_target > before.calorie_target, (before.calorie_target, after.calorie_target)
    assert f"targets {before.calorie_target} → {after.calorie_target} cal" in r
    # idempotent
    assert handle_update_profile(u.id, {"avg_steps": 10500}) == "ok: already on file — nothing changed"


def test_update_profile_validates_and_normalizes(db, on):
    from agent_tools import handle_update_profile
    u = make_user(db, **SARAH)
    r = handle_update_profile(u.id, {"wake_time": "9:00", "sleep_time": "2:30", "workout_days": "mon, wed, fri",
                                     "equipment": "Home Gym", "injuries": "none", "occupation": "cog sci + design"})
    row = _u(u.id)
    assert row.wake_time == "09:00" and row.sleep_time == "02:30" and row.workout_days == "mon,wed,fri"
    assert row.equipment == "home_gym" and row.occupation == "cog sci + design"
    assert "wake_time='09:00' (was '08:00')" in r and "targets set: " in r
    bad = handle_update_profile(u.id, {"wake_time": "8am", "experience": "pro", "equipment": "garage"})
    assert bad.startswith("error: nothing to write") and "HH:MM" in bad and "experience must be" in bad
    assert handle_update_profile(u.id, {}) .startswith("error: nothing to write")
    mixed = handle_update_profile(u.id, {"avg_steps": 7000, "user_timezone": "Mars/Olympus"})
    assert mixed.startswith("ok: avg_steps=7000") and "ignored: user_timezone" in mixed
    assert _u(u.id).user_timezone == "America/Los_Angeles"


def test_update_profile_leaves_user_picked_targets_alone(db, on):
    from agent_tools import handle_update_profile
    u = make_user(db, **dict(SARAH, targets_source="user", calorie_target=1600, protein_target=90))
    r = handle_update_profile(u.id, {"avg_steps": 12000})
    row = _u(u.id)
    assert row.avg_steps == 12000 and row.calorie_target == 1600 and row.protein_target == 90
    assert "targets unchanged (1600 cal / 90g)" in r
