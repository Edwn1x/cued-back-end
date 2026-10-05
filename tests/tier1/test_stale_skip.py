"""
Stale-tick skip (stale_skip.py): a code rule between the guardrails and the Opus
decision call. Skip only when the state fingerprint equals the last REAL (silent)
decision's, nothing landed since, no code gate or spoken tick in between, under the
floor, and no model recheck is due. Shadow mode records the verdict and still runs
the model; enabled mode skips the call. These prove the machinery deterministically;
the live false-negative rate is read off HEARTBEAT_STALE_SHADOW_MISS in prod.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _allow(monkeypatch, user):
    import config
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])


def _ticks(user_id):
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        return (s.query(HeartbeatTick).filter(HeartbeatTick.user_id == user_id)
                .order_by(HeartbeatTick.id).all())
    finally:
        s.close()


def _silent(stub, reason="nothing new to add", **extra):
    from tests._fake_anthropic import ToolUse
    stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": reason, **extra}))


def _speak(stub, message="u eat yet"):
    from tests._fake_anthropic import ToolUse
    stub.reply_with(lambda kw: ToolUse("send_text", {"message": message}))


def _backdate_tick(tick_id, hours):
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        t = s.get(HeartbeatTick, tick_id)
        t.decided_at = t.decided_at - timedelta(hours=hours)
        s.commit()
    finally:
        s.close()


def _landed(user_id, since):
    """What new_rows_since reports for this user since `since` (the detail string a
    verdict carries may say 'state changed' first when the row also altered context)."""
    from models import get_session, User
    from stale_skip import new_rows_since
    s = get_session()
    try:
        return new_rows_since(s.get(User, user_id), s, since, _utcnow_naive())
    finally:
        s.close()


def _verdict(user_id):
    """The rule's verdict for a fresh tick RIGHT NOW (no model, no tick row)."""
    import heartbeat
    from models import get_session, User
    from stale_skip import evaluate
    s = get_session()
    try:
        u = s.get(User, user_id)
        ctx = heartbeat._proactive_context(u, s)
        return evaluate(u, s, ctx)
    finally:
        s.close()


# ---- pure: normalize + bands -------------------------------------------------

def test_normalize_drops_volatile_blocks_and_collapses_fine_time():
    from stale_skip import normalize
    ctx = ("## MEMORY\nlikes beef\n\n"
           "## NOW\nMonday 2026-10-05 11:28 PT\n\n"
           "## MEAL GAP\nLast logged meal was 6.5h ago (eggs). It's 11:28, ~45 min after their 10:43 wake.\n\n"
           "## TIME SINCE YOUR LAST MESSAGE\n~3.2 hours\n\n"
           "## TIME SINCE THEIR LAST MESSAGE\n~7.9 hours — judge by this\n\n"
           "## DEADLINE RADAR\n- HW6 (in 37h); 5 days since their last lift\n\n"
           "## TICK HISTORY (your recent proactive decisions)\n- silent: x\n\n"
           "## PROACTIVE STATUS\nno history\n")
    out = normalize(ctx)
    for gone in ("## NOW", "TIME SINCE YOUR", "TIME SINCE THEIR", "TICK HISTORY", "PROACTIVE STATUS", "11:28", "6.5h", "37h", "45 min"):
        assert gone not in out, gone
    assert "likes beef" in out and "HW6" in out and "eggs" in out
    assert "5 days since" in out, "day-granular counts are kept on purpose"
    assert "<dur>" in out and "<clock>" in out


def test_bands_and_dayparts():
    from stale_skip import _band_hours, _daypart
    assert _band_hours(None) == "none"
    assert _band_hours(0.5) == "<1h" and _band_hours(2.9) == "<3h" and _band_hours(3.0) == "<6h"
    assert _band_hours(100) == ">=72h"
    assert _daypart(6) == "morning" and _daypart(12) == "midday" and _daypart(15) == "afternoon"
    assert _daypart(18) == "evening" and _daypart(23) == "night" and _daypart(2) == "night"


def test_fingerprint_stable_within_daypart_and_changes_across(db, monkeypatch, anthropic_stub):
    import heartbeat
    from models import get_session, User
    from stale_skip import fingerprint
    from tests.factories import make_user

    user = make_user(db)
    s = get_session()
    try:
        u = s.get(User, user.id)
        ctx_a = heartbeat._proactive_context(u, s)
        ctx_b = heartbeat._proactive_context(u, s)
        # 08:00 PT vs 08:30 PT (same daypart) vs 13:00 PT (midday) — all 2026-10-05, PDT = UTC-7
        t1 = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 10, 5, 15, 30, tzinfo=timezone.utc)
        t3 = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
        assert fingerprint(u, s, ctx_a, now=t1) == fingerprint(u, s, ctx_b, now=t2), \
            "two context builds seconds apart in the same daypart must fingerprint identically"
        assert fingerprint(u, s, ctx_a, now=t1) != fingerprint(u, s, ctx_a, now=t3), \
            "a new daypart re-opens the question"
    finally:
        s.close()


