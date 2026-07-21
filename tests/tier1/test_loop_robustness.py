"""
Loop robustness — the burn-in truncation finding. A multi-item tool turn (a calendar
screenshot → several log_event calls) can hit the output cap: thinking + tool JSON +
reply all count against max_tokens on Sonnet 5. The old loop capped at 400, hit
stop_reason=max_tokens with no text, and raised a bare "no text block" — forcing a
guess from token counts. These lock in: a raised cap, truncation as its own branch
with named logging, graceful iteration-bound degradation, and diagnostic no-text logs.
"""

from __future__ import annotations

import logging

import pytest


def _enable_loop(monkeypatch):
    import config
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)


def test_loop_uses_the_raised_output_cap(db, monkeypatch, anthropic_stub):
    """The loop must request AGENT_LOOP_MAX_TOKENS (room for thinking+tools+reply),
    not the 400-token SMS reply cap that truncated multi-item turns."""
    import config
    from agent_loop import run_agent_loop
    from tests.factories import make_user

    _enable_loop(monkeypatch)
    anthropic_stub.reply_with(lambda kw: "noted.")
    user = make_user(db)

    run_agent_loop(user, "hey", "freeform")

    assert anthropic_stub.calls[-1]["max_tokens"] == config.AGENT_LOOP_MAX_TOKENS
    assert config.AGENT_LOOP_MAX_TOKENS >= 2000, "cap must be substantially above the 400 SMS cap"


def test_truncation_with_partial_text_returns_it_and_logs(db, monkeypatch, anthropic_stub, caplog):
    from agent_loop import run_agent_loop
    from tests._fake_anthropic import Truncated
    from tests.factories import make_user

    _enable_loop(monkeypatch)
    anthropic_stub.reply_with(lambda kw: Truncated("here's what i got so far"))
    user = make_user(db)

    with caplog.at_level(logging.WARNING):
        reply = run_agent_loop(user, "big schedule dump", "freeform")

    assert reply == "here's what i got so far", "partial text on truncation must be returned, not dropped"
    assert "AGENT_LOOP_TRUNCATED" in caplog.text
    assert "stop=max_tokens" in caplog.text and "blocks=" in caplog.text


def test_truncation_without_text_degrades_not_raises(db, monkeypatch, anthropic_stub, caplog):
    from agent_loop import run_agent_loop, _LOOP_DEGRADE_REPLY
    from tests._fake_anthropic import Truncated
    from tests.factories import make_user

    _enable_loop(monkeypatch)
    anthropic_stub.reply_with(lambda kw: Truncated(""))  # cap hit before any text
    user = make_user(db)

    with caplog.at_level(logging.WARNING):
        reply = run_agent_loop(user, "big schedule dump", "freeform")  # must NOT raise

    assert reply == _LOOP_DEGRADE_REPLY
    assert "AGENT_LOOP_TRUNCATED" in caplog.text


def test_iteration_bound_degrades_gracefully_never_raises(db, monkeypatch, anthropic_stub, caplog):
    """A model that never stops calling tools must not raise into fallback — the tool
    writes persisted; degrade with a best-effort reply and log the bound by name."""
    import config
    from agent_loop import run_agent_loop, _LOOP_DEGRADE_REPLY
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    _enable_loop(monkeypatch)
    monkeypatch.setattr(config, "AGENT_LOOP_MAX_TOOL_ITERS", 3)  # exhaust quickly
    anthropic_stub.reply_with(
        lambda kw: ToolUse("log_event", {"description": "loop item"}))  # never terminates
    user = make_user(db)

    with caplog.at_level(logging.WARNING):
        reply = run_agent_loop(user, "log a bunch", "freeform")  # must NOT raise

    assert reply == _LOOP_DEGRADE_REPLY
    assert "AGENT_LOOP_MAX_ITERS" in caplog.text


def test_no_text_anomaly_logs_diagnostics_then_raises(db, monkeypatch, anthropic_stub, caplog):
    """A terminal stop with no text is a genuine anomaly: log stop_reason + block types
    (the observability that was missing), then raise so the caller falls back to legacy."""
    from agent_loop import run_agent_loop
    from tests._fake_anthropic import _Response
    from tests.factories import make_user

    _enable_loop(monkeypatch)

    def empty_end_turn(kw):
        r = _Response("")
        r.content = []          # end_turn but no text block — anomalous
        r.stop_reason = "end_turn"
        return r
    anthropic_stub.reply_with(empty_end_turn)
    user = make_user(db)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError) as exc:
            run_agent_loop(user, "hi", "freeform")

    assert "stop_reason=end_turn" in str(exc.value) and "blocks=" in str(exc.value)
    assert "AGENT_LOOP_NO_TEXT" in caplog.text
