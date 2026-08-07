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
- WARMTH anchors (Part 2): the heartbeat is a friend, not only a coach who catches slips.
  YES (must speak): a genuine win (a real 5-in-7 training streak above goal), a grounded
  check-in on something they mentioned (episodic life-context, flag on for the test).
  NO / anti-bot (must stay silent): a modest on-track week with NO specific warm material
  — the guard against generic engagement bait, the most important negative anchor here.
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


def test_heartbeat_speaks_past_stale_open_thread(db, monkeypatch):
    """YES-anchor for the MODEL-LAYER unanswered-gap deadlock (seen live Aug 6-7): the
    coach reactively asked a question hours ago, the user never answered, and the tick
    history is full of 'waiting on his reply - mid conversation' silent decisions. Five
    straight prod ticks over 3 hours re-cited that reason with the last inbound 1-4
    hours old — the code gate was fixed in PR1 (time-elapse clears the anti-stack), but
    the model rebuilt the same deadlock from the transcript's shape + its own tick echo.

    Fixture = the unambiguous accountability state (multi-day fall-off + explicit
    call-me-out, both halves — same as the core yes-anchor) PLUS the deadlock dressing:
    a reactive coach question 6h unanswered as the most recent outbound, last inbound
    7h ago, and two seeded silent ticks echoing 'waiting on reply'. The dressing must
    NOT mute the anchor: a question that has sat unanswered for hours is an open
    thread (speak material), not a live conversation. Binary — re-run 3x, pass every
    time; the failure mode is precisely the model copying the seeded tick reason."""
    import config, heartbeat
    from models import get_session, Message, HeartbeatTick, Workout
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    call_out = ("Committed to training 4x/week (push, pull, legs, upper) on a cut. "
                "Consistency is the real struggle — tends to skip legs and then fall off "
                "for days. Explicitly asked to be called out when he slips, not coddled.")
    user = make_user(db, name="Sam", coaching_summary=call_out)
    s = get_session()
    try:
        s.add(Workout(user_id=user.id, workout_type="push", completed=True,
                      date=_utcnow_naive() - timedelta(days=10)))
        s.add(Workout(user_id=user.id, workout_type="pull", completed=True,
                      date=_utcnow_naive() - timedelta(days=12)))
        # the stale exchange: they texted 7h ago, the coach asked a (reactive) question
        # 6h ago, silence since — an open thread, NOT a live conversation
        s.add(Message(user_id=user.id, direction="in",
                      body="not sure what to cook tonight",
                      created_at=_utcnow_naive() - timedelta(hours=7)))
        s.add(Message(user_id=user.id, direction="out", message_type="freeform",
                      body="what've you got in the fridge? i'll pick the highest-protein option",
                      created_at=_utcnow_naive() - timedelta(hours=6)))
        # the echo: prior ticks already talked themselves into 'waiting on reply'
        for hrs in (3, 1):
            s.add(HeartbeatTick(user_id=user.id, spoke=False,
                                reason="just asked what he's cooking, waiting on his reply - mid conversation",
                                decided_at=_utcnow_naive() - timedelta(hours=hrs)))
        s.commit()
    finally:
        s.close()

    # preconditions: no CODE gate suppresses this tick — the reactive question is not
    # a proactive nudge (anti-stack) and the inbound is far past the convo pre-gate
    from engagement_tracker import has_unanswered_proactive
    assert has_unanswered_proactive(user.id, config.HEARTBEAT_STACK_WINDOW_MINUTES) is False

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT yes-anchor stale-open-thread] spoke={spoke} :: {payload!r}")
    assert spoke is True, (
        "YES-ANCHOR FAILED (model-layer deadlock): the coach stayed silent on a ~10-day "
        "fall-off for a call-me-out user because its own 6-hour-old unanswered question "
        "read as 'mid-conversation' — the unanswered-gap deadlock rebuilt at the model "
        f"layer. Silence reason: {payload!r}")


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


# ---- WARMTH & PRESENCE anchors (Part 2) -------------------------------------

