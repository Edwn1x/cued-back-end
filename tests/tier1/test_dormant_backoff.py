"""
Dormant-user backoff (stale_skip.backoff_verdict): once a user has been quiet for N
days, the heartbeat looks at most once per H hours unless they engage — a dormancy-
dependent floor that ignores the fingerprint (daypart changes no longer re-open the
question). Shadow mode records the verdict and still runs the model; enabled mode
skips. Deterministic machinery tests; the live false-negative rate is read off
HEARTBEAT_BACKOFF_SHADOW_MISS.
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


def _silent(stub, reason="ghosted, holding", **extra):
    from tests._fake_anthropic import ToolUse
    stub.reply_with(lambda kw: ToolUse("stay_silent", {"reason": reason, **extra}))


def _speak(stub, message="hey, been a min"):
    from tests._fake_anthropic import ToolUse
    stub.reply_with(lambda kw: ToolUse("send_text", {"message": message}))


def _seed_inbound(user_id, days_ago, body="hey"):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction="in", body=body, message_type="freeform",
                      created_at=_utcnow_naive() - timedelta(days=days_ago)))
        s.commit()
    finally:
        s.close()


def _backdate_tick(tick_id, hours):
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        t = s.get(HeartbeatTick, tick_id)
        t.decided_at = t.decided_at - timedelta(hours=hours)
        s.commit()
    finally:
        s.close()


def _verdict(user_id):
    import heartbeat
    from models import get_session, User
    from stale_skip import evaluate
    s = get_session()
    try:
        u = s.get(User, user_id)
        return evaluate(u, s, heartbeat._proactive_context(u, s))
    finally:
        s.close()


def _dormant_user(db, monkeypatch, days_quiet=10):
    """A user whose last inbound was `days_quiet` days ago, with one real (silent)
    model evaluation already on file, and a different daypart baked into the next
    fingerprint so the STALE rule would say 'run' (that's the case the backoff owns)."""
    from tests.factories import make_user
    user = make_user(db)
    _allow(monkeypatch, user)
    _seed_inbound(user.id, days_quiet)
    return user


# ---- pure: tiers --------------------------------------------------------------

def test_tier_parsing_and_lookup(monkeypatch):
    import config
    from stale_skip import backoff_tiers, backoff_hours_for
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_TIERS", "14:20, 7:10, junk, 3:x")
    assert backoff_tiers() == [(7.0, 10.0), (14.0, 20.0)]
    assert backoff_hours_for(None) == 0
    assert backoff_hours_for(2) == 0
    assert backoff_hours_for(7) == 10
    assert backoff_hours_for(13.9) == 10
    assert backoff_hours_for(14) == 20 and backoff_hours_for(40) == 20
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_TIERS", "")
    assert backoff_hours_for(30) == 0, "no tiers = no backoff (fail-open)"


def test_days_quiet_uses_latest_of_inbound_and_phone_activity(db):
    from models import get_session, User
    from stale_skip import days_quiet
    from tests.factories import make_user
    user = make_user(db)
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert days_quiet(u, s) is None, "no engagement on record → never backed off"
        _seed_inbound(user.id, 12)
        s.expire_all()
        assert 11.9 < days_quiet(u, s) < 12.1
        u.last_active_at = _utcnow_naive() - timedelta(days=2)   # card open 2 days ago
        s.commit()
        assert 1.9 < days_quiet(u, s) < 2.1, "a card open is engagement"
    finally:
        s.close()


# ---- shadow (default): verdict recorded, model still runs ---------------------

def test_dormant_user_would_skip_in_shadow_but_model_runs(db, monkeypatch, sms_capture, anthropic_stub, caplog):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)

    heartbeat.heartbeat_tick(user.id)       # the look
    # push the first evaluation into a different daypart so the stale rule says "run"
    # (fingerprint differs) — the backoff is what must hold the line
    _backdate_tick(_ticks(user.id)[0].id, 5)
    with caplog.at_level("INFO", logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)

    t1, t2 = _ticks(user.id)
    assert t2.backoff_would_skip is True and t2.stale_would_skip is False
    assert len(anthropic_stub.calls) == 2, "shadow never skips the model call"
    assert "HEARTBEAT_BACKOFF_SHADOW_HIT" in caplog.text
    assert sms_capture == []


def test_backoff_shadow_miss_logged_when_model_speaks(db, monkeypatch, sms_capture, anthropic_stub, caplog):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    _speak(anthropic_stub)
    with caplog.at_level("INFO", logger="cued.heartbeat"):
        heartbeat.heartbeat_tick(user.id)
    t2 = _ticks(user.id)[-1]
    assert t2.spoke is True and t2.backoff_would_skip is True
    assert "HEARTBEAT_BACKOFF_SHADOW_MISS" in caplog.text
    assert len(sms_capture) == 1, "shadow changes nothing user-facing"


# ---- what the backoff keys on ------------------------------------------------

def test_active_user_never_backed_off(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=2)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    v = _verdict(user.id)
    assert v.would_skip is False and v.kind == ""


def test_interval_elapsed_runs_again(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=10)   # tier 7:10
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 9.5)
    assert _verdict(user.id).kind == "backoff"
    _backdate_tick(_ticks(user.id)[0].id, 1)               # now 10.5h since the look
    v = _verdict(user.id)
    assert v.would_skip is False


