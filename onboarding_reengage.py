"""Stalled-onboarding re-engagement (code-owned, flag-gated).

Live 2026-10-15/16: fifteen real sign-ups in one night; one finished setup. Eleven stalled
mid-intake (most within minutes — a question went out, no answer came back) and NOTHING
contacted them again: the heartbeat only serves onboarded users (step 3), and the
onboarding agent only speaks when they do. Every stalled account sat silent for four days
until the founder nudged a few by hand.

This sweep sends at most TWO nudges per stalled user, ~20h and ~72h after they went quiet,
inside 10:00–21:00 local, one bubble each, always ending in the next plain intake ask so a
reply flows straight back into handle_onboarding_reply. Never to archived, opted-out, or
pending-waitlist users. Idempotent: the count and timing come from the user's own
outbound rows typed MESSAGE_TYPE, so a deploy mid-window can't double-send.

Founder, 2026-10-24/25 (the planned attack): the first nudge leads with WHY they signed up
(their goal, their own words), says starting is the hard part and the coach is here, and
lands on the one easiest, highest-ROI thing they can do right now — log today's food — as
the only ask, phrased for the time of day. No setup questions. The second nudge is smaller
and easier. Model-written per person from a brief (template fallback when the output fails
the shape checks). And the path underneath is rebuilt: a mid-setup user's reply to that
nudge routes to the agent loop (food-first window: log_meal + the numbers), and code asks
the next intake field only once a meal is logged, with the reason attached.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config
from models import get_session, User, Message

logger = logging.getLogger("cued.onboarding_reengage")

MESSAGE_TYPE = "onboarding_nudge"
MORE_TYPE = "onboarding_nudge_more"     # a nudge's second bubble — never counted as a nudge
MAX_NUDGES = 2
LOCAL_HOURS = (10, 21)          # send only between 10:00 and 20:59 local
MAX_FOOD_FIRST_TURNS = 4        # diverted turns after a nudge before intake takes over again
BANNED = ("fitness journey", "if you're serious", "if ur serious", "if youre serious", "lock in or", "that's on me", "my bad", "sorry")
_FOOD_RE = r"(?:\beat(?:en|ing)?\b|\bate\b|\bfood\b|\bmeal\b|\bbreakfast\b|\blunch\b|\bdinner\b|\bpic\b|\bsnack)"
_SETUP_RE = r"(?:height|weight|major|steps|how many days|what time|split)"
_GOAL_WORDS = {"muscle_building": "build muscle", "fat_loss": "lean out", "strength": "get stronger",
               "general_fitness": "get in shape", "endurance": "build endurance"}


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_hour(user, now: datetime) -> int:
    from timefmt import resolve_tz
    return now.replace(tzinfo=timezone.utc).astimezone(resolve_tz(user)).hour


def _nudges(session, user_id: int) -> list:
    return (session.query(Message.created_at)
            .filter(Message.user_id == user_id, Message.direction == "out", Message.message_type == MESSAGE_TYPE)
            .order_by(Message.created_at).all())


def _last_inbound(session, user_id: int):
    return (session.query(Message.created_at)
            .filter(Message.user_id == user_id, Message.direction == "in")
            .order_by(Message.created_at.desc()).first())


def _last_outbound_any(session, user_id: int):
    return (session.query(Message.created_at)
            .filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.created_at.desc()).first())


def _remaining(user) -> list:
    from onboarding_agent import _get_missing_fields
    return _get_missing_fields(user)


def _next_ask(user) -> str:
    from onboarding_agent import _plain, _BIG_ASK_NEVER
    missing = _remaining(user)
    field = next((f for f in missing if f[0] not in _BIG_ASK_NEVER), missing[0] if missing else None)
    return _plain(field) if field else "how's ur day going"


def _first_name(user) -> str:
    return (user.name or "").split()[0] if user.name else ""


def goal_phrase(user) -> str:
    """'build muscle' / 'lean out and get stronger' / 'get stronger, leaner and fitter'."""
    raw = [g.strip() for g in (getattr(user, "goal", None) or "").split(",") if g.strip()]
    words = [_GOAL_WORDS.get(g, g.replace("_", " ")) for g in raw]
    if not words:
        return "get in shape"
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return f"{words[0]} and {words[1]}"
    return "get stronger, leaner and fitter"


def food_ask(user, now: datetime | None = None) -> str:
    """The one ask, for the hour: the easiest win is today's food."""
    h = _local_hour(user, now or _naive_utcnow())
    if h < 10:
        return "send me what u eat today as u go, pic or just text it, and i keep count. what's breakfast?"
    if h < 16:
        return "what have u eaten so far today? pic or text, i'll count it up"
    return "what did u eat today so far? a pic of dinner works too"


