"""
Founder, 2026-10-18: "he's a beginner, we shouldn't be asking him to give us what he runs
cause he doesn't run anything and he's listening to us. we're the experts here so we
should be guiding him." Live: user 40, after his SECOND session: "it's still the default
full body setup tho… want me to save what u actually run so next week's dialed in?"
"""
from __future__ import annotations

from types import SimpleNamespace as NS

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import FOUNDER, imessage_on, sidecar_ok, card_ok, sync_threads  # noqa: F401

BEGINNER = dict(FOUNDER, experience="none", current_split="none", split_pointer_day=None, split_pointer_source=None, lift_anchors=None)


def test_the_start_tool_tells_the_coach_to_prescribe_not_poll_a_beginner(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub):
    from agent_tools import handle_start_workout_session, _defaults_instruction
    anthropic_stub.reply_with(lambda kw: "nope")
    u = make_user(db, preferred_channel="imessage", **BEGINNER)
    r = handle_start_workout_session(u.id, {"template_key": "full_body", "no_anchors": True})
    assert "BEGINNER" in r and "NEVER ask what they run" in r and "WHY it's built this way" in r, r
    assert "ask what they actually run" not in r
    v = make_user(db, preferred_channel="imessage", phone="+15550009801", **FOUNDER)            # ppl on file, intermediate
    r2 = handle_start_workout_session(v.id, {"template_key": "push"})
    assert "ask what they actually run" in r2 and "BEGINNER" not in r2
    w = make_user(db, preferred_channel="imessage", phone="+15550009803", **dict(FOUNDER, current_split=None, split_pointer_day=None, split_pointer_source=None))
    r3 = handle_start_workout_session(w.id, {"template_key": "push"})                            # intermediate, no split told → they run something: ask
    assert "ask what they actually run" in r3 and "BEGINNER" not in r3
    assert "BEGINNER" in _defaults_instruction("push", beginner=True) and "[silent]" in _defaults_instruction("push")


def test_the_context_block_and_the_voice_carry_the_expert_stance(db, anthropic_stub):
    import agent_loop
    from agent_loop import _voice_prompt
    anthropic_stub.reply_with(lambda kw: "ok")
    u = make_user(db, **BEGINNER)
    agent_loop.run_agent_loop(u, "what's my workout today", "freeform")
    texts = []
    for c in anthropic_stub.calls:
        for b in c.get("system", []) or []:
            texts.append(b.get("text", "") if isinstance(b, dict) else str(b))
        for m in c.get("messages", []) or []:
            texts.append(str(m.get("content")))
    seen = " ".join(texts)
    assert "they're a BEGINNER following you" in seen and "NEVER ask what they run" in seen
    assert "offer to save what they actually run" not in seen
    v = " ".join(_voice_prompt().split())
    assert "You prescribe; you don't poll" in v and "Not for a beginner:" in v and "never ask what they run or what they'd like the plan to be" in v


def test_reconstruct_for_a_beginner_says_build_it_yourself(db):
    from agent_tools import handle_reconstruct_routine_from_history
    u = make_user(db, **BEGINNER)
    r = handle_reconstruct_routine_from_history(u.id, {"template_key": "push"})
    assert "beginner with no routine of their own" in r and "Build the day yourself" in r and "Ask them" not in r
    v = make_user(db, phone="+15550009802", **FOUNDER)
    assert "Ask them what they run" in handle_reconstruct_routine_from_history(v.id, {"template_key": "push"})
