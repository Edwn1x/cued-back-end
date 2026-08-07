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


def _seed_msg(user_id, direction, minutes_ago, message_type="freeform", body="."):
    """Seed one Message `minutes_ago` in the past (naive-UTC, prod's convention)."""
    from models import get_session, Message
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction=direction, body=body,
                      message_type=message_type, created_at=when))
        s.commit()
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


def test_guardrail_proactive_stack_blocks(db, monkeypatch, sms_capture, anthropic_stub):
    """Anti-stack: a proactive (heartbeat) nudge sent recently and still unanswered
    suppresses a SECOND proactive nudge within the window — don't pile on."""
    import config, heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    # an unanswered heartbeat outbound 10 min ago, well inside the stack window
    _seed_msg(user.id, "out", minutes_ago=10, message_type="heartbeat")

    heartbeat.heartbeat_tick(user.id)

    assert _ticks(user.id)[-1].reason == "guardrail:proactive_stack"
    assert anthropic_stub.calls == [], "anti-stack must block BEFORE any model call"
    assert sms_capture == []


def test_gap_clears_after_window_without_user_reply(db, monkeypatch, sms_capture, anthropic_stub):
    """Deadlock regression + reachable clear condition: a single unanswered proactive
    nudge, once the stack window has elapsed, must NOT suppress the next tick — even
    though the user never replied. The gap clears on time, not only on user input."""
    import config, heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    window = config.HEARTBEAT_STACK_WINDOW_MINUTES
    # unanswered heartbeat outbound sent JUST beyond the window; no inbound ever
    _seed_msg(user.id, "out", minutes_ago=window + 10, message_type="heartbeat")
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "nothing new"}))

    heartbeat.heartbeat_tick(user.id)

    assert anthropic_stub.calls, "elapsed-window gap must let the tick reach the model"
    reason = _ticks(user.id)[-1].reason
    assert reason != "guardrail:proactive_stack"
    assert not reason.startswith("guardrail:"), reason


def test_reactive_reply_does_not_wedge_heartbeat(db, monkeypatch, sms_capture, anthropic_stub):
    """The core deadlock: the coach's own REACTIVE reply left an 'unanswered
    outbound' that muted all proactive initiation. A reactive reply (non-heartbeat
    type), even recent and unanswered, must not suppress the heartbeat."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    # user texted 40 min ago (past active-convo window), coach replied 39 min ago;
    # that reply is the most recent outbound and is unanswered — old gate deadlocked here
    _seed_msg(user.id, "in", minutes_ago=40)
    _seed_msg(user.id, "out", minutes_ago=39, message_type="freeform")
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "quiet"}))

    heartbeat.heartbeat_tick(user.id)

    assert anthropic_stub.calls, "a reactive reply must not wedge the heartbeat silent"
    assert not (_ticks(user.id)[-1].reason or "").startswith("guardrail:")


def test_legacy_briefing_does_not_wedge_heartbeat(db, monkeypatch, sms_capture, anthropic_stub):
    """Item 1 point 4 / spec test 3: a legacy morning_briefing outbound (unanswered)
    must not by itself suppress the heartbeat — it isn't a proactive-stack candidate."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _seed_msg(user.id, "out", minutes_ago=40, message_type="morning_briefing")
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "quiet"}))

    heartbeat.heartbeat_tick(user.id)

    assert anthropic_stub.calls, "a legacy briefing must not wedge the heartbeat silent"
    assert not (_ticks(user.id)[-1].reason or "").startswith("guardrail:")


# ---- unit: has_unanswered_proactive (the anti-stack gate) --------------------

def test_has_unanswered_proactive_true_within_window(db):
    from engagement_tracker import has_unanswered_proactive
    from tests.factories import make_user

    user = make_user(db)
    _seed_msg(user.id, "out", minutes_ago=10, message_type="heartbeat")
    assert has_unanswered_proactive(user.id, 180) is True


def test_has_unanswered_proactive_false_past_window(db):
    from engagement_tracker import has_unanswered_proactive
    from tests.factories import make_user

    user = make_user(db)
    _seed_msg(user.id, "out", minutes_ago=200, message_type="heartbeat")
    assert has_unanswered_proactive(user.id, 180) is False


def test_has_unanswered_proactive_false_when_answered(db):
    from engagement_tracker import has_unanswered_proactive
    from tests.factories import make_user

    user = make_user(db)
    _seed_msg(user.id, "out", minutes_ago=10, message_type="heartbeat")
    _seed_msg(user.id, "in", minutes_ago=5)  # user replied after the nudge
    assert has_unanswered_proactive(user.id, 180) is False