def own_words(session, user_id: int, limit: int = 6) -> list[str]:
    """What they said during intake, their words — skipping the 'hey cued' hook."""
    rows = (session.query(Message.body).filter(Message.user_id == user_id, Message.direction == "in")
            .order_by(Message.created_at.desc()).limit(12).all())
    out = []
    for (b,) in rows:
        t = " ".join((b or "").split())
        if len(t) < 3 or t.lower().strip("!. ") in ("hey cued", "hi cued", "hello", "hi", "hey"):
            continue
        out.append(t[:120])
        if len(out) >= limit:
            break
    return list(reversed(out))


def nudge_text(user, n: int, *, replied: bool, now: datetime | None = None) -> str:
    """The template (fallback for #1, the copy for #2). The founder's structure: why they
    signed up → starting is the hard part, i'm here → the ONE ask: today's food."""
    name = _first_name(user)
    hey = f"hey {name}" if name else "hey"
    ask = food_ask(user, now)
    if n >= 2:
        small = {"what's breakfast?": "what's breakfast? text or pic",
                 }.get(ask.split(". ")[-1], "what's the next thing u eat? just send a pic")
        return f"{hey}, last one from me — easiest start there is: {small}. no stress if not"
    goal = goal_phrase(user)
    if not replied:
        return (f"{hey}, it's cued — u signed up to {goal}, so u want this. starting is always the hard part, "
                f"and that's the part i'm here for. {ask}")
    return (f"{hey}, u signed up to {goal}, so i know u want this. getting started is always the hard part — "
            f"i'm here, u just gotta lock in. let's start with the easiest thing: {ask}")


def _bubbles(text: str) -> list[str]:
    return [b.strip() for b in (text or "").split("\n---\n") if b.strip()]


def valid_first_nudge(text: str | None) -> bool:
    """Shape checks on model copy: ≤2 bubbles, short, lowercase-friend, ends on the food ask,
    no setup questions, none of the banned phrases."""
    import re
    bs = _bubbles(text or "")
    if not bs or len(bs) > 2:
        return False
    joined = " ".join(bs)
    if len(joined) > 380 or "?" not in bs[-1]:
        return False
    low = joined.lower()
    if any(b in low for b in BANNED) or "!" in joined:
        return False
    if not re.search(_FOOD_RE, bs[-1], re.I) or re.search(_SETUP_RE, low):
        return False
    return True


