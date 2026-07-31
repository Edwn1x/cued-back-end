"""
Tier-2 (live) — heartbeat speak-calibration anchors. After PR2 the suite is no longer
judgment-only: the two ENDS of the decision are pinned as HARD anchors, the ambiguous
middle stays printed-observation.

- YES-anchor (must speak): a multi-day training fall-off for a user who asked to be
  called out — the product's core promise, asserted `spoke is True`. Proven RED on the
  pre-calibration prompt (0/4); must be GREEN after 2a+2b. Binary: re-run 2-3x, pass
  every time.
- NO-anchors (must stay silent): empty state, on-track quiet day, mid-conversation —
  asserted `spoke is False`, so a loosened threshold can't become a nag.
- The middle (open-thread, lookup-worthy, search-by-need) stays a printed FINDING, and
  cost is recorded two-track so the summary can set a per-day search budget from a real
  speak rate. Run: pytest --run-tier2 -s.
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


def test_heartbeat_speaks_on_accountability_gap(db, monkeypatch):
    """THE yes-anchor — the single most important test in the suite: the product's core
    promise as a hard assertion. A multi-day training fall-off for a user who explicitly
    asked to be held accountable is the clearest possible SPEAK moment; if the coach
    stays silent here it has failed at its one job, deadlock-fixed or not.

    This anchor was proven RED on the pre-calibration prompt (0/4 across scripted
    openings — the model chose silence on exactly this state, reasoning 'not yet a
    pattern' / 'no new info since last tick'). It must go GREEN after 2a+2b. Because
    the anchor is BINARY, it must be re-run 2-3x and pass EVERY time — 2/3 means the
    coach stays silent on a third of the clearest accountability moments, i.e. not done.

    The fixture MUST encode BOTH halves that make 'must speak' definitionally true —
    a gap alone is a judgment call (maybe they're traveling); gap + explicit standing
    request is what makes the yes unambiguous:
      1. a multi-day training fall-off (~10-12 days, then nothing) in the log, AND
      2. an explicit standing 'call me out / hold me accountable' request in the summary.
    No recent inbound (no 'reply in-thread' pull), no proactive nudge outstanding."""
    import config, heartbeat
    from models import get_session, User, Workout
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])  # allowlist bypassed via decide()
    call_out = ("Committed to training 4x/week (push, pull, legs, upper) on a cut. "
                "Consistency is the real struggle — tends to skip legs and then fall off "
                "for days. Explicitly asked to be called out when he slips, not coddled.")
    user = make_user(db, name="Sam", coaching_summary=call_out)
    s = get_session()
    try:
        # last trained ~10 and ~12 days ago, then nothing — an unambiguous fall-off
        s.add(Workout(user_id=user.id, workout_type="push", completed=True,
                      date=_utcnow_naive() - timedelta(days=10)))
        s.add(Workout(user_id=user.id, workout_type="pull", completed=True,
                      date=_utcnow_naive() - timedelta(days=12)))
        s.commit()
        # verify BOTH halves are actually in the fixture, or the anchor asserts less
        # than it appears to (half #1 = the gap reaches context; half #2 = the request)
        ctx = heartbeat._proactive_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "called out" in call_out, "half #2 missing: no explicit accountability request"
    assert "days" in ctx or "10" in ctx or "12" in ctx, \
        "half #1 sanity: the training history must reach the decision context"

    # precondition: the code gate must NOT suppress this tick (deadlock-fix, PR1)
    from engagement_tracker import has_unanswered_proactive
    assert has_unanswered_proactive(user.id, config.HEARTBEAT_STACK_WINDOW_MINUTES) is False

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT yes-anchor accountability-gap] spoke={spoke} :: {payload!r}")

    _cost_usd, n = _cost(user.id)
    assert n >= 1, "the decision call must have reached the model"
    assert spoke is True, (
        "YES-ANCHOR FAILED: the coach stayed silent on a ~10-day training fall-off for a "
        "user who explicitly asked to be called out — the clearest possible SPEAK moment. "
        f"This is the product's core promise. Silence reason: {payload!r}")


def test_heartbeat_stays_silent_on_on_track_quiet_day(db, monkeypatch):
    """Clear-NO anchor: calibration loosens WHEN the coach speaks, it must NOT turn the
    heartbeat into a chatterbox. A user training on schedule with nothing standing and
    nothing new is the honest default-silent case — trained today and yesterday, no
    open thread, no accountability request. If this speaks, the threshold dropped too
    far. Hard-assert silence: 'speaks when a good coach would' dies here as much as at
    the yes-anchor."""
    import config, heartbeat
    from models import get_session, Workout
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, name="Sam",
                     coaching_summary="Training consistently, on a lean bulk. Doing well.")
    s = get_session()
    try:
        # on schedule: trained today and yesterday, nothing skipped, nothing standing
        s.add(Workout(user_id=user.id, workout_type="push", completed=True,
                      date=_utcnow_naive() - timedelta(hours=6)))
        s.add(Workout(user_id=user.id, workout_type="pull", completed=True,
                      date=_utcnow_naive() - timedelta(days=1)))
        s.commit()
    finally:
        s.close()

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT no-anchor on-track-quiet] spoke={spoke} :: {payload!r}")
    assert spoke is False, (
        "NO-ANCHOR FAILED: the coach texted an on-track user with nothing standing and "
        f"nothing new — the calibration over-loosened into nagging. Message: {payload!r}")


def test_heartbeat_stays_silent_mid_conversation(db, monkeypatch):
    """Clear-NO anchor: a recent inbound means an active conversation — that's REACTIVE
    territory, the coach must reply in-thread, not proactively double-text on top of it.
    In production the active_conversation guardrail hard-gates this in code; here we
    call decide() directly (bypassing guardrails) to prove the PROMPT itself holds the
    line — the loosened threshold must not make it double-text mid-exchange."""
    import config, heartbeat
    from models import get_session, Message
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, name="Sam")
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in",
                      body="just got to the gym, about to start legs",
                      created_at=_utcnow_naive() - timedelta(minutes=3)))
        s.commit()
    finally:
        s.close()

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT no-anchor mid-conversation] spoke={spoke} :: {payload!r}")
    assert spoke is False, (
        "NO-ANCHOR FAILED: the coach proactively texted on top of a 3-minute-old inbound "
        f"— mid-conversation is reactive territory, not a heartbeat's. Message: {payload!r}")


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
