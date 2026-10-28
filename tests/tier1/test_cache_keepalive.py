"""
Prompt-cache keep-alive (2026-10-28). The 1-hour prefixes (reply loop 58k tokens,
heartbeat 35k, voice-only 29k) lapsed across 1.5–3h overnight gaps and the next call
rewrote them at 2× — 15 writes = 66% of the day's spend. Each real call now registers
its cached prefix; a 5-minute sweep re-sends exactly that prefix with max_tokens=0
(bills only the cache read, returns nothing) once it's gone 50 minutes untouched,
until 6 hours after the last real call. Live probe: the max_tokens=0 ping read all
29,269 heartbeat-prefix tokens with 0 written, 0 output.
"""

from __future__ import annotations

import types

import pytest

T0 = 1_800_000_000.0
MIN = 60.0
VOICE = {"type": "text", "text": "VOICE " * 400, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
TOOLS = [{"name": "send_text", "description": "d", "input_schema": {"type": "object"}}]
THINK = {"type": "adaptive"}
EFFORT = {"effort": "low"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    import cache_keepalive, config
    cache_keepalive._reset_for_tests()
    monkeypatch.setattr(config, "CACHE_KEEPALIVE_ENABLED", True)
    monkeypatch.setattr(config, "CACHE_KEEPALIVE_REFRESH_MIN", 50)
    monkeypatch.setattr(config, "CACHE_KEEPALIVE_IDLE_HOURS", 6)
    monkeypatch.setattr(config, "CACHE_KEEPALIVE_MAX_ENTRIES", 8)
    yield
    cache_keepalive._reset_for_tests()


def _resp(read=30_000, wrote=0):
    return types.SimpleNamespace(
        stop_reason="max_tokens", content=[],
        usage=types.SimpleNamespace(input_tokens=5, output_tokens=0,
                                    cache_creation_input_tokens=wrote, cache_read_input_tokens=read))


@pytest.fixture
def pings(monkeypatch):
    """Capture pings instead of calling the API; .reply sets what the next ping returns."""
    import cache_keepalive

    class _Sent(list):
        reply = _resp()
    sent = _Sent()

    def fake(entry):
        sent.append(entry)
        if isinstance(sent.reply, Exception):
            raise sent.reply
        return sent.reply
    monkeypatch.setattr(cache_keepalive, "_ping", fake)
    monkeypatch.setattr("cost_tracking.track", lambda *a, **k: None)
    return sent


def _note(tail="ctx", tools=TOOLS, now=T0, site="heartbeat.decide", system=None):
    from cache_keepalive import note
    system = system if system is not None else [VOICE, {"type": "text", "text": tail}]
    note(site, model="claude-opus-5-5", system=system, tools=tools,
         thinking=THINK, output_config=EFFORT, now=now)


# ---- what registers ----------------------------------------------------------------

def test_only_one_hour_cached_prefixes_register():
    import cache_keepalive
    from cache_keepalive import note
    note("x", model="m", system="a plain string system", now=T0)
    note("x", model="m", system=[{"type": "text", "text": "no breakpoint"}], now=T0)
    note("x", model="m", system=[{"type": "text", "text": "v", "cache_control": {"type": "ephemeral"}}], now=T0)
    assert cache_keepalive._entries == {}, "string / uncached / 5-minute prefixes are not kept alive"
    _note()
    assert len(cache_keepalive._entries) == 1


def test_per_call_tail_shares_one_entry_but_tools_split_it():
    import cache_keepalive
    _note(tail="user 40 context")
    _note(tail="user 77 context")
    assert len(cache_keepalive._entries) == 1, "the uncached per-user tail is not part of the prefix"
    _note(tools=TOOLS + [{"name": "react", "description": "d", "input_schema": {"type": "object"}}])
    assert len(cache_keepalive._entries) == 2, "a different tool list is a different cached prefix"


def test_disabled_flag_is_a_full_no_op(monkeypatch, pings):
    import cache_keepalive, config
    monkeypatch.setattr(config, "CACHE_KEEPALIVE_ENABLED", False)
    _note()
    assert cache_keepalive._entries == {}
    assert cache_keepalive.run_due(now=T0 + 55 * MIN)["pinged"] == 0
    assert pings == []


# ---- when it pings -------------------------------------------------------------------

def test_pings_at_fifty_minutes_not_before(pings):
    from cache_keepalive import run_due
    _note()
    assert run_due(now=T0 + 49 * MIN)["pinged"] == 0
    assert run_due(now=T0 + 50 * MIN)["pinged"] == 1
    entry = pings[0]
    assert entry["system"] == [VOICE], "the ping sends only the cached prefix, never a user's context"
    assert entry["tools"] == TOOLS and entry["thinking"] == THINK and entry["output_config"] == EFFORT


def test_a_ping_restarts_the_clock_and_chains_until_idle(pings):
    from cache_keepalive import run_due
    _note()
    run_due(now=T0 + 50 * MIN)
    assert run_due(now=T0 + 60 * MIN)["pinged"] == 0, "just refreshed"
    assert run_due(now=T0 + 100 * MIN)["pinged"] == 1
    # 6h after the last REAL call it stops, however many pings it has had
    out = run_due(now=T0 + 6 * 60 * MIN + 1)
    assert out["dropped_idle"] == 1 and out["pinged"] == 0
    assert run_due(now=T0 + 7 * 60 * MIN)["pinged"] == 0


def test_a_real_call_resets_both_clocks(pings):
    import cache_keepalive
    from cache_keepalive import run_due
    _note(now=T0)
    _note(now=T0 + 40 * MIN)
    assert run_due(now=T0 + 55 * MIN)["pinged"] == 0, "the real call at 40m refreshed the entry"
    assert run_due(now=T0 + 90 * MIN)["pinged"] == 1
    run_due(now=T0 + 6 * 60 * MIN + 1)
    assert len(cache_keepalive._entries) == 1, "idle counts from the LAST real call (40m), not the first"


def test_a_ping_that_reads_nothing_drops_the_prefix(pings, caplog):
    import logging
    import cache_keepalive
    from cache_keepalive import run_due
    _note()
    pings.reply = _resp(read=0, wrote=30_000)
    with caplog.at_level(logging.WARNING):
        out = run_due(now=T0 + 50 * MIN)
    assert out["dropped_miss"] == 1 and out["pinged"] == 0
    assert cache_keepalive._entries == {}, "never pay a write every 50 minutes for a drifted prefix"
    assert any("CACHE_KEEPALIVE_MISS" in r.getMessage() for r in caplog.records)


def test_a_failed_ping_keeps_the_entry_for_the_next_sweep(pings):
    import cache_keepalive
    from cache_keepalive import run_due
    _note()
    pings.reply = RuntimeError("overloaded")
    assert run_due(now=T0 + 50 * MIN)["failed"] == 1
    assert len(cache_keepalive._entries) == 1
    pings.reply = _resp()
    assert run_due(now=T0 + 55 * MIN)["pinged"] == 1, "retried 5 minutes later, still inside the hour"


def test_entry_cap_evicts_the_longest_idle(monkeypatch):
    import cache_keepalive, config
    monkeypatch.setattr(config, "CACHE_KEEPALIVE_MAX_ENTRIES", 2)
    _note(tools=[{"name": "a"}], now=T0)
    _note(tools=[{"name": "b"}], now=T0 + 1)
    _note(tools=[{"name": "c"}], now=T0 + 2)
    assert sorted(e["tools"][0]["name"] for e in cache_keepalive._entries.values()) == ["b", "c"]


def test_ping_request_is_a_max_tokens_zero_prefill(anthropic_stub):
    """The real _ping: max_tokens=0 (no output billed), placeholder user turn, and the
    exact prefix/tools/thinking/effort — through the SDK's Messages.create."""
    from cache_keepalive import _ping
    _note()
    import cache_keepalive
    entry = next(iter(cache_keepalive._entries.values()))
    _ping(entry)
    kw = anthropic_stub.calls[-1]
    assert kw["max_tokens"] == 0
    assert kw["system"] == [VOICE] and kw["tools"] == TOOLS
    assert kw["thinking"] == THINK and kw["output_config"] == EFFORT
    assert kw["messages"] == [{"role": "user", "content": "keepalive"}]


# ---- the sites register the bytes they actually send ---------------------------------

def _prefix_of(kw):
    from cache_keepalive import _cached_prefix
    return _cached_prefix(kw["system"])


def _replayed(anthropic_stub):
    """Run the sweep 51 minutes on (through the stub) and return the ping kwargs."""
    import time
    from cache_keepalive import run_due
    before = len(anthropic_stub.calls)
    run_due(now=time.time() + 51 * MIN)
    pings = [kw for kw in anthropic_stub.calls[before:] if kw.get("max_tokens") == 0]
    assert pings, "the sweep must have pinged the registered prefix"
    return pings


def _assert_replays(real, ping):
    assert ping["model"] == real["model"]
    assert ping["system"] == _prefix_of(real), "byte-identical cached prefix"
    assert ping.get("tools") == real.get("tools"), "byte-identical tool list"
    assert ping.get("thinking") == real.get("thinking")
    assert ping.get("output_config") == real.get("output_config")


def test_heartbeat_registers_its_exact_prefix(db, monkeypatch, anthropic_stub):
    import config, heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])
    anthropic_stub.reply_with(lambda kw: None if kw.get("max_tokens") == 0
                              else ToolUse("stay_silent", {"reason": "x"}))
    heartbeat.decide(user.id)
    real = next(kw for kw in anthropic_stub.calls if kw.get("max_tokens") != 0)
    _assert_replays(real, _replayed(anthropic_stub)[0])


