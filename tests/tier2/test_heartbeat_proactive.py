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


def _evening_tz():
    """A fixed-offset zone where the user's local time is ~8pm right now. The
    search-worthy scenarios ('late session tonight if it's open') are only live in
    the evening — run at 2am local they resolve themselves ('window passed') and the
    tick never has a reason to search, regardless of the necessity claim. Note
    Etc/GMT signs are inverted: Etc/GMT-5 means UTC+5."""
    offset = (20 - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


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

    spoke, payload, search = heartbeat.decide(user.id)
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

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT repeat-guard] spoke={spoke} :: {payload!r}")

    if spoke:
        # if it speaks at all, it must be a DIFFERENT thought, not the same skipped-legs line
        assert "skipped legs twice" not in payload.lower(), \
            "re-sent the identical nudge — anti-repetition signal failed"


def test_heartbeat_search_by_need_not_reflex(db, monkeypatch):
    """Addendum: the founder's necessity claim, checked. A tick with a genuinely
    search-worthy proactive opening (user headed to the RSF late — is it even open?)
    vs a tick with nothing to look up. Hard assertion only on the reflex leak mode
    (the nothing-to-look-up tick must not search); whether the search-worthy tick
    actually searches is the burn-in FINDING, printed either way."""
    import config, heartbeat
    from models import get_session, Message
    from tests.factories import make_user

    assert config.HEARTBEAT_WEB_SEARCH is True, "addendum: search on (budgeted) for burn-in"

    tz = _evening_tz()  # both users at ~8pm local, so the openings are live

    # nothing to look up: quiet user, empty day
    quiet = make_user(db, name="Sam", user_timezone=tz)
    spoke_q, payload_q, search_q = heartbeat.decide(quiet.id)
    print(f"\n[SEARCH-NEED quiet] spoke={spoke_q} search_used={search_q['used']} "
          f"query={search_q['query']!r} :: {payload_q!r}")

    # search-worthy: user said hours ago they'd hit the RSF late tonight if it's open
    u = make_user(db, name="Priya", user_timezone=tz)
    s = get_session()
    try:
        s.add(Message(user_id=u.id, direction="in",
                     body="gonna try to squeeze in a late RSF session around 11 tonight "
                          "if it's even open that late, otherwise skipping again",
                     created_at=_utcnow_naive() - timedelta(hours=4)))
        s.commit()
    finally:
        s.close()
    spoke_w, payload_w, search_w = heartbeat.decide(u.id)
    print(f"[SEARCH-NEED worthy] spoke={spoke_w} search_used={search_w['used']} "
          f"query={search_w['query']!r} :: {payload_w!r}")

    assert search_q["available"] is True and search_w["available"] is True, \
        "under budget, the tool must have been offered on both ticks"
    assert search_q["used"] is False, \
        "searched with nothing to look up — the 'used because present' leak mode"
    if not search_w["used"]:
        print("[SEARCH-NEED] FINDING: the search-worthy opening did NOT trigger a search — "
              "evidence against the necessity claim; read the reply above before concluding.")


def test_heartbeat_speak_rate_and_cost_summary(db, monkeypatch, capsys):
    """Burn-in: run the tick across a spread of states, print speak rate + cost
    TWO-TRACK (searched vs unsearched ticks — addendum §4). No hard assertion on the
    rate — this is the measurement the summary quotes; a blended average would hide
    an 11x line item when extrapolating to 50 users."""
    import config, heartbeat
    from models import get_session, HeartbeatTick, Message
    from tests.factories import make_user

    assert config.HEARTBEAT_WEB_SEARCH is True  # proactive path, burn-in config (budgeted)

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
    # 4. a lookup-worthy opening, so the searched track isn't structurally empty
    #    (evening-local user — at 2am the opening self-resolves and nothing searches)
    u4 = make_user(db, name="Jo", user_timezone=_evening_tz())
    s = get_session()
    try:
        s.add(Message(user_id=u4.id, direction="in",
                     body="might do a late RSF session near midnight if it's open that late",
                     created_at=_utcnow_naive() - timedelta(hours=5)))
        s.commit()
    finally:
        s.close()
    scenarios.append(("lookup-worthy", u4.id))

    # two-track accumulators: searched vs unsearched ticks
    track = {True: {"n": 0, "spoke": 0, "cost": 0.0},
             False: {"n": 0, "spoke": 0, "cost": 0.0}}
    total_calls = 0
    for label, uid in scenarios:
        spoke, payload, search = heartbeat.decide(uid)
        c, n = _cost(uid)
        total_calls += n
        t = track[search["used"]]
        t["n"] += 1
        t["spoke"] += 1 if spoke else 0
        t["cost"] += c
        print(f"\n[HEARTBEAT {label}] spoke={spoke} search_used={search['used']} "
              f"query={search['query']!r} :: {payload!r}  (${c:.5f})")

    n_all = sum(t["n"] for t in track.values())
    spoke_all = sum(t["spoke"] for t in track.values())
    print(f"\n[HEARTBEAT] overall speak rate: {spoke_all}/{n_all} = {spoke_all/n_all:.0%} "
          f"(over {total_calls} model call(s))")
    print(f"[HEARTBEAT] search share: {track[True]['n']}/{n_all} ticks invoked search")
    for used, name in ((True, "searched"), (False, "unsearched")):
        t = track[used]
        if t["n"] == 0:
            print(f"[HEARTBEAT] {name} ticks: none this run")
            continue
        print(f"[HEARTBEAT] {name} ticks: speak rate {t['spoke']}/{t['n']}, "
              f"avg cost/tick ${t['cost']/t['n']:.5f}")
    print("[HEARTBEAT] NOTE: a searching tick also bills ~$0.01/search on top of tokens; "
          "extrapolate to 50 users from the SHARE + per-track costs above, never the "
          "blended average. HEARTBEAT_SEARCH_MAX_PER_DAY caps searched ticks/user/day.")
