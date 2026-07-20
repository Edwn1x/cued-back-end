"""
Phase 3 tool 6 — read_image. No pre-classifier: the inbound MMS goes to the model's
vision and IT routes food/calendar/whiteboard/other in-call (voice.md). Tier-1
covers the gating (image reaches the model when enabled; a safe note when not — never
invents). The food/calendar/whiteboard routing itself is tier-2, validated against
real screenshots when they land.
"""

from __future__ import annotations

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}


def test_image_reaches_vision_when_enabled(db, monkeypatch, anthropic_stub):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", True)
    captured = {}

    def handler(kw):
        captured["messages"] = kw.get("messages")
        return "that plate looks solid"

    anthropic_stub.reply_with(handler)
    user = make_user(db)
    run_agent_loop(user, "check this out", "freeform", image_data=_IMG)

    content = captured["messages"][0]["content"]
    assert isinstance(content, list)
    assert any(b.get("type") == "image" for b in content), "image not sent to the model's vision"


def test_image_not_sent_and_noted_when_disabled(db, monkeypatch, anthropic_stub):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "READ_IMAGE_ENABLED", False)
    captured = {}

    def handler(kw):
        captured["messages"] = kw.get("messages")
        return "what's in it?"

    anthropic_stub.reply_with(handler)
    user = make_user(db)
    run_agent_loop(user, "check this out", "freeform", image_data=_IMG)

    content = captured["messages"][0]["content"]
    assert isinstance(content, str) and "image you can't see" in content, \
        "with read_image off, the model must be told it can't see the image (not fed vision)"
