"""
Tier-2 (live) — heartbeat burn-in measurement. The heartbeat is judged, not
asserted: does the coach stay silent when nothing is happening (default-silent
discipline), speak on a real signal, and NOT re-send a nudge it already sent?
This harness runs `decide` live over a few seeded states, prints each decision +
reason, and records cost so the summary can set a per-day search budget from a
real speak rate. Run: pytest --run-tier2 -s.

The pass/fail here is deliberately light (silence on the empty state + cost
recorded); the speak *quality* is read off the printed transcript, per the founder's
"prompts describe intent; transcript shows behavior."
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cost(user_id):
    from models import get_session, TokenUsage
    s = get_session()
    try:
        rows = s.query(TokenUsage).filter(
            TokenUsage.user_id == user_id, TokenUsage.site == "heartbeat.decide").all()
    finally:
        s.close()
    return sum(r.cost_usd or 0 for r in rows), len(rows)


def test_heartbeat_default_silent_on_empty_state(db, monkeypatch):
    """Nothing has happened — no signal, no open thread. A good coach says nothing."""
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])  # allowlist bypassed via decide()
    user = make_user(db, name="Sam")

    spoke, payload = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT empty-state] spoke={spoke} :: {payload!r}")
    std, n = _cost(user.id)
    print(f"[HEARTBEAT empty-state] cost=${std:.5f} over {n} call(s)")

    assert spoke is False, "default-silent discipline: an empty state must not trigger a text"
    assert n >= 1, "the decision call must be metered"


def test_heartbeat_does_not_repeat_a_sent_nudge(db, monkeypatch):
    """The exact thought was sent an hour ago. The tick must NOT send it again."""
    import config, heartbeat
    from models import get_session, HeartbeatTick, Message
    from tests.factories import make_user

    user = make_user(db, name="Sam")
    nudge = "hey, you've skipped legs twice this week — what's getting in the way?"
    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=user.id, spoke=True, reason="spoke", message=nudge,
                            decided_at=_utcnow_naive() - timedelta(hours=1)))
        s.add(Message(user_id=user.id, direction="out", body=nudge,
                     created_at=_utcnow_naive() - timedelta(hours=1)))
        s.commit()
    finally:
        s.close()

    spoke, payload = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT repeat-guard] spoke={spoke} :: {payload!r}")

    if spoke:
        # if it speaks at all, it must be a DIFFERENT thought, not the same skipped-legs line
        assert "skipped legs twice" not in payload.lower(), \
            "re-sent the identical nudge — anti-repetition signal failed"


def test_heartbeat_speak_rate_and_cost_summary(db, monkeypatch, capsys):
    """Burn-in: run the tick across a spread of states, print the speak rate + cost.
    No hard assertion on the rate — this is the measurement the summary quotes."""
    import config, heartbeat
    from models import get_session, HeartbeatTick, Message
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_WEB_SEARCH", True)  # proactive path, burn-in config

    scenarios = []
    # 1. quiet, nothing happening
    scenarios.append(("quiet", make_user(db, name="Sam").id))
    # 2. an open personal thread mentioned earlier today (via an inbound message)
    u2 = make_user(db, name="Priya")
    s = get_session()
    try:
        s.add(Message(user_id=u2.id, direction="in",
                     body="ugh i have a huge orgo midterm tomorrow morning",
                     created_at=_utcnow_naive() - timedelta(hours=18)))
        s.commit()
    finally:
        s.close()
    scenarios.append(("open-thread", u2.id))
    # 3. already nudged today (should lean silent)
    u3 = make_user(db, name="Marcus")
    s = get_session()
    try:
        s.add(HeartbeatTick(user_id=u3.id, spoke=True, reason="spoke",
                            message="proud of you for hitting the gym 4x this week",
                            decided_at=_utcnow_naive() - timedelta(hours=2)))
        s.commit()
    finally:
        s.close()
    scenarios.append(("already-spoke", u3.id))

    spoke_count, total_cost, total_calls = 0, 0.0, 0
    for label, uid in scenarios:
        spoke, payload = heartbeat.decide(uid)
        c, n = _cost(uid)
        total_cost += c
        total_calls += n
        spoke_count += 1 if spoke else 0
        print(f"\n[HEARTBEAT {label}] spoke={spoke} :: {payload!r}  (${c:.5f})")

    rate = spoke_count / len(scenarios)
    print(f"\n[HEARTBEAT] speak rate: {spoke_count}/{len(scenarios)} = {rate:.0%}")
    print(f"[HEARTBEAT] avg cost/tick: ${total_cost/len(scenarios):.5f} "
          f"(over {total_calls} model call(s))")
    print("[HEARTBEAT] NOTE: a searching tick also bills ~$0.01/search on top of tokens; "
          "record the search count from the console. Use this rate to set HEARTBEAT_MAX_PER_DAY.")