def test_reminder_compose_registers_its_exact_prefix(db, anthropic_stub):
    import reminders
    from models import Reminder
    from tests.factories import make_user
    user = make_user(db, name="Alex")
    anthropic_stub.reply_with(lambda kw: None if kw.get("max_tokens") == 0 else "water time")
    reminders._compose(user, Reminder(user_id=user.id, text="drink water", every_hours=2))
    real = next(kw for kw in anthropic_stub.calls if kw.get("max_tokens") != 0)
    ping = _replayed(anthropic_stub)[0]
    _assert_replays(real, ping)
    assert "Alex" not in str(ping["system"]), "a user's name never rides the ping"


def test_reply_loop_registers_its_exact_prefix(db, anthropic_stub):
    from agent_loop import run_agent_loop
    from tests.factories import make_user
    user = make_user(db)
    anthropic_stub.reply_with(lambda kw: None if kw.get("max_tokens") == 0 else "ok")
    run_agent_loop(user, "hey", "freeform")
    real = next(kw for kw in anthropic_stub.calls if kw.get("max_tokens") != 0)
    _assert_replays(real, _replayed(anthropic_stub)[0])


def test_scheduler_registers_the_sweep():
    import pathlib
    src = pathlib.Path("scheduler.py").read_text()
    assert 'id="cache_keepalive"' in src and "cache_keepalive import run_due" in src
