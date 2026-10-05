"""
Coaching lessons — the coach's SELF-authored notes about its own past misses with
THIS user (lessons.py + a `coaching_lessons` memory category).

What's there now: memory stores facts ABOUT the user; delivered_coaching_points stores
what the coach EXPLAINED; nudge_guard stores what it nudged TODAY. Nothing stores
"I got this wrong with them before — don't repeat it". Every such lesson in the
incident log (pork logged as chicken, the protein nudge restated 5×, the week rundown
that dropped Friday) was learned by US shipping a PR, never by the system.

What it becomes: a correction by the user → a lesson, phrased as an instruction to the
coach, stored in `coaching_lessons` (own soft cap, never safety, never stale-closed),
rendered as its own authoritative block in every reactive AND heartbeat context.
Two writers converge on the same apply_facts ladder: the reactive model via
remember(category="coaching_lessons") and a background Haiku extractor that runs ONLY
when a code-side correction-cue gate fires (budget gate, not a classifier).

Pinned here (tier 1, model mocked):
  * category + caps + stale exemption (code-owned hygiene)
  * the cue gate (no model call on a non-correction turn; none with the flag off)
  * the extractor's envelope: JSON parse, confidence gate, generalizable gate,
    sanitizer (length, ≥4 words, pronoun neutralization), no raise
  * rendering: own block with ids, NOT duplicated inside WHAT YOU REMEMBER, absent
    when empty / flag off; reaches the heartbeat context
  * remember tool accepts the category only when the flag is on
  * prompts carry the correction → lesson rule
"""
from __future__ import annotations

import json

import pytest

import config
from tests.factories import make_user


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "LESSONS_ENABLED", True)
    monkeypatch.setattr(config, "LESSONS_EXTRACT_ENABLED", True)


def _profile(db, user_id):
    from models import User
    db.expire_all()
    return dict(db.get(User, user_id).user_profile_memory or {})


def _lesson_texts(profile):
    return [e["text"] for e in (profile.get("coaching_lessons") or [])]


# ─── category + hygiene ─────────────────────────────────────────────────────

def test_category_exists_and_is_not_a_user_fact_category():
    from memory import CATEGORIES
    from lessons import LESSONS_CATEGORY
    assert LESSONS_CATEGORY == "coaching_lessons" and LESSONS_CATEGORY in CATEGORIES


def test_lessons_have_their_own_soft_cap(monkeypatch):
    """Lessons must not be squeezed by the generic 400-char category cap, nor crowd it."""
    from memory import apply_facts
    monkeypatch.setattr(config, "USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP", 100)
    monkeypatch.setattr(config, "LESSONS_SOFT_CAP", 250)
    lessons = [
        "Verify the meat type in a food photo before logging it; ask when it's ambiguous.",
        "Don't restate the same nudge more than once a day; vary the angle or let it rest.",
        "When they ask for the rest of the week, list every day through Friday, deadlines first.",
        "Check the log before asking what they ate; never ask about something already logged.",
        "Late at night, lead with sleep, not with closing the protein gap.",
    ]
    facts = [{"action": "add", "category": "coaching_lessons", "text": t} for t in lessons]
    prof, stats = apply_facts({}, facts, user_id=1)
    kept = _lesson_texts(prof)
    assert 2 <= len(kept) < 5 and sum(len(t) for t in kept) <= 250      # cap bit, but above 100
    # generic categories still obey the small cap
    prof2, _ = apply_facts({}, [{"action": "add", "category": "goals", "text": "x" * 60 + " goal one"},
                               {"action": "add", "category": "goals", "text": "y" * 60 + " goal two"}], user_id=1)
    assert sum(len(t) for t in (e["text"] for e in prof2["goals"])) <= 100


def test_consolidation_never_closes_a_lesson_as_stale():
    from datetime import datetime, timezone, timedelta
    from memory import _new_entry
    from consolidation import _close_stale
    old = _new_entry("Don't restate the protein nudge more than once a day; they call it nagging")
    old["ts"] = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    old["uses"] = 0
    fact = _new_entry("prefers morning workouts before class")
    fact["ts"] = old["ts"]
    fact["uses"] = 0
    cand = {"coaching_lessons": [old], "training_preferences": [fact]}
    ops = []
    _close_stale(cand, ops, datetime.now(timezone.utc))
    assert cand["coaching_lessons"] and cand["coaching_lessons"][0]["text"].startswith("Don't restate")
    assert not cand["training_preferences"], "control: a never-used ordinary fact IS closed"


# ─── the cue gate (code-owned budget gate) ──────────────────────────────────

@pytest.mark.parametrize("msg", [
    "no that was pork not chicken",
    "that's wrong, i said 2 eggs",
    "you already told me that like five times",
    "stop asking me that",
    "i didn't eat that today, that was yesterday",
    "wrong — the midterm is friday not thursday",
    "bro you keep saying the same thing",
    "it was oat milk not whole milk, fix it",
    "not what i meant",
])
def test_cue_gate_fires_on_corrections(msg):
    from lessons import looks_like_correction
    assert looks_like_correction(msg)


