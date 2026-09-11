"""
Berkeley-friend identity + conversational intake + web search on every surface
(founder, 2026-09-11).

1. ONE identity (prompts/identity.md) is loaded first on every surface: the coach
   loop / heartbeat prefix (_voice_prompt) and the onboarding system prompt.
2. Onboarding has NO intake block: the eight-question big ask and the two-field
   gap bundling are gone. The system prompt carries the STILL UNKNOWN list; the
   reply instruction asks for at most ONE woven question. The first reply to the
   hook advances to step 2 without any list being sent.
3. web_search is registered once in agent_tools (cap = WEB_SEARCH_MAX_USES = 2)
   and offered by the coach loop, the heartbeat, and onboarding's _generate; every
   query is logged with the user id (WEB_SEARCH_QUERY).
4. The extractor is handed the coach's previous message (there is no "last asked
   field" anymore) so a bare "5" maps to what was actually asked.
5. The summary is presented the first time nothing is unknown; a stray "ok" in
   the message that filled the last field can no longer complete onboarding.
6. The big ask and the two-field bundle are KEPT for when a list is appropriate
   (founder): they ask for it, the conversation has run long with most fields
   unknown, or one/two fields are left to close out. Code-decided (_intake_mode).
"""

from __future__ import annotations

import logging

import pytest

from tests._fake_anthropic import WebSearchUse
from tests.factories import make_user

INTAKE = dict(height_ft=None, weight_lbs=None, occupation=None, activity_level=None,
              avg_steps=None, workout_days=None, workout_time=None, current_split=None,
              cooking_situation=None, diet=None, injuries=None, wake_time=None,
              sleep_time=None, existing_tools=None)


def _is_extract(kwargs) -> bool:
    """The field extractor call (any model) vs the reply call."""
    return "Extract any fitness coaching profile data" in str(kwargs["messages"][0]["content"])


def _new_signup(db, **over):
    """A user right after the hook: signup fields only, onboarding_step=1."""
    kw = dict(INTAKE, name="Nau", age=20, goal="muscle_building", experience="beginner",
              onboarding_step=1)
    kw.update(over)
    return make_user(db, **kw)


def _outbound(db, user):
    from models import Message
    db.expire_all()
    return [m.body for m in db.query(Message).filter(Message.user_id == user.id,
                                                      Message.direction == "out")
            .order_by(Message.id).all()]


# ── 1. one identity, every surface ───────────────────────────────────────────

def test_identity_is_the_first_thing_on_every_surface(db):
    from agent_loop import identity_prompt, _voice_prompt
    import onboarding_agent

    ident = identity_prompt()
    assert "friend at Berkeley" in ident and "Engage the specific thing" in ident
    assert "Know the campus" in ident and "Look things up" in ident
    # coach loop + heartbeat prefix
    assert _voice_prompt().startswith(ident)
    # onboarding
    sp = onboarding_agent._build_system_prompt(_new_signup(db))
    assert sp.startswith(ident)
    # voice.md no longer carries its own competing identity section
    from agent_loop import _VOICE_PATH
    raw = open(_VOICE_PATH, encoding="utf-8").read()
    assert "## Who you are" not in raw and "## How you talk" not in raw


def test_onboarding_prompt_lists_unknowns_and_forbids_the_list(db):
    import onboarding_agent
    user = _new_signup(db, height_ft=5, height_in=10, weight_lbs=170)
    sp = onboarding_agent._build_system_prompt(user)
    assert "STILL UNKNOWN" in sp
    assert "height and weight" not in sp.split("STILL UNKNOWN", 1)[1]  # already known
    assert "food situation" in sp                                       # still unknown
    assert "At most ONE question per message" in sp
    assert "## RIGHT NOW" in sp and "in Berkeley" in sp
    import re as _re
    assert _re.search(r"It's \w+day, \w{3} \d{1,2}, 20\d\d, \d{1,2}:\d\d[ap]m in Berkeley", sp), \
        "the day line must carry the YEAR — without it the model searches 'Fall 2024'"
    # the intake block is not in the DEFAULT prompt (the big ask exists, but only
    # when _intake_mode says a list is appropriate)
    assert "drop me everything" not in sp.lower() and "in one text" not in sp.lower()