def _seed_workout(user_id, days_ago, wtype):
    from models import get_session, Workout
    when = _utcnow_naive() - timedelta(days=days_ago)
    s = get_session()
    try:
        s.add(Workout(user_id=user_id, workout_type=wtype, completed=True, date=when))
        s.commit()
    finally:
        s.close()


def test_heartbeat_speaks_on_genuine_win(db, monkeypatch):
    """WARMTH yes-anchor (hard SPEAK): a friend marks a real win, not just a slip. A
    genuine training streak — 5 completed sessions in the last 7 days for someone whose
    stated goal is 4x/week — clears the goal and is worth a text. The code-computed
    MOMENTUM block gives the model the count; it must recognize the win and speak. This
    is the half Part 1 could not do: speak when something is GOOD. Binary, re-run 3x."""
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, name="Sam",
                     coaching_summary=("Goal is training 4x/week on a lean bulk. Has been "
                                       "grinding hard and staying consistent lately."))
    # 5 completed sessions across the last 7 days — above the 4x goal, a real streak
    for d, t in ((0, "push"), (1, "pull"), (2, "legs"), (4, "upper"), (6, "push")):
        _seed_workout(user.id, d, t)

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT warmth win-anchor] spoke={spoke} :: {payload!r}")
    assert spoke is True, (
        "WARMTH YES-ANCHOR FAILED: the coach stayed silent on a genuine 5-in-7 training "
        "streak above the user's 4x goal — a friend marks a real win, it's not all "
        f"callouts. Silence reason: {payload!r}")


def test_heartbeat_checks_in_on_mentioned_event(db, monkeypatch):
    """WARMTH yes-anchor (hard SPEAK): a grounded check-in on something THEY mentioned.
    With episodic life-context available (flag on for this test — the material the
    heartbeat USES, generation is upstream/out of scope), a note about a real upcoming
    event the user was nervous about is exactly 'how'd X go' / 'good luck with X' territory
    — a friend follows up. Tests the heartbeat reaching for episodic warm material."""
    import config, heartbeat
    from models import get_session, EpisodicDigest
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "EPISODIC_ENABLED", True)  # USE episodic material (present)
    user = make_user(db, name="Sam")
    s = get_session()
    try:
        s.add(EpisodicDigest(
            user_id=user.id,
            text="Mentioned a big founders summit pitch this Friday — nervous about it.",
            occurred_on=_utcnow_naive() - timedelta(days=1)))
        s.commit()
    finally:
        s.close()

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT warmth check-in-anchor] spoke={spoke} :: {payload!r}")
    assert spoke is True, (
        "WARMTH YES-ANCHOR FAILED: the coach stayed silent when it had specific, real "
        "life-context to follow up on (a founders summit the user was nervous about) — a "
        f"grounded check-in is what a friend does. Silence reason: {payload!r}")


def test_heartbeat_stays_silent_no_warm_material(db, monkeypatch):
    """ANTI-BOT anchor (hard SILENT) — the most important negative anchor in this pass.
    Warmth is where the 'insufferable proactive bot' risk lives. A quiet, on-track day
    with a modest, unremarkable cadence (3 workouts, no stated goal to be above/below),
    NO episodic life-context, no event, nothing standing — there is no specific warm
    material. A friend does NOT manufacture a 'nice, 3 workouts!' text out of nothing.
    Must stay silent. If the warmth pass over-loosened, this is where it shows. Binary."""
    import config, heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "EPISODIC_ENABLED", True)  # available but EMPTY (no notes)
    user = make_user(db, name="Sam",
                     coaching_summary="Trains regularly, no specific cadence goal. Doing fine, nothing notable.")
    for d, t in ((1, "push"), (3, "pull"), (5, "legs")):  # ordinary cadence, not a streak
        _seed_workout(user.id, d, t)

    spoke, payload, _search = heartbeat.decide(user.id)
    print(f"\n[HEARTBEAT anti-bot no-anchor] spoke={spoke} :: {payload!r}")
    assert spoke is False, (
        "ANTI-BOT ANCHOR FAILED: the coach manufactured a warm text with no specific "
        "material behind it — a modest 3-workout week is not a win, and there was nothing "
        f"standing. This is generic engagement bait, the failure mode. Message: {payload!r}")


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