@pytest.mark.parametrize("msg", [
    "sounds good",
    "just had 2 eggs and toast",
    "heading to the gym",
    "what time does the rsf close",
    "not sure what to eat tonight",
    "took a wrong turn on the way to class lol",
    "ok",
    "",
    None,
])
def test_cue_gate_stays_quiet_on_ordinary_turns(msg):
    from lessons import looks_like_correction
    assert not looks_like_correction(msg)


# ─── the extractor envelope ─────────────────────────────────────────────────

def _lesson_json(lesson, *, confidence="high", generalizable=True):
    return json.dumps({"lesson": lesson, "confidence": confidence, "generalizable": generalizable})


def test_extractor_stores_a_high_confidence_generalizable_lesson(db, anthropic_stub):
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    anthropic_stub.push(_lesson_json(
        "Verify the meat type in a food photo before logging it; ask when it's ambiguous."))
    extract_and_store_lesson_task(user.id, "no that was pork not chicken, you keep doing this",
                                  "my bad — fixed it to pork chops")
    assert anthropic_stub.calls, "the extractor should have called the model on a correction"
    texts = _lesson_texts(_profile(db, user.id))
    assert texts == ["Verify the meat type in a food photo before logging it; ask when it's ambiguous."]


def test_extractor_skips_without_a_cue_and_makes_no_model_call(db, anthropic_stub):
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    extract_and_store_lesson_task(user.id, "just had 2 eggs and toast", "nice, logged it")
    assert not anthropic_stub.calls
    assert _lesson_texts(_profile(db, user.id)) == []


def test_extractor_is_off_with_the_flag_off(db, anthropic_stub, monkeypatch):
    from lessons import extract_and_store_lesson_task
    monkeypatch.setattr(config, "LESSONS_EXTRACT_ENABLED", False)
    user = make_user(db, name="Sam")
    extract_and_store_lesson_task(user.id, "no that was pork not chicken", "fixed")
    assert not anthropic_stub.calls


@pytest.mark.parametrize("payload", [
    _lesson_json("Verify the meat type in a food photo before logging it.", confidence="low"),
    _lesson_json("Verify the meat type in a food photo before logging it.", generalizable=False),
    _lesson_json(None),
    _lesson_json(""),
    _lesson_json("Be careful."),                      # fragment
    "not json at all",
    json.dumps({"lesson": "x" * 2000, "confidence": "high", "generalizable": True}),   # absurd length → rejected
])
def test_extractor_gates_low_confidence_one_offs_and_junk(db, anthropic_stub, payload):
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    anthropic_stub.push(payload)
    extract_and_store_lesson_task(user.id, "no that was pork not chicken", "fixed")
    assert _lesson_texts(_profile(db, user.id)) == []


def test_extractor_neutralizes_pronouns_and_dedups(db, anthropic_stub):
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    anthropic_stub.push(_lesson_json("Don't repeat the protein nudge; he finds it nagging."))
    extract_and_store_lesson_task(user.id, "you already said that five times", "fair, dropping it")
    anthropic_stub.push(_lesson_json("Don't repeat the protein nudge; he finds it nagging."))
    extract_and_store_lesson_task(user.id, "you already said that five times", "fair, dropping it")
    texts = _lesson_texts(_profile(db, user.id))
    assert len(texts) == 1
    assert " he " not in f" {texts[0]} " and "they" in texts[0]


def test_extractor_sees_the_prior_coach_message(db, anthropic_stub):
    """The lesson is about what the COACH did before the correction — the extractor
    must be shown the coach's prior message, not just the reply to the correction."""
    from datetime import datetime, timezone, timedelta
    from models import get_session, Message
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    s = get_session()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(Message(user_id=user.id, direction="out", body="logged 6oz chicken breast, ~280 cal",
                      message_type="freeform", created_at=now - timedelta(minutes=3)))
        s.add(Message(user_id=user.id, direction="in", body="no that was pork not chicken",
                      message_type="freeform", created_at=now - timedelta(minutes=1)))
        s.add(Message(user_id=user.id, direction="out", body="my bad — fixed it to pork chops",
                      message_type="freeform", created_at=now))
        s.commit()
    finally:
        s.close()
    anthropic_stub.push(_lesson_json("Verify the meat type in a photo before logging; ask if unsure."))
    extract_and_store_lesson_task(user.id, "no that was pork not chicken", "my bad — fixed it to pork chops")
    sent = json.dumps(anthropic_stub.calls[-1].get("messages"))
    assert "logged 6oz chicken breast" in sent
    assert "fixed it to pork chops" in sent