# ── 2. the first reply is a friend reply, not a big ask ──────────────────────

def test_first_reply_is_one_friend_message_and_advances_to_step_2(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from models import User
    user = _new_signup(db)
    seen = {}

    def _handler(kwargs):
        # the extractor (Haiku) returns nothing; the generate call is the reply
        if _is_extract(kwargs):
            return "{}"
        seen["system"] = kwargs["system"]
        seen["instruction"] = kwargs["messages"][0]["content"]
        return "discrete math at 4 on a friday is criminal. what's it on — induction, or the counting stuff already"
    anthropic_stub.reply_with(_handler)

    done = onboarding_agent.handle_onboarding_reply(
        user, "ugh have a 70 quiz at 4 and i'm so behind")

    assert done is False
    assert len(sms_capture) == 1, "exactly one outbound — no big ask, no bundle"
    assert "criminal" in sms_capture[0][1]
    db.expire_all()
    assert db.get(User, user.id).onboarding_step == 2
    # the instruction asks for ONE woven question at most, never a list
    ins = seen["instruction"]
    assert "ONE question" in ins and "pick it before you write" in ins
    assert "drop" not in ins.lower() and "everything" not in ins.lower()
    assert seen["system"].startswith(onboarding_agent._build_system_prompt(user)[:200])


# ── 3. web_search: one definition, every surface, capped at 2, every query logged ──

def test_web_search_tool_is_shared_and_capped_at_two():
    import config
    from agent_tools import WEB_SEARCH_TOOL
    assert config.WEB_SEARCH_MAX_USES == 2
    assert WEB_SEARCH_TOOL["type"] == "web_search_20260209" and WEB_SEARCH_TOOL["name"] == "web_search"
    assert WEB_SEARCH_TOOL["max_uses"] == 2
    assert WEB_SEARCH_TOOL["user_location"]["city"] == "Berkeley"


def test_onboarding_generate_offers_search_joins_text_and_logs_query(db, anthropic_stub, monkeypatch, caplog):
    import config, onboarding_agent
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    user = _new_signup(db)
    seen = {}

    def _handler(kwargs):
        seen.update(tools=kwargs.get("tools"), thinking=kwargs.get("thinking"),
                    effort=kwargs.get("output_config"), max_tokens=kwargs.get("max_tokens"))
        return WebSearchUse("cs70 fall 2026 midterm date", text="70 midterm tuesday? that's the brutal one. eat before it")
    anthropic_stub.reply_with(_handler)

    with caplog.at_level(logging.INFO):
        text = onboarding_agent._generate("sys", "hi", user_id=user.id)

    assert text.startswith("70 midterm tuesday")
    from agent_tools import WEB_SEARCH_TOOL
    assert seen["tools"] == [WEB_SEARCH_TOOL]
    assert seen["thinking"] == {"type": "adaptive"} and seen["effort"] == {"effort": "medium"}
    assert seen["max_tokens"] == config.AGENT_LOOP_MAX_TOKENS, "thinking + search + reply share the loop ceiling"
    lines = [r.getMessage() for r in caplog.records if "WEB_SEARCH_QUERY" in r.getMessage()]
    assert lines and f"user={user.id}" in lines[0] and "site=onboarding.generate" in lines[0]
    assert "cs70 fall 2026 midterm date" in lines[0]


def test_onboarding_generate_no_tools_when_flag_off(anthropic_stub, monkeypatch):
    import config, onboarding_agent
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", False)
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(tools=kw.get("tools")) or "hey")
    assert onboarding_agent._generate("sys", "hi", user_id=1) == "hey"
    assert seen["tools"] is None


