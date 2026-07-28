"""
Phase 4 — heartbeat. Guardrails run in code before any model call (a violating
tick never reaches the model); the decision call answers speak-or-silent with a
stay_silent tool; every tick is logged, and the recent ticks + today's outbound
feed the next tick so the coach can't re-send the same nudge.

These are deterministic: the model is stubbed. The *quality* of speak/silence is
graded live in tier-2. Here we prove the machinery — guardrails block, the two
decision branches route correctly, and the anti-repetition signal reaches context.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _utcnow_naive():
    """Naive UTC — how prod (UTC server) stores timestamps, and what the code's
    `.replace(tzinfo=utc)` convention expects. The disposable PG runs in the local
    TZ, so we seed explicit naive-UTC values instead of the aware-default column."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _allow(monkeypatch, user):
    """Put this user's phone on the allowlist so allowlist/other guardrails pass."""
    import config
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])


def _ticks(user_id):
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        return (s.query(HeartbeatTick)
                .filter(HeartbeatTick.user_id == user_id)
                .order_by(HeartbeatTick.id).all())
    finally:
        s.close()


# ---- guardrails: each blocks in code, no model call, no SMS -----------------

def test_guardrail_not_allowlisted_blocks(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", ["+15550009999"])  # someone else
    user = make_user(db)

    heartbeat.heartbeat_tick(user.id)

    ticks = _ticks(user.id)
    assert len(ticks) == 1 and ticks[0].spoke is False
    assert ticks[0].reason == "guardrail:not_allowlisted"
    assert sms_capture == []
    assert anthropic_stub.calls == [], "guardrail must block BEFORE any model call"


def test_guardrail_quiet_hours_blocks(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from tests.factories import make_user

    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)
    user = make_user(db, quiet_until=future)
    _allow(monkeypatch, user)

    heartbeat.heartbeat_tick(user.id)

    assert _ticks(user.id)[0].reason == "guardrail:quiet_hours"
    assert anthropic_stub.calls == []


def test_guardrail_daily_budget_blocks(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from models import get_session, HeartbeatTick
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    monkeypatch.setattr(config, "HEARTBEAT_MAX_PER_DAY", 2)

    s = get_session()
    try:
        for _ in range(2):
            s.add(HeartbeatTick(user_id=user.id, spoke=True, reason="spoke", message="hi"))
        s.commit()
    finally:
        s.close()

    heartbeat.heartbeat_tick(user.id)

    assert _ticks(user.id)[-1].reason == "guardrail:daily_budget"
    assert anthropic_stub.calls == []


def test_guardrail_active_conversation_blocks(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, Message
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)

    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="just texted you",
                     created_at=_utcnow_naive()))
        s.commit()
    finally:
        s.close()

    heartbeat.heartbeat_tick(user.id)

    assert _ticks(user.id)[-1].reason == "guardrail:active_conversation"
    assert anthropic_stub.calls == []


