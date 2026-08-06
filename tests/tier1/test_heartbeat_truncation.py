"""
Heartbeat truncation + stop_reason observability (rewrite/heartbeat-truncation).

Live bug (Aug 6): the decide call ran under MAX_RESPONSE_TOKENS (400, the SMS
reply cap) — adaptive thinking + tool JSON + the composed message share that one
output budget, so a long-reasoning tick hit the ceiling and the truncated
response fell through to the bare-text fallback as reason="no message composed",
indistinguishable from chosen silence. These pin the fix: a dedicated ceiling, an
explicit max_tokens branch, and stop_reason visible in the logs on every outcome
so the next instance of this class announces itself.
"""

from __future__ import annotations

import logging

from tests.tier1.test_heartbeat import _allow, _ticks


def _tick_records(caplog):
    return [r.getMessage() for r in caplog.records if "HEARTBEAT_TICK" in r.getMessage()]


def _tokens_records(caplog):
    return [r.getMessage() for r in caplog.records
            if "TOKENS" in r.getMessage() and "site=heartbeat.decide" in r.getMessage()]


# ---- Fix 1: truncation is its own outcome, never chosen silence -------------

def test_truncated_decide_is_not_chosen_silence(db, monkeypatch, sms_capture,
                                                anthropic_stub, caplog):
    """stop_reason=max_tokens with no text (thinking ate the budget before any
    block was emitted — the Aug 6 shape) must be recorded as truncation, NOT as
    the coach choosing silence via 'no message composed'."""
    import heartbeat
    from tests._fake_anthropic import Truncated
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: Truncated())

    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)

    assert sms_capture == []
    tick = _ticks(user.id)[-1]
    assert tick.spoke is False
    assert tick.reason.startswith("truncated:max_tokens"), (
        f"a truncated tick must be labeled as such, got reason={tick.reason!r}")
    assert "no message composed" not in tick.reason
    # the truncation announces itself BY NAME in the log
    assert any("HEARTBEAT_TRUNCATED" in r.getMessage() for r in caplog.records)


def test_truncated_mid_compose_never_sends_partial_text(db, monkeypatch, sms_capture,
                                                        anthropic_stub, caplog):
    """max_tokens with partial text present: the old fallback would SEND the
    cut-off text (the 'glitchy connection' class — a mid-sentence SMS). A
    truncated tick never sends; it logs as truncated."""
    import heartbeat
    from tests._fake_anthropic import Truncated
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: Truncated("so about that midter"))

    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)

    assert sms_capture == [], "cut-off text must never reach the phone"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is False
    assert tick.reason.startswith("truncated:max_tokens")


def test_stay_silent_still_records_chosen_reason_distinctly(db, monkeypatch, sms_capture,
                                                            anthropic_stub, caplog):
    """Genuine chosen silence stays exactly what it was — the model's own reason,
    nothing truncation-flavored — so the two outcomes are distinguishable in the
    DB and the tick log."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "nothing new to add"}))

    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)

    assert sms_capture == []
    tick = _ticks(user.id)[-1]
    assert tick.spoke is False and tick.reason == "nothing new to add"
    assert "truncated" not in tick.reason
    # chosen silence logs its stop_reason too (tool_use), distinct from max_tokens
    assert any("stop=tool_use" in m for m in _tick_records(caplog))


def test_send_text_still_sends_normally(db, monkeypatch, sms_capture, anthropic_stub, caplog):
    """No regression on the speak path — and its stop_reason is visible."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: ToolUse("send_text", {"message": "how'd the midterm go?"}))

    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)

    assert any("midterm" in body for _phone, body in sms_capture)
    tick = _ticks(user.id)[-1]
    assert tick.spoke is True and "midterm" in tick.message
    assert any("stop=tool_use" in m for m in _tick_records(caplog))


# ---- Fix 2: stop_reason observability at the chokepoint ---------------------

def test_stop_reason_in_tokens_log_for_truncated_and_clean(db, monkeypatch, sms_capture,
                                                           anthropic_stub, caplog):
    """The universal fix: the TOKENS instrumentation line (cost_tracking.track,
    the one point every tracked call site already routes through) carries
    stop_reason — so truncation is visible on ANY surface, not just ones with a
    bespoke branch."""
    import heartbeat
    from tests._fake_anthropic import Truncated, ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)

    anthropic_stub.reply_with(lambda kw: Truncated())
    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)
    assert any("stop=max_tokens" in m for m in _tokens_records(caplog))

    caplog.clear()
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "quiet day"}))
    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)
    assert any("stop=tool_use" in m for m in _tokens_records(caplog))


def test_truncated_tick_log_line_distinguishable(db, monkeypatch, sms_capture,
                                                 anthropic_stub, caplog):
    """The HEARTBEAT_TICK line itself must distinguish a truncated tick from a
    chosen-silence tick (the ambiguity that let the Aug 6 bug hide)."""
    import heartbeat
    from tests._fake_anthropic import Truncated
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: Truncated())

    with caplog.at_level(logging.INFO):
        heartbeat.heartbeat_tick(user.id)

    tick_lines = _tick_records(caplog)
    assert tick_lines, "tick must log"
    assert any("stop=max_tokens" in m for m in tick_lines)


# ---- guard against a silent revert of the ceiling ---------------------------

def test_decide_runs_under_raised_dedicated_ceiling(db, monkeypatch, sms_capture,
                                                    anthropic_stub):
    """The decide call uses its OWN config ceiling (not MAX_RESPONSE_TOKENS=400,
    the SMS reply cap), sized like AGENT_LOOP_MAX_TOKENS for the same
    thinking+tools+message workload. Pins both the wiring and the floor."""
    import config, heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "ok"}))

    heartbeat.heartbeat_tick(user.id)

    assert anthropic_stub.calls, "decide must reach the model"
    sent = anthropic_stub.calls[0]["max_tokens"]
    assert sent == config.HEARTBEAT_DECIDE_MAX_TOKENS
    assert sent >= 2000, (
        "decide output budget must fit adaptive thinking + tool JSON + a composed "
        f"message; {sent} is SMS-cap territory (the Aug 6 truncation)")