def test_has_unanswered_proactive_false_for_reactive_outbound(db):
    from engagement_tracker import has_unanswered_proactive
    from tests.factories import make_user

    user = make_user(db)
    _seed_msg(user.id, "out", minutes_ago=10, message_type="freeform")  # not proactive
    assert has_unanswered_proactive(user.id, 180) is False


def test_has_unanswered_proactive_false_when_no_outbound(db):
    from engagement_tracker import has_unanswered_proactive
    from tests.factories import make_user

    user = make_user(db)
    assert has_unanswered_proactive(user.id, 180) is False


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

def test_decide_speaks_via_send_text_tool(db, monkeypatch, sms_capture, anthropic_stub):
    """PRIMARY speak path: speaking is an explicit send_text tool call, not bare text.
    Burn-in finding — with only a stay_silent tool offered, a model that HAD decided to
    speak still reflexively called stay_silent ('actually should speak but tool forces
    silence'), because 'emit text' fought its tool-use prior. send_text is the fix: the
    model routes a speak decision through its own channel. This pins that path."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: ToolUse("send_text", {"message": "how'd the midterm go?"}))

    heartbeat.heartbeat_tick(user.id)

    assert any("midterm" in body for _phone, body in sms_capture), "send_text must send an SMS"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is True and "midterm" in tick.message


def test_decide_speaks_bare_text_fallback_still_sends(db, monkeypatch, sms_capture, anthropic_stub):
    """Fallback path: if the model ends with bare text instead of calling send_text, the
    tick must still speak (robustness — kept from before the two-tool change)."""
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: "how'd the midterm go?")

    heartbeat.heartbeat_tick(user.id)

    assert any("midterm" in body for _phone, body in sms_capture), "bare-text fallback must send an SMS"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is True and "midterm" in tick.message


def test_decide_sends_all_text_blocks_not_just_first(db, monkeypatch, sms_capture, anthropic_stub):
    """Seam regression (same as agent_loop's _join_text burn-in fix): with adaptive
    thinking + inline web_search the model splits the nudge across several text blocks
    — a lead-in, then the tool result, then the substance. The tick must send EVERY
    text block concatenated, not just the first (which dropped the actual message)."""
    import heartbeat
    from tests._fake_anthropic import MultiText
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: MultiText(
        "checked the RSF hours: ",
        ("web_search_tool_result", ""),
        "open till 1am, you've still got time to hit legs tonight"))

    heartbeat.heartbeat_tick(user.id)

    assert len(sms_capture) == 1
    _phone, body = sms_capture[0]
    assert body == "checked the RSF hours: open till 1am, you've still got time to hit legs tonight"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is True and "hit legs tonight" in tick.message


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
    # Addendum: HEARTBEAT_WEB_SEARCH defaults ON with a per-day budget; a fresh
    # user is under budget, so the tool must be in the set here.
    assert any(t.get("type", "").startswith("web_search") for t in captured["tools"])


# ---- calibration (PR2): empty-history renders PERMISSION, not a void --------

def test_proactive_context_empty_history_renders_permission(db, monkeypatch, anthropic_stub):
    """Calibration bias #1: a fresh user (no outbound today, no ticks) must see an
    explicit PROACTIVE STATUS block granting PERMISSION to speak — NOT an empty void
    the model reads as 'no proof this isn't a duplicate → stay cautious'. Red before
    2a: previously both blocks were simply omitted when empty, rendering nothing."""
    import heartbeat
    from models import get_session, User
    from tests.factories import make_user

    user = make_user(db)
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "PROACTIVE STATUS" in ctx, "empty history must render an explicit status block"
    assert "PERMISSION" in ctx, "the empty-history block must grant permission, not caution"
    # and it must NOT masquerade as history the model could fear repeating
    assert "RECENT PROACTIVE MESSAGES" not in ctx
    assert "TICK HISTORY" not in ctx


def test_proactive_context_with_history_suppresses_permission_block(db, monkeypatch, anthropic_stub):
    """The other branch: once there IS a proactive message today, the anti-repetition
    blocks appear and the empty-case PERMISSION line must NOT — a user who was already
    nudged is not a clean slate. Pins both branches so neither can silently vanish."""
    import heartbeat
    from models import get_session, User, Message
    from tests.factories import make_user

    user = make_user(db)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="how's the cut going?",
                      created_at=_utcnow_naive()))
        s.commit()
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "RECENT PROACTIVE MESSAGES" in ctx and "how's the cut going?" in ctx
    assert "PROACTIVE STATUS" not in ctx, "a user already nudged today is not a clean slate"
    assert "PERMISSION" not in ctx


# ---- inbound age: the mid-conversation judgment needs THEIR timestamp -------

def test_proactive_context_carries_inbound_age(db, monkeypatch, anthropic_stub):
    """The model can't date the transcript's last user message; without a code-computed
    inbound age it reads a history ending on the coach's own question as a live exchange
    and re-cites 'waiting on reply' for hours (the model-layer unanswered-gap deadlock,
    seen live Aug 6-7: five straight 'mid conversation' silent ticks with the last
    inbound 1-4 hours old). The context must state how old THEIR last message is."""
    import heartbeat
    from models import get_session, User, Message
    from tests.factories import make_user

    user = make_user(db)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="what should i cook tonight?",
                      created_at=_utcnow_naive() - timedelta(hours=5)))
        s.commit()
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "TIME SINCE THEIR LAST MESSAGE" in ctx
    assert "~5.0 hours" in ctx, "the inbound age must be code-computed, not left to the model"


def test_proactive_context_no_inbound_age_when_no_inbound(db, monkeypatch, anthropic_stub):
    """Clean-absent: a user who has never texted gets no inbound-age block (no
    placeholder for the model to reason about)."""
    import heartbeat
    from models import get_session, User
    from tests.factories import make_user

    user = make_user(db)
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "TIME SINCE THEIR LAST MESSAGE" not in ctx


# ---- warmth (Part 2): code-computed win/momentum signal ---------------------

def _seed_workout(user_id, days_ago, wtype="push", completed=True):
    from models import get_session, Workout
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    s = get_session()
    try:
        s.add(Workout(user_id=user_id, workout_type=wtype, completed=completed, date=when))
        s.commit()
    finally:
        s.close()


def test_proactive_context_surfaces_momentum_win(db, monkeypatch, anthropic_stub):
    """Warmth material the model can't compute itself: a code-counted '7-day completed
    workouts' figure (the playbook's precompute-what-you-ask-for — date arithmetic is
    code's job). 5 sessions in the last 7 days must surface as a MOMENTUM block with the
    count, so the model has a genuine win to reach for. Red before 2a (no such block)."""
    import heartbeat
    from models import get_session, User
    from tests.factories import make_user

    user = make_user(db, name="Sam")
    for d, t in ((0, "push"), (1, "pull"), (2, "legs"), (4, "upper"), (6, "push")):
        _seed_workout(user.id, d, t)

    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "MOMENTUM" in ctx, "a real 7-day training streak must surface as warm material"
    assert "5" in ctx, "the code-computed 7-day workout count must be present"


def test_proactive_context_no_momentum_when_no_recent_training(db, monkeypatch, anthropic_stub):
    """Clean-absent case (spec tier-1 #1): a user with NO recent completed training gets
    NO momentum block — no crash, no placeholder, no participation-trophy substrate. An
    OLD workout (10 days ago) is outside the 7-day window and must not count."""
    import heartbeat
    from models import get_session, User
    from tests.factories import make_user

    user = make_user(db, name="Sam")
    _seed_workout(user.id, 10, "push")            # outside the 7-day window
    _seed_workout(user.id, 2, "pull", completed=False)  # not completed — not a win

    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "MOMENTUM" not in ctx, "no recent completed training must not fabricate momentum"


# ---- addendum: web search on-by-default, budgeted in code -------------------

def _has_search(tools):
    return any(t.get("name") == "web_search" or t.get("type", "").startswith("web_search")
               for t in tools)


def _capture_tools(anthropic_stub, reply="..."):
    """Handler that records the tool set of the FIRST call and replies `reply`."""
    captured = {}

    def handler(kw):
        captured.setdefault("tools", kw.get("tools"))
        return reply

    anthropic_stub.reply_with(handler)
    return captured


def _seed_tick(user_id, *, decided_at=None, search_available=False, search_used=False,
               search_query=None, spoke=False, reason="silent"):
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=user_id, spoke=spoke, reason=reason,
                            decided_at=decided_at or _utcnow_naive(),
                            search_available=search_available, search_used=search_used,
                            search_query=search_query))
        s.commit()
    finally:
        s.close()


def test_heartbeat_web_search_on_by_default_under_budget(db, monkeypatch, anthropic_stub):
    """Addendum §1: proactive search is ON by default (budgeted). A user with no
    searched ticks today is under budget — the tool set must include web_search."""
    import config, heartbeat
    from tests.factories import make_user

    assert config.HEARTBEAT_WEB_SEARCH is True, "addendum: default must be on (budgeted)"
    user = make_user(db)
    captured = _capture_tools(anthropic_stub)

    heartbeat.decide(user.id)

    assert _has_search(captured["tools"]), captured["tools"]


def test_search_at_budget_omits_tool_but_tick_still_speaks(db, monkeypatch, sms_capture, anthropic_stub):
    """Addendum §2: at/over budget the tool is omitted and the tick STILL runs and
    can still speak — search scarcity must never suppress a message."""
    import config, heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    monkeypatch.setattr(config, "HEARTBEAT_SEARCH_MAX_PER_DAY", 3)
    for _ in range(3):
        _seed_tick(user.id, search_available=True, search_used=True, search_query="q")
    captured = _capture_tools(anthropic_stub, reply="gym's calling your name")

    heartbeat.heartbeat_tick(user.id)

    assert not _has_search(captured["tools"]), captured["tools"]
    assert any("gym" in body for _p, body in sms_capture), \
        "over-budget tick must still run the decision and be able to speak"
    tick = _ticks(user.id)[-1]
    assert tick.spoke is True and tick.search_available is False


def test_budget_counts_searched_ticks_not_offered(db, monkeypatch, anthropic_stub):
    """Addendum §2: an offered-but-unused search tool costs nothing — three
    search-available ticks with no invocation leave search available on the 4th."""
    import config, heartbeat
    from tests.factories import make_user

    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_SEARCH_MAX_PER_DAY", 3)
    for _ in range(3):
        _seed_tick(user.id, search_available=True, search_used=False)
    captured = _capture_tools(anthropic_stub)

    heartbeat.decide(user.id)

    assert _has_search(captured["tools"]), \
        "budget must count searched ticks (spend), not offered ticks"


def test_search_decision_fields_recorded_on_searched_tick(db, monkeypatch, sms_capture, anthropic_stub):
    """Addendum §3: the tick records search_available / search_used / search_query —
    the instrumentation behind 'used because needed, or used because present?'"""
    import heartbeat
    from tests._fake_anthropic import WebSearchUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(
        lambda kw: WebSearchUse("rsf hours tonight", "rsf's open till 1am — you've got time"))

    heartbeat.heartbeat_tick(user.id)

    tick = _ticks(user.id)[-1]
    assert tick.spoke is True
    assert tick.search_available is True
    assert tick.search_used is True
    assert tick.search_query == "rsf hours tonight"


def test_search_decision_fields_recorded_on_unsearched_tick(db, monkeypatch, sms_capture, anthropic_stub):
    """Offered but not used: available=True, used=False, no query — including on a
    silent tick (the stay_silent path must not lose the instrumentation)."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    anthropic_stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": "nothing to add"}))

    heartbeat.heartbeat_tick(user.id)

    tick = _ticks(user.id)[-1]
    assert tick.spoke is False
    assert tick.search_available is True
    assert tick.search_used is False
    assert tick.search_query is None


