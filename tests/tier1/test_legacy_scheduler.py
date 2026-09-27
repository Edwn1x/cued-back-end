"""
Heartbeat calibration — Item 2: the legacy templated scheduler has been REMOVED
(phase-6 Commit A). The heartbeat is the ONLY proactive system. The reversible
flag (LEGACY_SCHEDULER_ENABLED, default off) is retained in config as a tripwire,
and these prove start_scheduler still boots cleanly with only the heartbeat (and
no legacy per-user / adherence jobs) registered.
"""

from __future__ import annotations


# ---- default: off -----------------------------------------------------------

def test_legacy_scheduler_defaults_off():
    import config
    assert config.LEGACY_SCHEDULER_ENABLED is False, \
        "legacy proactive scheduler must default OFF — heartbeat owns proactive contact"


# ---- start_scheduler wiring: heartbeat registers, legacy does not -----------

def test_start_scheduler_registers_heartbeat_not_legacy_when_disabled(db, monkeypatch):
    """With the legacy templated scheduler removed, start_scheduler still starts
    cleanly, the heartbeat job registers, and no legacy proactive job (per-user or
    the global adherence check) is registered — only the heartbeat can produce a
    proactive outbound."""
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
        assert "global_heartbeat" in job_ids, "heartbeat must register"
        assert "global_adherence_check" not in job_ids, "legacy adherence job must not register"
        assert not any(jid.startswith(f"user_{user.id}_") for jid in job_ids), \
            "no legacy per-user job may register"
    finally:
        for jid in ("global_heartbeat", "daily_dining_scrape"):
            if scheduler.scheduler.get_job(jid):
                scheduler.scheduler.remove_job(jid)
