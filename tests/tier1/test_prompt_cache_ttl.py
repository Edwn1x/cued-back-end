"""
Prompt-cache TTL + shared prefix (2026-10-26): cache WRITES were 61% of the Opus bill
because the ~33k-token voice prompt sat behind a 5-minute TTL and three sites built a
DIFFERENT byte prefix (identity prepended twice). Every stable prefix now carries the
1-hour TTL via agent_loop.stable_cache_control(), every site caches the byte-identical
_voice_prompt(), and the cost tracker prices 1-hour writes at 2×.
"""

from __future__ import annotations

import types


def test_stable_cache_control_default_is_one_hour(monkeypatch):
    import config
    from agent_loop import stable_cache_control
    monkeypatch.setattr(config, "PROMPT_CACHE_TTL", "1h")
    assert stable_cache_control() == {"type": "ephemeral", "ttl": "1h"}
    monkeypatch.setattr(config, "PROMPT_CACHE_TTL", "5m")
    assert stable_cache_control() == {"type": "ephemeral"}
    monkeypatch.setattr(config, "PROMPT_CACHE_TTL", "")
    assert stable_cache_control() == {"type": "ephemeral"}


def test_voice_prompt_starts_with_identity_once():
    from agent_loop import _voice_prompt, identity_prompt
    v = _voice_prompt()
    ident = identity_prompt().strip()
    assert v.lstrip().startswith(ident[:60])
    assert v.count(ident[:200]) == 1, "identity must appear exactly once in the shared prefix"


def test_heartbeat_system_uses_shared_prefix_with_ttl(db, monkeypatch, anthropic_stub):
    import config, heartbeat
    from agent_loop import _voice_prompt
    from tests.factories import make_user
    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])
    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        from tests._fake_anthropic import ToolUse
        return ToolUse("stay_silent", {"reason": "x"})
    anthropic_stub.reply_with(handler)
    heartbeat.decide(user.id)
    sys0 = captured["system"][0]
    assert sys0["text"] == _voice_prompt(), "byte-identical shared prefix"
    assert sys0["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in captured["system"][-1], "per-tick context stays uncached (ordering rule)"


def test_reminder_compose_uses_shared_prefix_with_ttl(db, monkeypatch, anthropic_stub):
    import reminders
    from agent_loop import _voice_prompt
    from models import Reminder
    from tests.factories import make_user
    user = make_user(db, name="Alex")
    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        return "water time"
    anthropic_stub.reply_with(handler)
    row = Reminder(user_id=user.id, text="drink water", every_hours=2)
    out = reminders._compose(user, row)
    assert out == "water time"
    sys0 = captured["system"][0]
    assert sys0["text"] == _voice_prompt(), "a water ping must READ the warm voice entry, not write its own"
    assert sys0["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "Alex" in captured["system"][1]["text"] and "cache_control" not in captured["system"][1]


def test_workout_review_system_uses_shared_prefix_on_both_stable_blocks():
    from agent_loop import _voice_prompt
    from workouts.review import _system, training_knowledge
    sys_ = _system("Ed", "America/Los_Angeles")
    assert sys_[0]["text"] == _voice_prompt()
    assert sys_[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert sys_[1]["text"] == training_knowledge()
    assert sys_[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in sys_[2]


def test_no_site_prepends_identity_to_the_voice_prompt():
    """Source-level guard: the duplicated-identity prefix (a different byte string that
    can never share the warm cache) must not come back at any call site."""
    import pathlib
    bad = 'identity_prompt() + "\\n\\n---\\n\\n" + _voice_prompt()'
    for f in ("reminders.py", "onboarding_reengage.py", "workouts/review.py", "agent_tasks.py", "heartbeat.py"):
        src = pathlib.Path(f).read_text()
        assert bad not in src, f"{f} rebuilds a unique prefix"


def test_agent_loop_reactive_system_marks_stable_blocks_one_hour(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    from agent_loop import run_agent_loop, _voice_prompt
    user = make_user(db)
    captured = {}

    def handler(kw):
        captured.setdefault("systems", []).append(kw.get("system"))
        return "ok"
    anthropic_stub.reply_with(handler)
    run_agent_loop(user, "hey", "freeform")
    systems = captured.get("systems") or []
    assert systems, "the reactive loop must have called the model"
    sys_ = systems[0]
    assert isinstance(sys_, list)
    assert sys_[0]["text"] == _voice_prompt()
    assert sys_[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in sys_[-1]
    # ordering rule: no 5-minute entry may precede a 1-hour entry
    seen_5m = False
    for b in sys_:
        cc = b.get("cache_control")
        if cc and cc.get("ttl") == "1h":
            assert not seen_5m, "a 1h block after a 5m block violates the API ordering rule"
        elif cc:
            seen_5m = True


# ---- cost tracker ----------------------------------------------------------------

def _usage(inp=0, cc=0, cr=0, out=0, cc1h=None):
    u = types.SimpleNamespace(input_tokens=inp, cache_creation_input_tokens=cc,
                              cache_read_input_tokens=cr, output_tokens=out)
    if cc1h is not None:
        u.cache_creation = types.SimpleNamespace(ephemeral_1h_input_tokens=cc1h,
                                                 ephemeral_5m_input_tokens=cc - cc1h)
    return u


def test_compute_cost_prices_one_hour_writes_at_2x():
    import config
    from cost_tracking import compute_cost
    rate = config.MODEL_PRICING["opus"]["input"]
    five = compute_cost("opus", _usage(cc=1_000_000))                 # no breakdown → 5m
    assert abs(five - rate * 1.25) < 1e-9
    one_h = compute_cost("opus", _usage(cc=1_000_000, cc1h=1_000_000))
    assert abs(one_h - rate * 2.0) < 1e-9
    mixed = compute_cost("opus", _usage(cc=1_000_000, cc1h=250_000))
    assert abs(mixed - (rate * 0.75 * 1.25 + rate * 0.25 * 2.0)) < 1e-9
    assert abs(compute_cost("opus", _usage(cr=1_000_000)) - rate * 0.10) < 1e-9
    assert compute_cost("opus", _usage(cc=100, cc1h=10_000)) <= 100 * rate * 2.0 / 1e6 + 1e-12, "breakdown is clamped to the total"


def test_sonnet_pricing_matches_the_model_in_use():
    import config
    assert config.MEMORY_EXTRACTOR_MODEL.startswith("claude-sonnet-5")
    assert config.MODEL_PRICING["sonnet"] == {"input": 2.00, "output": 10.00}
