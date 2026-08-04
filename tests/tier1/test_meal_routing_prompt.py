"""
Macro-accuracy Phase E — the escalation-routing prompt block.

Routing applies to EVERY reactive turn (meals arrive mostly as text; there is
deliberately no pre-classifier), so unlike Phase A's image-only block this one
extends the CACHED stable prefix: system = [voice (cached), routing (cached),
volatile context, (Phase A block on image turns)]. Tier-1 pins that layout and the
heartbeat isolation. Whether the model actually climbs the ladder correctly is
tier-2 (judged, with recorded tool dispatches).
"""

from __future__ import annotations

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}

# Stable marker phrases; keep in sync with the prompt files if reworded.
_ROUTING_MARKER = "kind of information"
_PHASE_A_MARKER = "reference object"


def _enable(monkeypatch, **flags):
    import config
    defaults = dict(MEAL_ROUTING_PROMPT_ENABLED=True, READ_IMAGE_ENABLED=True,
                    MEAL_ESTIMATION_PROMPT_ENABLED=True, LOG_MEAL_TOOL_ENABLED=True)
    defaults.update(flags)
    for name, val in defaults.items():
        monkeypatch.setattr(config, name, val)


def _capture(anthropic_stub, reply="got it"):
    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        return reply

    anthropic_stub.reply_with(handler)
    return captured


def test_routing_block_cached_between_voice_and_context_on_text_turn(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop, _voice_prompt

    _enable(monkeypatch)
    captured = _capture(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "had chicken and rice", "freeform")

    system = captured["system"]
    assert isinstance(system, list)
    assert system[0]["text"] == _voice_prompt() and "cache_control" in system[0]
    assert _ROUTING_MARKER in system[1]["text"], \
        "routing must sit directly after voice (the stable prefix), before volatile context"
    assert "cache_control" in system[1], \
        "routing is stable text and must extend the cached prefix (every turn pays it otherwise)"
    assert all(_ROUTING_MARKER not in b["text"] for b in system[2:]), \
        "routing duplicated outside the prefix"


def test_image_turn_carries_routing_and_phase_a_block_in_order(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    _enable(monkeypatch)
    captured = _capture(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "lunch", "freeform", image_data=_IMG)

    system = captured["system"]
    texts = [b["text"] for b in system]
    assert _ROUTING_MARKER in texts[1]
    assert _PHASE_A_MARKER in texts[-1], \
        "Phase A portion block stays LAST (after volatile context), uncached"
    assert "cache_control" not in system[-1]


def test_absent_when_flag_off(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    _enable(monkeypatch, MEAL_ROUTING_PROMPT_ENABLED=False)
    captured = _capture(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "had chicken and rice", "freeform")

    joined = "\n".join(b["text"] for b in captured["system"])
    assert _ROUTING_MARKER not in joined


def test_string_system_branch_orders_voice_routing_context(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop, _voice_prompt

    _enable(monkeypatch, PROMPT_CACHING_ENABLED=False)
    captured = _capture(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "had chicken and rice", "freeform")

    system = captured["system"]
    assert isinstance(system, str)
    v, r = system.find(_voice_prompt()[:40]), system.find(_ROUTING_MARKER)
    assert 0 <= v < r, "string branch must keep voice → routing → context order"


def test_heartbeat_system_never_contains_routing_block(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    _enable(monkeypatch)
    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])

    captured = {}

    def handler(kw):
        captured.setdefault("systems", []).append(kw.get("system"))
        return ToolUse("stay_silent", {"reason": "nothing new"})

    anthropic_stub.reply_with(handler)
    heartbeat.heartbeat_tick(user.id)

    assert captured.get("systems"), "tick never reached the model — setup broke, not the claim"
    for system in captured["systems"]:
        texts = [system] if isinstance(system, str) else [b["text"] for b in system]
        assert all(_ROUTING_MARKER not in t for t in texts), \
            "routing guidance leaked into the heartbeat system prompt"