def test_search_budget_window_is_user_local_day(db, monkeypatch, anthropic_stub):
    """Addendum §2 + timestamp-fix regression: a searched tick at 11pm LOCAL
    yesterday lands in today's UTC day (11pm PDT = 6am UTC) — it must count toward
    YESTERDAY's local budget, leaving today's budget untouched."""
    import config, heartbeat
    from zoneinfo import ZoneInfo
    from tests.factories import make_user

    user = make_user(db)  # factory default tz: America/Los_Angeles
    monkeypatch.setattr(config, "HEARTBEAT_SEARCH_MAX_PER_DAY", 1)
    tz = ZoneInfo("America/Los_Angeles")
    yesterday_11pm_local = (datetime.now(tz) - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0)
    decided = yesterday_11pm_local.astimezone(timezone.utc).replace(tzinfo=None)
    _seed_tick(user.id, decided_at=decided, search_available=True, search_used=True,
               search_query="late gym hours")
    captured = _capture_tools(anthropic_stub)

    heartbeat.decide(user.id)

    assert _has_search(captured["tools"]), \
        "a UTC-day window would double-count yesterday's 11pm-local searched tick"


def test_heartbeat_web_search_false_kill_switch_survives_budget(db, monkeypatch, anthropic_stub):
    """Addendum §tests-6: HEARTBEAT_WEB_SEARCH=false fully disables the tool
    regardless of budget headroom — the kill switch survives the budget layer."""
    import config, heartbeat
    from tests.factories import make_user

    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_WEB_SEARCH", False)
    captured = _capture_tools(anthropic_stub)

    heartbeat.decide(user.id)

    assert not _has_search(captured["tools"]), captured["tools"]
    tick_fields = _ticks(user.id)  # decide() alone logs nothing — no row expected
    assert tick_fields == []


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