def test_onboarding_generate_resumes_pause_turn(anthropic_stub, monkeypatch):
    """A server tool that hits its iteration limit returns pause_turn — re-send."""
    import config, onboarding_agent
    from tests._fake_anthropic import MultiText
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    anthropic_stub.push(MultiText(("server_tool_use", ""), stop_reason="pause_turn"),
                        "gbc closes at 3 today, go now")
    assert onboarding_agent._generate("sys", "hi", user_id=1) == "gbc closes at 3 today, go now"
    assert len(anthropic_stub.calls) == 2
    assert anthropic_stub.calls[1]["messages"][1]["role"] == "assistant"


def test_agent_loop_offers_shared_tool_and_logs_query(db, anthropic_stub, monkeypatch, caplog):
    import config
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    user = make_user(db, name="Sam")
    seen = {}

    def _handler(kwargs):
        seen["tools"] = kwargs.get("tools")
        return WebSearchUse("rsf hours saturday", text="rsf closes at 8 on saturdays")
    anthropic_stub.reply_with(_handler)

    with caplog.at_level(logging.INFO):
        reply = run_agent_loop(user, "what time does rsf close sat", "freeform")

    assert "rsf closes at 8" in reply
    from agent_tools import WEB_SEARCH_TOOL
    ws = [t for t in seen["tools"] if t.get("name") == "web_search"]
    assert ws == [WEB_SEARCH_TOOL]
    lines = [r.getMessage() for r in caplog.records if "WEB_SEARCH_QUERY" in r.getMessage()]
    assert lines and f"user={user.id}" in lines[0] and "site=agent_loop.run" in lines[0]


def test_heartbeat_offers_the_shared_tool(db, anthropic_stub, monkeypatch):
    import config, heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_WEB_SEARCH", True)
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    user = make_user(db, name="Sam")
    seen = {}
    from tests._fake_anthropic import ToolUse
    anthropic_stub.reply_with(lambda kw: seen.setdefault("tools", kw.get("tools")) and
                              ToolUse("stay_silent", {"reason": "nothing to say"}))
    try:
        heartbeat.decide(user.id)
    except Exception:
        pass  # only the offered tool list is under test here
    from agent_tools import WEB_SEARCH_TOOL
    ws = [t for t in (seen.get("tools") or []) if t.get("name") == "web_search"]
    assert ws == [WEB_SEARCH_TOOL]


# ── 4. the extractor reads the coach's previous message ──────────────────────

def test_extractor_is_given_the_previous_coach_message(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from models import get_session, Message
    user = _new_signup(db, onboarding_step=2)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="you gonna hit the gym after or is today a wash",
                      message_type="onboarding"))
        s.commit()
    finally:
        s.close()
    prompts = []

    def _handler(kwargs):
        if _is_extract(kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            return '{"workout_days": "5"}'
        return "5 days is a real commitment. rsf or the dorm gym"
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "5")

    assert prompts, "the extractor ran"
    assert "you gonna hit the gym after" in prompts[0]
    assert "previous message" in prompts[0]
    db.expire_all()
    from models import User
    assert db.get(User, user.id).workout_days == "5"


# ── 5. summary gating ────────────────────────────────────────────────────────

def test_last_field_landing_presents_summary_even_if_message_says_ok(db, anthropic_stub, sms_capture):
    """Old bug: 'ok so no injuries' filled the last field AND matched the confirmation
    keyword 'ok' → onboarding completed without the summary ever being shown."""
    import onboarding_agent
    from models import User
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=10, weight_lbs=170,
                       occupation="student", activity_level="active — walks campus", avg_steps=8000,
                       workout_days="4", workout_time="17:00", current_split="none",
                       cooking_situation="dining_hall", diet="omnivore", wake_time="08:00",
                       sleep_time="00:00", existing_tools="none")  # injuries still unknown
    gen_instructions = []

    def _handler(kwargs):
        if _is_extract(kwargs):
            return '{"injuries": "none"}'
        gen_instructions.append(kwargs["messages"][0]["content"])
        return "alr here's what i'm working with ... sound right?"
    anthropic_stub.reply_with(_handler)

    done = onboarding_agent.handle_onboarding_reply(user, "ok so no injuries")

    assert done is False, "must NOT complete — the summary was never shown"
    assert len(sms_capture) == 1 and "sound right" in sms_capture[0][1]
    assert "Present this summary" in gen_instructions[0]
    db.expire_all()
    assert db.get(User, user.id).onboarding_step == 2

    # now a confirmation TO the summary completes
    sms_capture.clear()
    def _handler2(kwargs):
        if _is_extract(kwargs):
            return "{}"
        return "locked in. you'll hear from me at 8"
    anthropic_stub.reply_with(_handler2)
    db.expire_all()
    assert onboarding_agent.handle_onboarding_reply(db.get(User, user.id), "yeah sounds good") is True
    db.expire_all()
    assert db.get(User, user.id).onboarding_step == 3