def test_extractor_yields_when_the_reactive_writer_already_saved_one(db, anthropic_stub):
    """Two writers, one correction: the remember tool wrote a lesson seconds ago → the
    background extractor must not add a paraphrase (live 2026-10-05: it did)."""
    from agent_tools import dispatch_tool
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    assert dispatch_tool("remember", {"action": "add", "category": "coaching_lessons",
                                      "text": "Verify the meat type in a food photo before logging it."}, user.id).startswith("ok:")
    anthropic_stub.push(_lesson_json("When logging from photos, confirm the specific meat rather than defaulting to chicken."))
    extract_and_store_lesson_task(user.id, "no that was pork not chicken", "fixed")
    assert not anthropic_stub.calls, "extractor should yield to a fresh lesson without a model call"
    assert len(_lesson_texts(_profile(db, user.id))) == 1


def test_extractor_never_raises(db, anthropic_stub):
    from lessons import extract_and_store_lesson_task
    user = make_user(db, name="Sam")
    anthropic_stub.reply_with(lambda kw: (_ for _ in ()).throw(RuntimeError("api down")))
    extract_and_store_lesson_task(user.id, "no that was pork not chicken", "fixed")  # no exception
    assert _lesson_texts(_profile(db, user.id)) == []


# ─── rendering ──────────────────────────────────────────────────────────────

def _seed_lessons(db, user, *texts):
    from memory import _new_entry
    from models import User
    from sqlalchemy.orm.attributes import flag_modified
    u = db.get(User, user.id)
    prof = dict(u.user_profile_memory or {})
    prof["coaching_lessons"] = [_new_entry(t) for t in texts]
    u.user_profile_memory = prof
    flag_modified(u, "user_profile_memory")
    db.commit()
    db.expire_all()
    return db.get(User, user.id)


def test_lessons_render_as_their_own_block_with_ids_not_inside_memory(db):
    from agent_loop import build_loop_context
    user = make_user(db, name="Sam")
    user = _seed_lessons(db, user, "Verify the meat type in a photo before logging it.")
    ctx = build_loop_context(user, db)
    assert "## LESSONS FROM COACHING THEM" in ctx
    block = ctx.split("## LESSONS FROM COACHING THEM", 1)[1].split("\n## ", 1)[0]
    assert "Verify the meat type" in block and "[id:" in block
    # not duplicated inside the user-facts block
    if "## WHAT YOU REMEMBER" in ctx:
        mem = ctx.split("## WHAT YOU REMEMBER", 1)[1].split("\n## ", 1)[0]
        assert "Verify the meat type" not in mem
    assert "authoritative" in block.lower() or "follow" in block.lower()


def test_no_block_when_empty_or_flag_off(db, monkeypatch):
    from agent_loop import build_loop_context
    user = make_user(db, name="Sam")
    assert "LESSONS FROM COACHING THEM" not in build_loop_context(user, db)
    user = _seed_lessons(db, user, "Verify the meat type in a photo before logging it.")
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    assert "LESSONS FROM COACHING THEM" not in build_loop_context(user, db)


def test_lessons_reach_the_heartbeat_context(db, monkeypatch):
    from heartbeat import _proactive_context
    user = make_user(db, name="Sam")
    user = _seed_lessons(db, user, "Don't restate the protein nudge more than once a day.")
    ctx = _proactive_context(user, db)
    assert "LESSONS FROM COACHING THEM" in ctx and "protein nudge" in ctx


def test_rendered_lessons_get_their_uses_bumped(db):
    """Eviction order is lowest-uses-first and consolidation closes never-used facts;
    a lesson that is rendered every turn must count as used."""
    from lessons import lessons_block
    user = make_user(db, name="Sam")
    user = _seed_lessons(db, user, "Verify the meat type in a photo before logging it.")
    text, ids = lessons_block(user)
    assert text and len(ids) == 1
    assert ids[0] == user.user_profile_memory["coaching_lessons"][0]["id"]


# ─── the remember tool as the second writer ─────────────────────────────────

def test_remember_tool_writes_a_lesson_when_on_and_refuses_when_off(db, monkeypatch):
    from agent_tools import dispatch_tool
    user = make_user(db, name="Sam")
    out = dispatch_tool("remember", {"action": "add", "category": "coaching_lessons",
                                     "text": "When they ask for 'the rest of the week', list every day through Friday."}, user.id)
    assert out.startswith("ok:")
    assert _lesson_texts(_profile(db, user.id)) == [
        "When they ask for 'the rest of the week', list every day through Friday."]
    monkeypatch.setattr(config, "LESSONS_ENABLED", False)
    out = dispatch_tool("remember", {"action": "add", "category": "coaching_lessons",
                                     "text": "Another lesson that should not be stored right now."}, user.id)
    assert out.startswith("error:")


def test_remember_tool_description_routes_corrections_to_lessons():
    from agent_tools import REMEMBER_TOOL
    assert "coaching_lessons" in REMEMBER_TOOL["description"]
    assert "coaching_lessons" in REMEMBER_TOOL["input_schema"]["properties"]["category"]["enum"]


def test_prompts_carry_the_correction_rule():
    voice = open("prompts/voice.md").read()
    assert "coaching_lessons" in voice
    assert "LESSONS FROM COACHING THEM" in voice
