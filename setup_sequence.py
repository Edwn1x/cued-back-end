"""
Setup sequence — what happens after the summary (founder, 2026-10-09).

The founder's own run (user 48, 2026-10-05) ended onboarding with three bubbles in one
second (summary, "what do u bench", the rundown), then seven more and a workout card at
11:46pm, then "So what now" twice and the coach saying "start the card" to someone at
home. The order is now:

  summary (+ profile link) → rundown            immediately, at completion (onboarding_agent)
  → connect offers: calendar, bcourses, one step at a time (connect_offers; the wearable
    waits for Google's API approval — GOOGLE_HEALTH_OFFER_ENABLED)
  → the water yes/no (water_offer) — quick, code-answered (founder: before the card)
  → the first card: extension pitch, lift ask / card, tour     LAST (workouts/card_setup)

One step at a time: the next step goes when the previous one was ANSWERED (an inbound
after it) or the conversation went quiet for SETUP_STEP_QUIET_MINUTES. Two triggers:
  - on_inbound(user_id): right after the coach's reply to their text, while engaged.
  - sweep(): every 10 min inside the heartbeat's guardrails (quiet hours, active
    conversation, budget), for the died-down case. Inside the setup window THIS sweep
    is the only sender (connect, water, card — one per tick): live 2026-10-10 11:19
    (user 49) connect_offers.sweep and water_offer.sweep fired in the same tick, water
    landing 5s before the calendar offer. Those sweeps skip users `owns()` is true for.
A workout ask at any point sends the card right then (the onboarding early exit, or the
start tool), which simply marks the card step done. Everything is once-only by the
existing ledgers (users.connect_offers, users.card_setup_at); nothing here repeats.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

import config

logger = logging.getLogger("cued.setup_sequence")

# Outbound types that are setup steps: the anchor for "was the previous step answered".
STEP_TYPES = ("program_brief", "connect_offer", "connect_link", "water_offer", "card_setup", "workout_intro", "workout_card")


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return bool(getattr(config, "SETUP_SEQUENCE_ENABLED", True))


def owns(user, now: datetime | None = None) -> bool:
    """True while the setup sequence is the only thing allowed to send setup steps to
    this user: the sequence is on, its sweep is registered (CARD_SETUP_ENABLED), they're
    inside the setup window, and the card (the last step) hasn't gone. water_offer.sweep
    and connect_offers.sweep skip these users; on_inbound/sweep here send one step at a
    time in the decided order (offers → water → card)."""
    if not (enabled() and config.CARD_SETUP_ENABLED):
        return False
    if getattr(user, "card_setup_at", None):
        return False
    from connect_offers import in_setup_window
    return in_setup_window(user, now or _naive_utcnow())


def _briefing_owed(session, user, now: datetime) -> bool:
    """True while the heartbeat still owes this user today's morning open. A setup step
    sent now trips the anti-stack gate for 180 min and pushes the briefing out of its
    90-min window — live 2026-10-12 (user 50, day two): water offer at wake+23 from this
    sweep, every heartbeat tick after it 'proactive_stack', no briefing. The briefing goes
    first; the step follows once they reply or the stack window lapses."""
    if not getattr(config, "SETUP_SEQUENCE_YIELDS_TO_BRIEFING", True):
        return False
    try:
        from heartbeat import _morning_open_signal
        return _morning_open_signal(user, session, now=now) is not None
    except Exception as e:  # noqa: BLE001 — never block setup on a hint
        logger.warning("SETUP_BRIEFING_CHECK_FAILED user=%s err=%s", getattr(user, "id", "?"), e)
        return False


def _card_due(session, user) -> bool:
    if not (config.CARD_SETUP_ENABLED and config.START_WORKOUT_TOOL_ENABLED):
        return False
    if getattr(user, "card_setup_at", None):
        return False
    from sms import _resolve_channel
    if _resolve_channel(user.id) != "imessage":
        return False
    from workouts.session_ops import active_session_id
    if active_session_id(user.id) is not None:
        return False
    # The first-card ASK is on the floor (no anchors → "what do u bench and squat for like
    # 5?"): its answer sends the card (calibrate.handle_pending_card_reply). Asked ONCE —
    # live 2026-10-10 12:09 and 12:39 (user 49) the quiet-settle rule re-asked it verbatim.
    from workouts.calibrate import peek_pending_setup
    return not peek_pending_setup(user.id)


def _water_due(session, user) -> bool:
    try:
        from water_offer import eligible, _has_interval_reminder
        return eligible(user) and not _has_interval_reminder(session, user.id)
    except Exception:  # noqa: BLE001
        return False


def _previous_step_settled(session, user, now: datetime) -> bool:
    """The last setup step (or the completion itself) has an inbound after it, or is
    older than SETUP_STEP_QUIET_MINUTES."""
    from models import Message
    anchor = getattr(user, "onboarding_completed_at", None)
    last_step = (session.query(Message.created_at)
                 .filter(Message.user_id == user.id, Message.direction == "out",
                         Message.message_type.in_(STEP_TYPES))
                 .order_by(Message.id.desc()).first())
    if last_step and last_step[0] and (anchor is None or last_step[0] > anchor):
        anchor = last_step[0]
    if anchor is None:
        return True
    if now - anchor >= timedelta(minutes=getattr(config, "SETUP_STEP_QUIET_MINUTES", 20)):
        return True
    answered = (session.query(Message.id)
                .filter(Message.user_id == user.id, Message.direction == "in",
                        Message.created_at > anchor).first())
    return answered is not None


def step_lead(session, user) -> str:
    """The directive lead for the next step: "first thing:" until a setup step has gone out
    since completion, then "next:". ('' when directive steps are off.)"""
    if not getattr(config, "SETUP_STEPS_DIRECTIVE_ENABLED", True):
        return ""
    from models import Message
    anchor = getattr(user, "onboarding_completed_at", None)
    q = (session.query(Message.id)
         .filter(Message.user_id == user.id, Message.direction == "out", Message.message_type.in_(STEP_TYPES)))
    if anchor is not None:
        q = q.filter(Message.created_at > anchor)
    return "next:" if q.first() is not None else "first thing:"


def next_step(session, user, now: datetime | None = None, *, require_settled: bool = True) -> str | None:
    """'connect' | 'water' | 'card' | None — the one thing setup still owes this user, when
    the previous step is settled (require_settled=False: right at completion, the summary
    itself is the previous step). Pure read."""
    if not enabled():
        return None
    now = now or _naive_utcnow()
    if (user.onboarding_step or 0) < 3 or not user.active:
        return None
    if config.STOP_OPTOUT_ENABLED and getattr(user, "opted_out", False):
        return None
    from connect_offers import in_setup_window, first_offer_candidates
    if not in_setup_window(user, now):
        return None
    if require_settled and not _previous_step_settled(session, user, now):
        return None
    if first_offer_candidates(session, user):
        return "connect"
    if _water_due(session, user):
        return "water"
    if _card_due(session, user):
        return "card"
    return None


def run_step(user_id: int, step: str, now: datetime | None = None, *, trigger: str) -> str:
    """Send one step. Returns what happened (for the log)."""
    now = now or _naive_utcnow()
    if step == "connect":
        from connect_offers import offer_now
        from models import get_session, User
        session = get_session()
        try:
            u = session.get(User, user_id)
            lead = step_lead(session, u) if u else ""
        finally:
            session.close()
        provider = offer_now(user_id, now, min_gap=timedelta(0), lead=lead or None)
        result = f"connect:{provider}" if provider else "connect:none"
    elif step == "water":
        from water_offer import send_offer
        result = f"water:{'sent' if send_offer(user_id, source='setup') else 'skipped'}"
    elif step == "card":
        from workouts.card_setup import run_onboarding_setup
        result = f"card:{run_onboarding_setup(user_id)}"
    else:
        result = "noop"
    logger.info("SETUP_STEP user=%s step=%s trigger=%s result=%s", user_id, step, trigger, result)
    return result


def kickoff(user_id: int) -> str | None:
    """Right after the summary (founder, 2026-10-12): the first setup step goes out as a
    thing to do — no rundown, no waiting for their next text. The summary is the previous
    step, so settledness is not required. Never raises."""
    if not (enabled() and getattr(config, "SETUP_STEPS_DIRECTIVE_ENABLED", True)):
        return None
    try:
        from models import get_session, User
        session = get_session()
        try:
            u = session.get(User, user_id)
            step = next_step(session, u, require_settled=False) if u else None
        finally:
            session.close()
        if not step:
            logger.info("SETUP_KICKOFF user=%s step=none", user_id)
            return None
        return run_step(user_id, step, trigger="completion")
    except Exception as e:  # noqa: BLE001
        logger.warning("SETUP_KICKOFF_FAILED user=%s err=%s", user_id, e)
        return None


_PROBLEM_RE = re.compile(r"\b(40[134]|500|error|not working|doesn'?t work|isn'?t working|broke|broken|bug|glitch|"
                         r"failed|fail|can'?t open|won'?t open|wrong|issue)\b", re.I)


def _last_inbound_is_a_problem_report(session, user_id: int) -> bool:
    """Live 2026-10-13 05:13 (user 51): "I got a 401 error?" on the calendar link → the coach
    replied, then this ran and stacked the water offer on top of an unresolved problem.
    A problem report is not the moment to advance setup."""
    from models import Message
    row = (session.query(Message.body).filter(Message.user_id == user_id, Message.direction == "in")
           .order_by(Message.id.desc()).first())
    return bool(row and row[0] and _PROBLEM_RE.search(row[0]))


def on_reaction(user_id: int) -> str | None:
    """A tapback on the coach's message is engagement too (live 2026-10-13 05:31, user 51:
    👍 on "connected. i'll pull ur calendar in…" — then nothing, because a reaction is not a
    Message row, so on_inbound never ran and the previous step never read as answered, and
    the sweep sat in his quiet hours). The reaction IS the answer: advance one step."""
    if not getattr(config, "SETUP_ADVANCES_ON_REACTION", True):
        return None
    try:
        from models import get_session, User
        session = get_session()
        try:
            u = session.get(User, user_id)
            step = next_step(session, u, require_settled=False) if u else None
        finally:
            session.close()
        if not step:
            return None
        return run_step(user_id, step, trigger="reaction")
    except Exception as e:  # noqa: BLE001
        logger.warning("SETUP_SEQUENCE_REACTION_FAILED user=%s err=%s", user_id, e)
        return None


def on_inbound(user_id: int) -> str | None:
    """After the coach's reply to their text: advance one step if one is owed. Never
    raises; never blocks the turn."""
    try:
        from models import get_session, User
        session = get_session()
        try:
            u = session.get(User, user_id)
            if u and _last_inbound_is_a_problem_report(session, user_id):
                logger.info("SETUP_STEP_DEFERRED user=%s reason=problem_report", user_id)
                return None
            step = next_step(session, u) if u else None
        finally:
            session.close()
        if not step:
            return None
        return run_step(user_id, step, trigger="inbound")
    except Exception as e:  # noqa: BLE001
        logger.warning("SETUP_SEQUENCE_INBOUND_FAILED user=%s err=%s", user_id, e)
        return None


def sweep(now: datetime | None = None) -> int:
    """The died-down path: every 10 min, inside the heartbeat's guardrails, ONE step per
    user per tick in the decided order — connect offer(s), then water, then the card.
    Inside the setup window this is the only sender (see owns()); connect_offers.sweep
    and water_offer.sweep take over again once the window closes or the card went."""
    if not (enabled() and config.CARD_SETUP_ENABLED):
        return 0
    now = now or _naive_utcnow()
    from models import get_session, User
    from heartbeat import guardrail_reason
    session = get_session()
    todo = []
    try:
        since = now - timedelta(hours=getattr(config, "SETUP_WINDOW_HOURS", 48))
        users = (session.query(User)
                 .filter(User.active.is_(True), User.onboarding_step >= 3,
                         User.card_setup_at.is_(None), User.onboarding_completed_at >= since).all())
        for u in users:
            if guardrail_reason(u, session, now=now):
                continue
            if _briefing_owed(session, u, now):
                logger.info("SETUP_STEP_DEFERRED user=%s reason=briefing_owed", u.id)
                continue
            step = next_step(session, u, now)
            if step in ("connect", "water", "card"):
                todo.append((u.id, step))
    finally:
        session.close()
    sent = 0
    for uid, step in todo:
        try:
            run_step(uid, step, now, trigger="sweep")
            sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("SETUP_SEQUENCE_SWEEP_FAILED user=%s err=%s", uid, e)
    return sent
