"""
Opus 4.8 → Opus 5.5 (2026-10-26, lever 4 of the cost pass): a model-id change plus
pricing. The request surface was already 5.5-compatible (adaptive thinking, explicit
effort, no forced tool_choice, no thinking replay across turns); these pin what the
migration touched and the pricing the tracker now applies.
"""

from __future__ import annotations

import types


def test_default_models_are_opus_5_5():
    import config
    assert config.AGENT_LOOP_MODEL == "claude-opus-5-5"
    assert config.COACH_MODEL == "claude-opus-5-5"


def test_model_key_separates_opus_5_5_from_older_opus():
    from cost_tracking import _model_key
    assert _model_key("claude-opus-5-5") == "opus55"
    assert _model_key("claude-opus-4-8") == "opus"
    assert _model_key("claude-opus-5") == "opus"
    assert _model_key("claude-sonnet-5") == "sonnet"
    assert _model_key("claude-haiku-4-5-20251001") == "haiku"
    assert len("opus55") <= 10, "token_usage.model is VARCHAR(10)"


def _usage(inp=0, cc=0, cr=0, out=0):
    return types.SimpleNamespace(input_tokens=inp, cache_creation_input_tokens=cc,
                                 cache_read_input_tokens=cr, output_tokens=out)


def test_opus_5_5_pricing_and_cache_read_rate():
    import config
    from cost_tracking import compute_cost
    p = config.MODEL_PRICING["opus55"]
    assert (p["input"], p["output"], p["cache_read"]) == (4.00, 20.00, 0.05)
    assert abs(compute_cost("opus55", _usage(inp=1_000_000)) - 4.00) < 1e-9
    assert abs(compute_cost("opus55", _usage(out=1_000_000)) - 20.00) < 1e-9
    assert abs(compute_cost("opus55", _usage(cr=1_000_000)) - 0.20) < 1e-9, "cache reads at $0.20/M"
    assert abs(compute_cost("opus55", _usage(cc=1_000_000)) - 5.00) < 1e-9, "5-min write 1.25× of $4"
    # older Opus rows keep the old rates (as-charged history stays correct)
    assert abs(compute_cost("opus", _usage(cr=1_000_000)) - 0.50) < 1e-9


def test_request_surface_is_5_5_compatible(db, monkeypatch, anthropic_stub):
    """No thinking budgets, no disabled thinking, no forced tool_choice on the hot paths."""
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    import heartbeat, config
    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])
    seen = []

    def handler(kw):
        seen.append(kw)
        from tests._fake_anthropic import ToolUse
        return ToolUse("stay_silent", {"reason": "x"}) if "stay_silent" in str(kw.get("tools")) else "ok"
    anthropic_stub.reply_with(handler)
    run_agent_loop(user, "hey", "freeform")
    heartbeat.decide(user.id)
    assert len(seen) >= 2
    for kw in seen:
        assert kw.get("model") == "claude-opus-5-5"
        assert kw.get("thinking") == {"type": "adaptive"}
        assert "budget_tokens" not in str(kw.get("thinking"))
        assert kw.get("output_config", {}).get("effort") in ("low", "medium", "high")
        assert kw.get("tool_choice") in (None, {"type": "auto"})


def test_heartbeat_names_a_refusal(db, monkeypatch, anthropic_stub):
    import heartbeat, config
    from tests.factories import make_user
    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])

    from tests._fake_anthropic import _Response

    def refused(kw):
        r = _Response("")
        r.content = []                     # declined before any output
        r.stop_reason = "refusal"
        r.stop_details = types.SimpleNamespace(type="refusal", category="cyber")
        return r
    anthropic_stub.reply_with(refused)
    spoke, payload, _ = heartbeat.decide(user.id)
    assert spoke is False and payload.startswith("refusal:cyber")