def test_deeper_dormancy_uses_longer_interval(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=20)   # tier 14:20
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 15)
    v = _verdict(user.id)
    assert v.kind == "backoff" and "every 20h" in v.detail


def test_inbound_since_last_look_runs(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    _seed_inbound(user.id, 0, body="back")
    v = _verdict(user.id)
    assert v.would_skip is False, "they engaged — and they're no longer dormant either"


def test_our_own_outbound_ping_is_not_engagement(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, Message
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="drink some water", message_type="reminder",
                      created_at=_utcnow_naive() - timedelta(minutes=10)))
        s.commit()
    finally:
        s.close()
    v = _verdict(user.id)
    assert v.kind == "backoff", "a water ping we sent must not count as them engaging"


def test_logged_meal_counts_as_engagement(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    from models import get_session, Meal
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="eggs", calories=300, protein_g=20,
                   eaten_at=_utcnow_naive(), logged_at=_utcnow_naive()))
        s.commit()
    finally:
        s.close()
    assert _verdict(user.id).would_skip is False


def test_model_recheck_due_overrides_backoff(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub, recheck_in_minutes=60)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)             # 5h since look, recheck was +1h → due
    from models import get_session, HeartbeatTick
    s = get_session()
    try:
        t = s.get(HeartbeatTick, _ticks(user.id)[0].id)
        t.recheck_at = t.recheck_at - timedelta(hours=5)   # keep it consistent with the backdate
        s.commit()
    finally:
        s.close()
    assert _verdict(user.id).would_skip is False


def test_gate_between_does_not_reset_the_backoff_clock(db, monkeypatch, sms_capture, anthropic_stub):
    """Unlike the stale rule, a quiet-hours gate between looks must NOT re-arm the
    model — otherwise every first-post-quiet tick runs daily at every tier."""
    import heartbeat
    from models import get_session, HeartbeatTick
    user = _dormant_user(db, monkeypatch, days_quiet=20)   # tier 14:20
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 12)
    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=user.id, spoke=False, reason="guardrail:quiet_hours_standing",
                            decided_at=_utcnow_naive() - timedelta(hours=6)))
        s.commit()
    finally:
        s.close()
    v = _verdict(user.id)
    assert v.kind == "backoff", "12h since the last LOOK, tier says 20h — still held"


def test_stale_rule_takes_precedence_when_it_applies(db, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    v = _verdict(user.id)      # seconds later: same daypart, same state → stale wins
    assert v.would_skip is True and v.kind == "stale"


# ---- enabled ------------------------------------------------------------------

def test_enabled_skips_model_and_logs_backoff_reason(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_ENABLED", True)
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    heartbeat.heartbeat_tick(user.id)
    heartbeat.heartbeat_tick(user.id)
    ticks = _ticks(user.id)
    assert len(anthropic_stub.calls) == 1
    assert ticks[1].reason.startswith("skipped:backoff") and ticks[2].reason.startswith("skipped:backoff")
    assert ticks[1].backoff_would_skip is True and ticks[1].fingerprint
    assert sms_capture == []

    captured = {}

    def handler(kw):
        captured["system"] = kw.get("system")
        from tests._fake_anthropic import ToolUse
        return ToolUse("stay_silent", {"reason": "still holding"})
    anthropic_stub.reply_with(handler)
    _backdate_tick(ticks[0].id, 6)                      # 11h since the look → tier 10h elapsed
    heartbeat.heartbeat_tick(user.id)
    assert len(anthropic_stub.calls) == 2
    sys_text = "".join(b["text"] for b in captured["system"])
    assert "skipped:backoff" not in sys_text, "skips are not decisions — out of TICK HISTORY"


def test_enabled_backoff_still_lets_stale_rule_run_first(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_STALE_SKIP_ENABLED", True)
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    heartbeat.heartbeat_tick(user.id)
    assert _ticks(user.id)[-1].reason.startswith("skipped:stale")


def test_dry_run_never_skips_on_backoff(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_ENABLED", True)
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    spoke, payload, search = heartbeat.decide(user.id, allow_skip=False)
    assert len(anthropic_stub.calls) == 2 and search["backoff_would_skip"] is True


def test_backoff_flags_off_records_nothing(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_SHADOW", False)
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_ENABLED", False)
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    heartbeat.heartbeat_tick(user.id)
    assert len(anthropic_stub.calls) == 2
    assert all(t.backoff_would_skip is False for t in _ticks(user.id))


def test_backoff_eval_failure_fails_open(db, monkeypatch, sms_capture, anthropic_stub):
    import config, heartbeat, stale_skip
    monkeypatch.setattr(config, "HEARTBEAT_BACKOFF_ENABLED", True)

    def boom(*a, **k):
        raise RuntimeError("tiers exploded")
    monkeypatch.setattr(stale_skip, "backoff_verdict", boom)
    user = _dormant_user(db, monkeypatch, days_quiet=10)
    _silent(anthropic_stub)
    heartbeat.heartbeat_tick(user.id)
    _backdate_tick(_ticks(user.id)[0].id, 5)
    heartbeat.heartbeat_tick(user.id)
    assert len(anthropic_stub.calls) == 2
    assert _ticks(user.id)[-1].reason == "ghosted, holding"