# ── 6. the list is kept for when it's appropriate ────────────────────────────

def test_intake_mode_rules():
    from onboarding_agent import _intake_mode, BIG_ASK_AFTER_TURNS, BUNDLE_AFTER_TURNS
    many = [("a", "x"), ("b", "y"), ("c", "z"), ("d", "w")]
    two = many[:2]
    # default: friend
    assert _intake_mode("ugh 70 quiz at 4", many, turns=1) == "friend"
    assert _intake_mode("cool", two, turns=1) == "friend"
    # they ask for the list → big ask (or bundle if only 1-2 left)
    for msg in ("what do you need from me", "just tell me what you need", "what info do you need",
                "send me the questions", "what should i send you", "what else do u need"):
        assert _intake_mode(msg, many, turns=1) == "big_ask", msg
        assert _intake_mode(msg, two, turns=1) == "bundle", msg
    # long conversation, most fields still unknown → big ask
    assert _intake_mode("yeah", many, turns=BIG_ASK_AFTER_TURNS) == "big_ask"
    assert _intake_mode("yeah", many, turns=BIG_ASK_AFTER_TURNS - 1) == "friend"
    # one or two left after a real conversation → bundle
    assert _intake_mode("yeah", two, turns=BUNDLE_AFTER_TURNS) == "bundle"
    assert _intake_mode("yeah", two, turns=BUNDLE_AFTER_TURNS - 1) == "friend"
    # nothing unknown never picks a list
    assert _intake_mode("what do you need", [], turns=9) == "friend"


def test_asking_for_the_list_sends_the_big_ask_in_the_friend_voice(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _new_signup(db, onboarding_step=2)
    seen = {}

    def _handler(kwargs):
        if _is_extract(kwargs):
            return "{}"
        seen["instruction"] = kwargs["messages"][0]["content"]
        return "alr real talk, just send me the basics in one go - height, weight, what your days look like, food situation, sleep."
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "lol just tell me what you need")

    assert len(sms_capture) == 1
    ins = seen["instruction"]
    assert "drop the basics in ONE text" in ins and "height and weight" in ins
    assert "react to the specific thing they said" in ins


def _seed_conversation(user_id, coach_replies: int, inbound_texts: int):
    """A conversation: the hook, `coach_replies` onboarding replies, and `inbound_texts`
    user texts (bursty users send several per turn)."""
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction="out", body="hey how's your day going?", message_type="onboarding"))
        for i in range(coach_replies):
            s.add(Message(user_id=user_id, direction="out", body=f"reply {i}", message_type="onboarding"))
        for i in range(inbound_texts):
            s.add(Message(user_id=user_id, direction="in", body=f"text {i}", message_type="freeform"))
        s.commit()
    finally:
        s.close()


def test_turns_are_coach_replies_not_inbound_texts(db):
    """Live bug (2026-09-11): the founder sent 6 texts across 2 exchanges and the big
    ask fired on exchange two. A burst of texts is ONE turn; count coach replies."""
    import onboarding_agent
    user = _new_signup(db, onboarding_step=2)
    _seed_conversation(user.id, coach_replies=1, inbound_texts=6)
    assert onboarding_agent._coach_turns(user.id) == 1  # hook excluded