# ---- shadow mode (default): verdict recorded, model still runs -------------

def test_first_tick_has_no_anchor_and_stores_fingerprint(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)

    heartbeat.heartbeat_tick(user.id)

    t = _ticks(user.id)[-1]
    assert t.reason == "nothing new to add"
    assert t.fingerprint and len(t.fingerprint) == 24
    assert t.stale_would_skip is False
    assert len(anthropic_stub.calls) == 1


def test_shadow_marks_would_skip_but_still_calls_model(db, monkeypatch, sms_capture, anthropic_stub, caplog):
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)

    heartbeat.heartbeat_tick(user.id)
    with caplog.at_level("INFO", logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)

    t1, t2 = _ticks(user.id)
    assert t1.fingerprint == t2.fingerprint
    assert t2.stale_would_skip is True
    assert t2.reason == "nothing new to add", "shadow: the model's own reason is still recorded"
    assert len(anthropic_stub.calls) == 2, "shadow mode never skips the model call"
    assert "HEARTBEAT_STALE_SHADOW_HIT" in caplog.text
    assert sms_capture == []


def test_shadow_miss_is_logged_when_model_speaks_on_a_would_skip_tick(db, monkeypatch, sms_capture, anthropic_stub, caplog):
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    _speak(anthropic_stub, "it's 6. get the run in")
    with caplog.at_level("INFO", logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)

    t2 = _ticks(user.id)[-1]
    assert t2.spoke is True and t2.stale_would_skip is True
    assert "HEARTBEAT_STALE_SHADOW_MISS" in caplog.text
    assert len(sms_capture) == 1, "shadow mode changes nothing user-facing"


def test_shadow_chain_walks_back_over_would_skip_ticks(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    heartbeat.heartbeat_tick(user.id)

    v = _verdict(user.id)
    assert v.would_skip is True
    assert "1 chained" in v.detail, "the anchor is the real decision, not the shadow-skipped tick"


# ---- what resets the chain ---------------------------------------------------

def test_spoken_anchor_never_skips(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _speak(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    v = _verdict(user.id)
    assert v.would_skip is False and "not model-silent" in v.detail and "spoke" in v.detail


def test_code_gate_between_resets_the_chain(db, monkeypatch, sms_capture, anthropic_stub):
    """The first tick after quiet hours / a calendar block ALWAYS runs: 22 of 31 live
    texts went out on exactly that tick with nothing new in the DB."""
    import heartbeat
    from models import get_session, HeartbeatTick
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=user.id, spoke=False, reason="guardrail:quiet_hours_standing"))
        s.commit()
    finally:
        s.close()

    v = _verdict(user.id)
    assert v.would_skip is False and "guardrail:quiet_hours_standing" in v.detail


def test_truncated_or_anomalous_anchor_never_skips(db, monkeypatch, sms_capture, anthropic_stub):
    from models import HeartbeatTick
    from stale_skip import is_model_silent
    assert is_model_silent(HeartbeatTick(spoke=False, reason="already nudged today")) is True
    for r in ("truncated:max_tokens (decide hit the output cap)", "guardrail:daily_budget",
              "skipped:stale (anchor #1)", "no message composed", "decision loop exhausted",
              "send_text empty message", ""):
        assert is_model_silent(HeartbeatTick(spoke=False, reason=r)) is False, r
    assert is_model_silent(HeartbeatTick(spoke=True, reason="spoke")) is False


def test_new_inbound_message_since_anchor_runs(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, Message
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="done w class", message_type="freeform",
                      created_at=_utcnow_naive()))
        s.commit()
    finally:
        s.close()

    assert _verdict(user.id).would_skip is False
    assert "message" in _landed(user.id, _ticks(user.id)[0].decided_at)


def test_new_meal_since_anchor_runs(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, Meal
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="eggs", calories=300, protein_g=20,
                   eaten_at=_utcnow_naive(), logged_at=_utcnow_naive()))
        s.commit()
    finally:
        s.close()

    assert _verdict(user.id).would_skip is False
    assert "meal" in _landed(user.id, _ticks(user.id)[0].decided_at)


