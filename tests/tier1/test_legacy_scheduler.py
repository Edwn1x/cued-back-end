"""
Heartbeat calibration — Item 2: the legacy templated scheduler is disabled behind
a reversible flag (LEGACY_SCHEDULER_ENABLED, default off) so the heartbeat is the
ONLY proactive system during burn-in. These prove the disable is clean and complete
(no legacy proactive outbound can fire) and that re-enabling restores the jobs.

Deterministic: the model is stubbed and Twilio is captured. We assert on job
registration and on send paths being no-ops, not on model output.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- default: off -----------------------------------------------------------

def test_legacy_scheduler_defaults_off():
    import config
    assert config.LEGACY_SCHEDULER_ENABLED is False, \
        "legacy proactive scheduler must default OFF — heartbeat owns proactive contact"


# ---- send paths are no-ops when disabled (strongest guarantee) --------------

def test_send_scheduled_message_noop_when_disabled(db, monkeypatch, sms_capture, anthropic_stub):
    import config, scheduler
    from tests.factories import make_user

    monkeypatch.setattr(config, "LEGACY_SCHEDULER_ENABLED", False)
    user = make_user(db)

    scheduler.send_scheduled_message(user.id, "morning_briefing")

    assert sms_capture == [], "no legacy proactive SMS may go out when disabled"
    assert anthropic_stub.calls == [], "must short-circuit before any model call"


def test_check_meal_adherence_noop_when_disabled(db, monkeypatch, sms_capture, anthropic_stub):
    import config, scheduler
    from models import get_session, Message
    from tests.factories import make_user

    monkeypatch.setattr(config, "LEGACY_SCHEDULER_ENABLED", False)
    user = make_user(db, onboarding_step=3)
    # active in last 24h, no meal logged -> would normally trigger an adherence nudge
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="hey",
                      created_at=_utcnow_naive() - timedelta(hours=2)))
        s.commit()
    finally:
        s.close()

    scheduler.check_meal_adherence()

    assert sms_capture == [], "adherence check must not send when legacy is disabled"
    assert anthropic_stub.calls == []


# ---- job registration gated at schedule_user (covers all 3 callers) ---------

def _user_job_ids(user_id):
    import scheduler
    return [j.id for j in scheduler.scheduler.get_jobs() if j.id.startswith(f"user_{user_id}_")]


def test_schedule_user_registers_no_jobs_when_disabled(db, monkeypatch):
    import config, scheduler
    from tests.factories import make_user

    monkeypatch.setattr(config, "LEGACY_SCHEDULER_ENABLED", False)
    user = make_user(db)  # has wake_time/sleep_time -> would register jobs if enabled

    scheduler.schedule_user(user)

    assert _user_job_ids(user.id) == [], "disabled legacy scheduler must register no per-user jobs"


def test_schedule_user_registers_jobs_when_enabled(db, monkeypatch):
    """The conscious re-enable path still works — proves the flag is the ONLY gate
    and we didn't otherwise break scheduling."""
    import config, scheduler
    from tests.factories import make_user

    monkeypatch.setattr(config, "LEGACY_SCHEDULER_ENABLED", True)
    user = make_user(db)
    try:
        scheduler.schedule_user(user)
        ids = _user_job_ids(user.id)
        assert f"user_{user.id}_morning_briefing" in ids
    finally:
        for jid in _user_job_ids(user.id):
            scheduler.scheduler.remove_job(jid)


# ---- start_scheduler wiring: heartbeat registers, legacy does not -----------

def test_start_scheduler_registers_heartbeat_not_legacy_when_disabled(db, monkeypatch):
    """Item 2 spec test 2/3: with legacy off, the scheduler still starts cleanly,
    the heartbeat job registers, and no legacy proactive job (per-user or the global
    adherence check) is registered — only the heartbeat can produce a proactive
    outbound."""
    import config, scheduler
    import dining_scraper
    from tests.factories import make_user

    monkeypatch.setattr(config, "LEGACY_SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    monkeypatch.setattr(config, "CONSOLIDATION_ENABLED", False)
    monkeypatch.setattr(config, "EPISODIC_ENABLED", False)
    # neutralize the network scrape and the real thread start
    monkeypatch.setattr(dining_scraper, "scrape_all_halls", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.scheduler, "start", lambda *a, **k: None)

    user = make_user(db)
    try:
        scheduler.start_scheduler()

        job_ids = {j.id for j in scheduler.scheduler.get_jobs()}
        assert "global_heartbeat" in job_ids, "heartbeat must register with legacy off"
        assert "global_adherence_check" not in job_ids, "legacy adherence job must not register"
        assert not any(jid.startswith(f"user_{user.id}_") for jid in job_ids), \
            "no legacy per-user job may register when disabled"
    finally:
        for jid in ("global_heartbeat", "daily_dining_scrape"):
            if scheduler.scheduler.get_job(jid):
                scheduler.scheduler.remove_job(jid)
