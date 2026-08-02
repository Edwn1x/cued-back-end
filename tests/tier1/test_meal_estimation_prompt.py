"""
Macro-accuracy Phase A — the meal-estimation prompt block (reference-object scaling).

The estimation guidance ships as a SEPARATE flag-gated system block injected only on
reactive image turns — never as a voice.md edit, because voice.md is the heartbeat's
cached system prefix too (rewrite/macro-accuracy/INVESTIGATION.md §1.3). Tier-1 pins
the composition: present exactly when it should be, voice block untouched, heartbeat
isolated. Whether the model USES the technique is tier-2.
"""

from __future__ import annotations

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}

# Stable marker phrase from prompts/meal_estimation.md — the composition assertions
# key on it. If the file is reworded, keep this phrase (or update both together).
_MARKER = "reference object"


def _enable(monkeypatch, **flags):
    import config
    defaults = dict(READ_IMAGE_ENABLED=True, MEAL_ESTIMATION_PROMPT_ENABLED=True,
                    LOG_MEAL_TOOL_ENABLED=True)
    defaults.update(flags)
    for name, val in defaults.items():
        monkeypatch.setattr(config, name, val)


def _capture_system(anthropic_stub, reply="looks solid"):
    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        return reply

    anthropic_stub.reply_with(handler)
    return captured


def _system_texts(system) -> list[str]:
    if isinstance(system, str):
        return [system]
    return [b["text"] for b in system]


def test_estimation_block_present_on_image_turn(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop, _voice_prompt

    _enable(monkeypatch)
    captured = _capture_system(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "lunch", "freeform", image_data=_IMG)

    system = captured["system"]
    assert isinstance(system, list)
    joined = "\n".join(_system_texts(system))
    assert _MARKER in joined, "estimation guidance missing from an image turn with the flag on"
    # voice.md prefix is byte-untouched and stays the ONLY cached block — the
    # heartbeat shares that file and its cache; this pin is the no-blast-radius claim.
    assert system[0]["text"] == _voice_prompt()
    assert "cache_control" in system[0]
    assert _MARKER not in system[0]["text"], "guidance must not be spliced into voice.md"
    est_blocks = [b for b in system[1:] if _MARKER in b["text"]]
    assert est_blocks and all("cache_control" not in b for b in est_blocks), \
        "estimation block must sit after the cache breakpoint, uncached"


def test_estimation_block_absent_when_flag_off(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    _enable(monkeypatch, MEAL_ESTIMATION_PROMPT_ENABLED=False)
    captured = _capture_system(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "lunch", "freeform", image_data=_IMG)

    assert _MARKER not in "\n".join(_system_texts(captured["system"]))


def test_estimation_block_absent_on_text_only_turn(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    _enable(monkeypatch)
    captured = _capture_system(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "had chicken and rice", "freeform")

    assert _MARKER not in "\n".join(_system_texts(captured["system"])), \
        "text-only turns must not pay for the vision-estimation block"


def test_estimation_block_absent_when_read_image_off(db, monkeypatch, anthropic_stub):
    """read_image off → the model never sees the image; portion-from-photo guidance
    would be pure noise on a turn it can't act on."""
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    _enable(monkeypatch, READ_IMAGE_ENABLED=False)
    captured = _capture_system(anthropic_stub, reply="what's in it?")
    user = make_user(db)
    run_agent_loop(user, "lunch", "freeform", image_data=_IMG)

    assert _MARKER not in "\n".join(_system_texts(captured["system"]))


def test_estimation_block_in_string_system_when_caching_off(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    _enable(monkeypatch, PROMPT_CACHING_ENABLED=False)
    captured = _capture_system(anthropic_stub)
    user = make_user(db)
    run_agent_loop(user, "lunch", "freeform", image_data=_IMG)

    system = captured["system"]
    assert isinstance(system, str) and _MARKER in system


def test_heartbeat_system_never_contains_estimation_block(db, monkeypatch, sms_capture, anthropic_stub):
    """Isolation pin: the heartbeat composes its own system from the same voice.md;
    the estimation block must never reach the proactive surface, whatever the flags."""
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
        assert _MARKER not in "\n".join(_system_texts(system)), \
            "estimation guidance leaked into the heartbeat system prompt"