def test_phone_activity_since_anchor_runs(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, User
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    s = get_session()
    try:
        s.get(User, user.id).last_active_at = _utcnow_naive()  # a card open / tapback
        s.commit()
    finally:
        s.close()

    assert _verdict(user.id).would_skip is False
    assert "phone_activity" in _landed(user.id, _ticks(user.id)[0].decided_at)


def test_floor_forces_a_real_evaluation(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    assert _verdict(user.id).would_skip is True

    _backdate_tick(_ticks(user.id)[0].id, config.HEARTBEAT_STALE_MAX_HOURS + 0.5)
    v = _verdict(user.id)
    assert v.would_skip is False and v.detail.startswith("floor:")


# ---- the model's own recheck time -------------------------------------------

def test_recheck_in_minutes_is_stored_clamped_and_honoured(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, HeartbeatTick
    from tests.factories import make_user

    user = make_user(db)
    _allow(monkeypatch, user)

    _silent(anthropic_stub, "session at 6, nothing till then", recheck_in_minutes=30)
    heartbeat.heartbeat_tick(user.id)
    t = _ticks(user.id)[-1]
    assert t.recheck_at is not None
    delta = (t.recheck_at - _utcnow_naive()).total_seconds() / 60
    assert 28 <= delta <= 31
    assert _verdict(user.id).would_skip is True, "recheck still in the future → stale"

    s = get_session()
    try:
        s.get(HeartbeatTick, t.id).recheck_at = _utcnow_naive() - timedelta(minutes=1)
        s.commit()
    finally:
        s.close()
    v = _verdict(user.id)
    assert v.would_skip is False and "recheck due" in v.detail

    # clamping: never sooner than a tick, never pinned past a day
    _silent(anthropic_stub, "x", recheck_in_minutes=2)
    heartbeat.heartbeat_tick(user.id)
    assert 9 <= (_ticks(user.id)[-1].recheck_at - _utcnow_naive()).total_seconds() / 60 <= 11
    _silent(anthropic_stub, "x", recheck_in_minutes=99999)
    heartbeat.heartbeat_tick(user.id)
    assert 1438 <= (_ticks(user.id)[-1].recheck_at - _utcnow_naive()).total_seconds() / 60 <= 1441
    _silent(anthropic_stub, "x", recheck_in_minutes="soon")
    heartbeat.heartbeat_tick(user.id)
    assert _ticks(user.id)[-1].recheck_at is None, "garbage is ignored, never a crash"


# ---- enabled: the model call is actually skipped ----------------------------

def test_enabled_skips_model_chains_and_floor_reopens(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_ENABLED", True)
    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)

    heartbeat.heartbeat_tick(user.id)                 # real decision (anchor)
    heartbeat.heartbeat_tick(user.id)                 # skipped
    heartbeat.heartbeat_tick(user.id)                 # skipped (anchor still tick 1)
    ticks = _ticks(user.id)
    assert len(anthropic_stub.calls) == 1, "enabled: a stale tick never reaches the model"
    assert ticks[1].reason.startswith("skipped:stale") and ticks[2].reason.startswith("skipped:stale")
    assert "1 chained" in ticks[2].reason
    assert ticks[1].fingerprint == ticks[0].fingerprint
    assert ticks[1].stale_would_skip is True
    assert sms_capture == []

    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        from tests._fake_anthropic import ToolUse
        return ToolUse("stay_silent", {"reason": "still nothing"})
    anthropic_stub.reply_with(handler)

    _backdate_tick(ticks[0].id, config.HEARTBEAT_STALE_MAX_HOURS + 0.5)
    heartbeat.heartbeat_tick(user.id)                 # floor → real evaluation
    assert len(anthropic_stub.calls) == 2
    assert _ticks(user.id)[-1].reason == "still nothing"
    sys_text = "".join(b["text"] for b in captured["system"])
    assert "skipped:stale" not in sys_text, "skipped ticks are not decisions — kept out of TICK HISTORY"


def test_enabled_runs_when_state_changes(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from models import get_session, Message
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_ENABLED", True)
    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    s = get_session()
    try:  # a fired reminder / water ping is an outbound row → something landed
        s.add(Message(user_id=user.id, direction="out", body="drink some water", message_type="reminder",
                      created_at=_utcnow_naive() - timedelta(minutes=5)))
        s.commit()
    finally:
        s.close()
    # the anti-stack gate would block a reminder within 180 min; neutralise it so the
    # tick reaches the rule (this test is about the rule, not the gate)
    monkeypatch.setattr(config, "HEARTBEAT_STACK_WINDOW_MINUTES", 1)

    heartbeat.heartbeat_tick(user.id)
    assert len(anthropic_stub.calls) == 2
    assert not _ticks(user.id)[-1].reason.startswith("skipped:")


def test_dry_run_never_skips_even_when_enabled(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_ENABLED", True)
    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)

    spoke, payload, search = heartbeat.decide(user.id, allow_skip=False)
    assert len(anthropic_stub.calls) == 2
    assert search["stale_would_skip"] is True and payload == "nothing new to add"


def test_eval_failure_fails_open_to_the_model(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat, stale_skip
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_ENABLED", True)

    def boom(*a, **k):
        raise RuntimeError("fingerprint exploded")
    monkeypatch.setattr(stale_skip, "evaluate", boom)

    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    assert len(anthropic_stub.calls) == 1
    t = _ticks(user.id)[-1]
    assert t.reason == "nothing new to add" and t.fingerprint is None


def test_both_flags_off_records_nothing(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_SHADOW", False)
    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_ENABLED", False)
    user = make_user(db)
    _allow(monkeypatch, user)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    heartbeat.heartbeat_tick(user.id)
    assert len(anthropic_stub.calls) == 2
    assert all(t.fingerprint is None and t.stale_would_skip is False for t in _ticks(user.id))