def model_first_nudge(session, user, *, replied: bool, now: datetime | None = None) -> str | None:
    """Per-person copy over the brief; None when the call fails or the shape is off (the
    sweep falls back to nudge_text)."""
    if not getattr(config, "ONBOARDING_REENGAGE_MODEL_COPY_ENABLED", True):
        return None
    try:
        from agent_loop import identity_prompt, _voice_prompt, _join_text
        from llm_client import make_client
        from cost_tracking import track
        now = now or _naive_utcnow()
        name = _first_name(user) or "them"
        created = getattr(user, "created_at", None)
        days = max(0, int((now - created.replace(tzinfo=None)).total_seconds() // 86400)) if created else None
        said = own_words(session, user.id)
        said_line = ("What they told you during setup, their words: " + " | ".join(f'"{w}"' for w in said)) if said else \
                    "They never answered the first question, so you only have the sign-up form."
        ask = food_ask(user, now)
        instruction = (
            f"Write the coach's first re-engagement text to {name}. They signed up for cued"
            f"{f' {days} days ago' if days is not None else ''} to {goal_phrase(user)} and stalled during setup "
            f"({'answered a question or two, then went quiet' if replied else 'never replied to the first text'}).\n"
            f"{said_line}\n\n"
            f"STRUCTURE — keep this order, 1–2 short bubbles, under 320 characters total:\n"
            f"1) remind them why they signed up — their goal, and if they told you something real (how they eat, "
            f"what they already train, their situation) use THAT, in a line a friend would text. 'u signed up so u want this' energy.\n"
            f"2) getting started is always the hard part, and that's what you're here for. u just gotta lock in. "
            f"Belief in them, not a dare — never 'if you're serious', never 'fitness journey', never guilt.\n"
            f"3) the ONLY ask, the easiest win: logging today's food. End on this ask in your own words, as a question: "
            f"\"{ask}\"\n\n"
            f"No setup questions (no height, weight, major, steps, days, split) — those come after their first logged "
            f"meal. Open with 'hey {name},' (lowercase, a comma, no exclamation mark). No emojis, no exclamation "
            f"marks, 'u'/'ur'. Never apologize or take blame ('that's on me', 'we never got going') — nothing went "
            f"wrong, they just haven't started. No stock filler ('that's still the move'); say something specific "
            f"about them or skip straight to the next beat. "
            f"Return only the text. Separate the two bubbles with a line containing just ---."
        )
        system = [{"type": "text", "text": identity_prompt() + "\n\n---\n\n" + _voice_prompt(),
                   "cache_control": {"type": "ephemeral"}}]
        client = make_client()
        resp = client.messages.create(model=config.AGENT_LOOP_MODEL, max_tokens=400, system=system,
                                      messages=[{"role": "user", "content": instruction}])
        try:
            track(user.id, "onboarding.reengage", config.AGENT_LOOP_MODEL, resp)
        except Exception:  # noqa: BLE001
            pass
        if getattr(resp, "stop_reason", None) == "max_tokens":
            return None
        text = (_join_text(resp.content) or "").strip()
        if not valid_first_nudge(text):
            logger.info("ONBOARDING_REENGAGE_MODEL_COPY_REJECTED user=%s text=%r", user.id, text[:160])
            return None
        return text
    except Exception as e:  # noqa: BLE001 — the template is the floor
        logger.warning("ONBOARDING_REENGAGE_MODEL_COPY_FAILED user=%s err=%s", user.id, e)
        return None


# ─── the food-first window: a mid-setup user answering the nudge goes to the loop ───

def last_nudge_at(session, user_id: int):
    rows = _nudges(session, user_id)
    return rows[-1][0] if rows else None


def food_first_active(session, user, now: datetime | None = None) -> bool:
    """True while a nudged, still-mid-setup user should be answered by the agent loop
    (log_meal + the numbers) instead of the intake agent: a nudge went out within
    ONBOARDING_REENGAGE_FOOD_FIRST_HOURS, no meal has been logged since, and fewer than
    MAX_FOOD_FIRST_TURNS of their texts have been diverted."""
    if not getattr(config, "ONBOARDING_REENGAGE_FOOD_FIRST_ENABLED", True):
        return False
    if (user.onboarding_step or 0) >= 3:
        return False
    last = last_nudge_at(session, user.id)
    if not last:
        return False
    now = now or _naive_utcnow()
    if now - last > timedelta(hours=getattr(config, "ONBOARDING_REENGAGE_FOOD_FIRST_HOURS", 48)):
        return False
    from models import Meal
    if session.query(Meal.id).filter(Meal.user_id == user.id, Meal.logged_at > last).first():
        return False
    turns = (session.query(Message.id).filter(Message.user_id == user.id, Message.direction == "in",
                                              Message.created_at > last).count())
    return turns <= MAX_FOOD_FIRST_TURNS


def meal_logged_since(session, user_id: int, since: datetime) -> bool:
    from models import Meal
    return session.query(Meal.id).filter(Meal.user_id == user_id, Meal.logged_at > since).first() is not None


def context_block(user, session=None) -> str | None:
    """The block the loop sees during the food-first window."""
    own = session is None
    session = session or get_session()
    try:
        if not food_first_active(session, user):
            return None
    finally:
        if own:
            session.close()
    name = _first_name(user) or "they"
    return (f"## RE-ENGAGED, STILL MID-SETUP\n{name} signed up to {goal_phrase(user)}, stalled during setup, and you "
            f"just texted them to start with the easiest win: logging what they eat today. Setup isn't done, so "
            f"there are NO targets yet — no 'remaining', no daily number, no 'ur on track'.\n"
            f"- They sent food (text or a photo) → log_meal it and reply with the numbers in ONE short line like a "
            f"friend: what it was, ~cal and protein. That's the win; make it feel easy.\n"
            f"- They said yes / asked how it works → ask for today's food: {food_ask(user)}\n"
            f"- They asked something else → answer it, then bring it back to today's food.\n"
            f"Don't run setup questions here (no height / weight / major / steps / days / split) — code asks the "
            f"next one itself once a meal is logged. One or two short bubbles.")


def intake_hook_line(user) -> str:
    """Code's line after the first logged meal: the reason to finish setup, then the next ask."""
    return (f"that's it, u just text me or send a pic and i keep count. to tell u how much u actually need in a "
            f"day i need a few quick things — first: {_next_ask(user)}?")


def eligible(session, user, now: datetime) -> tuple[bool, str, int, bool]:
    """(eligible, reason, next_nudge_number, replied_ever)."""
    if (user.onboarding_step or 0) >= 3:
        return False, "onboarded", 0, False
    if user.archived_at or getattr(user, "opted_out", False) or user.waitlist_status == "pending":
        return False, "excluded", 0, False
    if not user.preferred_channel and not user.phone:
        return False, "no_channel", 0, False
    sent = _nudges(session, user.id)
    if len(sent) >= MAX_NUDGES:
        return False, "max_nudges", 0, False
    last_in = _last_inbound(session, user.id)
    last_out = _last_outbound_any(session, user.id)
    if last_out is None:
        return False, "never_contacted", 0, False       # the hook hasn't gone out yet — not ours
    replied = last_in is not None
    quiet_since = max([t[0] for t in (last_in, last_out) if t])   # the thread's last word, either side
    first_gap = timedelta(hours=config.ONBOARDING_REENGAGE_FIRST_HOURS)
    second_gap = timedelta(hours=config.ONBOARDING_REENGAGE_SECOND_HOURS)
    if not sent:
        if now - quiet_since < first_gap:
            return False, "too_soon", 1, replied
        return True, "first", 1, replied
    # second nudge: measured from the first nudge, and only if they stayed silent after it
    if last_in is not None and last_in[0] > sent[-1][0]:
        return False, "replied_after_nudge", 2, replied
    if now - sent[-1][0] < second_gap:
        return False, "too_soon", 2, replied
    return True, "second", 2, replied


def sweep(now: datetime | None = None) -> int:
    if not config.ONBOARDING_REENGAGE_ENABLED:
        return 0
    now = now or _naive_utcnow()
    from sms import send_sms
    session = get_session()
    sent_n = 0
    try:
        users = (session.query(User)
                 .filter(User.onboarding_step < 3, User.archived_at.is_(None))
                 .all())
        for u in users:
            ok, reason, n, replied = eligible(session, u, now)
            if not ok:
                continue
            h = _local_hour(u, now)
            if not (LOCAL_HOURS[0] <= h < LOCAL_HOURS[1]):
                logger.info("ONBOARDING_REENGAGE_DEFERRED user=%s n=%s reason=local_hour=%s", u.id, n, h)
                continue
            text = (model_first_nudge(session, u, replied=replied, now=now) if n == 1 else None) \
                or nudge_text(u, n, replied=replied, now=now)
            try:
                bubbles = _bubbles(text) or [text]
                send_sms(u.phone, bubbles[0], user_id=u.id, message_type=MESSAGE_TYPE)
                for extra in bubbles[1:]:
                    send_sms(u.phone, extra, user_id=u.id, message_type=MORE_TYPE)
                sent_n += 1
                logger.info("ONBOARDING_REENGAGE_SENT user=%s n=%s replied_before=%s bubbles=%s text=%r",
                            u.id, n, replied, len(bubbles), bubbles[0][:90])
            except Exception as e:  # noqa: BLE001 — one failure must not stop the sweep
                logger.warning("ONBOARDING_REENGAGE_FAILED user=%s err=%s", u.id, e)
    finally:
        session.close()
    return sent_n