def test_guardrail_unanswered_outbound_blocks(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat, engagement_tracker
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    monkeypatch.setattr(engagement_tracker, "has_unanswered_outbound", lambda _uid: True)

    heartbeat.heartbeat_tick(user.id)

    assert _ticks(user.id)[-1].reason == "guardrail:unanswered_gap"
    assert anthropic_stub.calls == []


# ---- floor events hard-gate; model events inform (deterministic guardrails) --

def test_floor_in_class_event_hard_gates(db, monkeypatch, sms_capture, anthropic_stub):
    """A regex-floor in_class event (high precision) suppresses the tick in code —
    don't ping someone provably mid-lecture — before any model call."""
    import heartbeat
    from events import record_event
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    future = _utcnow_naive() + timedelta(hours=1)
    record_event(user.id, "in_class", ends_at=future, source="regex", raw_text="in class till 2")

    heartbeat.heartbeat_tick(user.id)

    assert _ticks(user.id)[-1].reason == "guardrail:in_class"
    assert anthropic_stub.calls == [], "floor event must hard-gate BEFORE the model call"


def test_model_scheduled_event_informs_but_does_not_hard_gate(db, monkeypatch, sms_capture, anthropic_stub):
    """A MODEL-logged event ('summit') must NOT hard-gate — it reaches the decision
    call and informs it via context. Deterministic guardrails, model decisions."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from models import get_session, User

    user = make_user(db)
    _allow(monkeypatch, user)
    handle_log_event(user.id, {"description": "founder summit", "starts_at": "12:00",
                               "ends_at": "14:30", "date": "today"})
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "nothing to add"}))

    heartbeat.heartbeat_tick(user.id)

    # reached the decision (model was called) rather than being hard-gated
    assert anthropic_stub.calls, "a model event must not hard-gate the tick"
    tick = _ticks(user.id)[-1]
    assert not (tick.reason or "").startswith("guardrail:")
    # and it INFORMS: the event is in the decision context
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "founder summit" in ctx


# ---- decision branches ------------------------------------------------------

def test_decide_speaks_sends_and_logs(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: "how'd the midterm go?")

    heartbeat.heartbeat_tick(user.id)

    assert any("midterm" in body for _phone, body in sms_capture), "speaking tick must send an SMS"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is True and "midterm" in tick.message


def test_decide_stay_silent_tool_logs_reason_no_send(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(
        lambda kw: ToolUse("stay_silent", {"reason": "nothing new to add"}))

    heartbeat.heartbeat_tick(user.id)

    assert sms_capture == [], "stay_silent must not send"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is False and tick.reason == "nothing new to add"


# ---- anti-repetition: prior ticks + today's outbound reach the context ------

def test_proactive_context_carries_tick_history_and_outbound(db, monkeypatch, anthropic_stub):
    import config, heartbeat
    from models import get_session, HeartbeatTick, Message
    from tests.factories import make_user

    user = make_user(db)
    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=user.id, spoke=True, reason="spoke",
                            message="you've skipped legs twice this week"))
        s.add(Message(user_id=user.id, direction="out", body="how's the cut going?",
                     created_at=_utcnow_naive()))
        s.commit()
    finally:
        s.close()

    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        captured["tools"] = kw.get("tools")
        return "..."

    anthropic_stub.reply_with(handler)
    heartbeat.decide(user.id)

    sys_text = "".join(b["text"] for b in captured["system"])
    assert "skipped legs twice" in sys_text, "prior spoken nudge must feed the next tick"
    assert "how's the cut going?" in sys_text, "today's outbound must be visible"
    assert "TICK HISTORY" in sys_text and "do NOT repeat" in sys_text
    assert any(t.get("name") == "stay_silent" for t in captured["tools"])
    # HEARTBEAT_WEB_SEARCH is a deliberate burn-in decision (post-burn-in item 4):
    # default OFF, an unbounded cost multiplier on an unmeasured speak rate.
    if config.HEARTBEAT_WEB_SEARCH:
        assert any(t.get("type", "").startswith("web_search") for t in captured["tools"])


def test_heartbeat_web_search_off_by_default_excludes_tool(db, monkeypatch, anthropic_stub):
    """Post-burn-in item 4: HEARTBEAT_WEB_SEARCH defaults False for burn-in — the
    proactive tool set must carry no web_search tool unless explicitly turned on."""
    import config, heartbeat
    from tests.factories import make_user

    assert config.HEARTBEAT_WEB_SEARCH is False, "default must be off for burn-in"
    user = make_user(db)
    captured = {}
    anthropic_stub.reply_with(lambda kw: captured.setdefault("tools", kw.get("tools")) or "...")

    heartbeat.decide(user.id)

    assert not any(t.get("name") == "web_search" or t.get("type", "").startswith("web_search")
                   for t in captured["tools"]), captured["tools"]


def test_proactive_context_has_now_anchor_totals_and_events(db, monkeypatch, anthropic_stub):
    """Post-burn-in item 4 regression: the burn-in fixes (local 'now' anchor,
    code-computed totals, today's events) must reach the heartbeat's decision
    context, not just the reactive loop's — that's the precondition for a
    well-timed proactive nudge."""
    import heartbeat
    from tests.factories import make_user
    from models import get_session, User, Meal, Event
    from datetime import datetime, timezone

    user = make_user(db, calorie_target=2000, protein_target=150)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="chicken bowl", calories=600, protein_g=50,
                   eaten_at=now, source="text", log_type="user_reported"))
        s.add(Event(user_id=user.id, event_type="scheduled", raw_text="founder summit",
                    occurred_at=now, source="model"))
        s.commit()
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "Right now:" in ctx, "no local 'now' anchor in proactive context"
    assert "TODAY'S TOTALS" in ctx, "no code-computed totals block in proactive context"
    assert "600" in ctx, "meal total not reflected in proactive context"
    assert "founder summit" in ctx, "today's event not reflected in proactive context"


# ---- heartbeat_all: flag gate + allowlist filter ----------------------------

def test_heartbeat_all_disabled_is_noop(db, monkeypatch, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", False)

    heartbeat.heartbeat_all()

    assert anthropic_stub.calls == []


def test_heartbeat_all_ticks_only_allowlisted(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    on = make_user(db)
    make_user(db)  # off the allowlist
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [on.phone])
    anthropic_stub.reply_with(lambda kw: "quick check-in")

    heartbeat.heartbeat_all()

    # exactly one user ticked (the allowlisted one); the other never reached a tick
    all_ticks = _ticks(on.id)
    assert len(all_ticks) == 1 and all_ticks[0].spoke is True
    assert all(phone == on.phone for phone, _ in sms_capture)