def test_long_conversation_with_most_unknown_escalates_to_big_ask(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _new_signup(db, onboarding_step=2)
    _seed_conversation(user.id, coach_replies=onboarding_agent.BIG_ASK_AFTER_TURNS, inbound_texts=6)
    seen = {}
    def _handler(kwargs):
        if _is_extract(kwargs):
            return "{}"
        seen["instruction"] = kwargs["messages"][0]["content"]
        return "ok real talk"
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "haha yeah")

    assert "drop the basics in ONE text" in seen["instruction"]

    # one reply fewer → still the friend
    sms_capture.clear(); seen.clear()
    user2 = _new_signup(db, onboarding_step=2)
    _seed_conversation(user2.id, coach_replies=onboarding_agent.BIG_ASK_AFTER_TURNS - 1, inbound_texts=12)
    onboarding_agent.handle_onboarding_reply(user2, "haha yeah")
    assert "drop the basics in ONE text" not in seen["instruction"]


def test_two_left_after_real_conversation_bundles(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from models import get_session, Message
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=10, weight_lbs=170,
                       occupation="student", activity_level="active", avg_steps=8000,
                       workout_days="4", workout_time="17:00", current_split="none",
                       cooking_situation="dining_hall", diet="omnivore", wake_time="08:00",
                       sleep_time="00:00")  # injuries + existing_tools still unknown
    _seed_conversation(user.id, coach_replies=onboarding_agent.BUNDLE_AFTER_TURNS, inbound_texts=4)
    seen = {}
    def _handler(kwargs):
        if _is_extract(kwargs):
            return "{}"
        seen["instruction"] = kwargs["messages"][0]["content"]
        return "last thing - anything banged up, and you tracking on any apps?"
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "yeah that's about it")

    ins = seen["instruction"]
    assert "last thing" in ins and "any injuries" in ins and "fitness apps" in ins
    assert "ONE text" not in ins


# ── 7. extraction: anecdotes aren't facts; latest clear statement wins ────────

def test_extractor_uses_sonnet_and_states_the_anecdote_rule(db, anthropic_stub):
    import config, onboarding_agent
    user = _new_signup(db, onboarding_step=2)
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(model=kw.get("model"), prompt=kw["messages"][0]["content"]) or "{}")
    onboarding_agent._extract_data_from_message("we got malatang after", user)
    assert seen["model"] == config.ONBOARDING_EXTRACTOR_MODEL == "claude-sonnet-5"
    assert "AN ANECDOTE IS NOT A FACT" in seen["prompt"]
    assert "never fill diet=\"omnivore\" unless" in seen["prompt"]


def test_store_latest_clear_statement_wins_during_onboarding(db):
    """Live bug: an early over-inference (mostly_eat_out) was made permanent by
    first-write-wins; the explicit 'I mostly cook' 30s later was dropped."""
    import onboarding_agent
    from models import User
    user = _new_signup(db, onboarding_step=2)
    onboarding_agent._store_extracted_data(user.id, {"cooking_situation": "mostly_eat_out"})
    onboarding_agent._store_extracted_data(user.id, {"cooking_situation": "mix", "diet": None})
    db.expire_all()
    u = db.get(User, user.id)
    assert u.cooking_situation == "mix"
    assert u.diet is None  # a null never clears a value, never invents one


def test_store_is_inert_after_onboarding(db):
    import onboarding_agent
    from models import User
    user = make_user(db, onboarding_step=3, cooking_situation="cook_myself")
    onboarding_agent._store_extracted_data(user.id, {"cooking_situation": "mostly_eat_out"})
    db.expire_all()
    assert db.get(User, user.id).cooking_situation == "cook_myself"


def test_store_accepts_year_and_meal_plan_status(db):
    """Live: "I'm a junior so I don't got the dining hall pass" — two Berkeley facts the
    User model already has columns for; the extractor now has keys for them."""
    import onboarding_agent
    from models import User
    user = _new_signup(db, onboarding_step=2)
    onboarding_agent._store_extracted_data(user.id, {"year": "junior", "meal_plan_status": "no_meal_plan"})
    db.expire_all()
    u = db.get(User, user.id)
    assert (u.year, u.meal_plan_status) == ("junior", "no_meal_plan")
