"""
Capability registry — the ONE list of what the coach can do for a user.

Two consumers:
  1. The post-onboarding rundown (onboarding_agent._send_capability_rundown): a
     second bubble after the kickoff, written by the model from the top few
     entries for THIS user, in the friend's voice — never a feature list.
  2. The coach loop's "THINGS THEY HAVEN'T USED YET" context block (agent_loop):
     up to three unused capabilities, so the coach can mention one when the
     moment calls for it (voice.md), never as a list.

MAINTENANCE RULE (founder, 2026-09-14: "as we add features that message needs
to keep up"): every user-facing thing the coach can do gets ONE entry here —
what it does in the user's words, which tools/flags it rides on, whether it's
on for this user, how relevant it is to their profile, and how we'd know they
have used it. tests/tier1/test_capabilities.py fails the build if the coach
loop offers a tool that no entry claims, so a new tool cannot ship without a
line in the rundown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import config


@dataclass
class Capability:
    id: str
    # In the user's words, present tense, as the coach would say it. One clause.
    what: str
    # How to use it — what the user actually does. One clause.
    how: str
    # Tool names (agent_tools *_TOOL["name"]) this capability rides on. Coverage test.
    tools: tuple = ()
    # Is it on for this user? Flags + channel. Default: always.
    enabled: Callable = lambda user: True
    # 0–10, how much THIS user's profile makes it worth mentioning first.
    relevance: Callable = lambda user: 5
    # Evidence they've used it (session, user) → bool. None = no durable evidence;
    # such capabilities are always eligible for a contextual mention.
    used: Callable | None = None
    # A hint for the reveal moment (voice.md): when to bring it up.
    reveal_when: str = ""


def _has(user, attr):
    return bool(getattr(user, attr, None))


def _goal_has(user, *keys):
    g = (getattr(user, "goal", "") or "")
    return any(k in g for k in keys)


def _is_imessage(user):
    try:
        from sms import _resolve_channel
        return _resolve_channel(user.id) == "imessage"
    except Exception:  # noqa: BLE001
        return False


def _meals_logged(session, user):
    from models import Meal, active
    return active(session, Meal, user_id=user.id).first() is not None


def _app_meals_logged(session, user):
    from models import Meal, active
    return active(session, Meal, user_id=user.id).filter(Meal.source == "app").first() is not None


def _workouts_logged(session, user):
    from models import Workout, active
    return active(session, Workout, user_id=user.id).first() is not None


def _events_logged(session, user):
    from models import Event, active
    return active(session, Event, user_id=user.id).first() is not None


def _reminders_set(session, user):
    from models import Reminder
    return session.query(Reminder.id).filter(Reminder.user_id == user.id).first() is not None


def _weighed_in(session, user):
    from models import WeightLog
    return session.query(WeightLog.id).filter(WeightLog.user_id == user.id).first() is not None


def _app_meal_logged(session, user):
    from models import Meal, active
    return active(session, Meal, user_id=user.id).filter(Meal.source == "app").first() is not None


def _sent_a_photo(session, user):
    from models import Message
    from sms import IMAGE_MARKER
    return (session.query(Message.id)
            .filter(Message.user_id == user.id, Message.direction == "in",
                    Message.body.contains(IMAGE_MARKER)).first() is not None)


CAPABILITIES: list[Capability] = [
    Capability(
        id="log_meals",
        what="i log what you eat and square it against your calorie and protein targets",
        how="text me what you ate — a pic of the plate works too — and i handle the numbers",
        tools=("log_meal", "match_meal_history", "usda_food_lookup"),
        enabled=lambda u: config.LOG_MEAL_TOOL_ENABLED,
        relevance=lambda u: 10 if _goal_has(u, "fat_loss", "muscle") else 8,
        used=_meals_logged,
        reveal_when="they mention eating something without logging it",
    ),
    Capability(
        id="food_photos",
        what="a photo of your food is enough — i read it and log it",
        how="send the pic, caption optional",
        tools=(),  # rides on log_meals + vision
        enabled=lambda u: config.LOG_MEAL_TOOL_ENABLED and getattr(config, "READ_IMAGE_ENABLED", True),
        relevance=lambda u: 7,
        used=_sent_a_photo,
        reveal_when="they describe a plate in words when a photo would be quicker",
    ),
    Capability(
        id="app_screenshot",
        what="still logging in myfitnesspal or another app? screenshot the meal or the day and i take the numbers as printed",
        how="send the screenshot of your app's diary — no re-typing, no connection needed",
        tools=(),  # rides on log_meals (from_app) + vision
        enabled=lambda u: config.LOG_MEAL_TOOL_ENABLED and getattr(config, "READ_IMAGE_ENABLED", True),
        relevance=lambda u: 6,
        used=_app_meal_logged,
        reveal_when="they mention another food app, or ask to connect one",
    ),
    Capability(
        id="log_workouts",
        what="i keep your training log and track where you are in your split",
        how="say 'starting push' and a card shows up you tap as you go — or just tell me what you hit",
        tools=("log_workout", "start_workout_session", "save_routine"),
        enabled=lambda u: config.LOG_WORKOUT_TOOL_ENABLED,
        relevance=lambda u: 9 if _has(u, "current_split") else 7,
        used=_workouts_logged,
        reveal_when="they mention a session they didn't tell you the details of",
    ),
    Capability(
        id="dining_halls",
        what="i know the berkeley dining hall menus and can pick what fits your day",
        how="ask 'what's good at crossroads tonight' or 'what should i get at foothill'",
        tools=("get_dining_menu", "match_dining_item"),
        enabled=lambda u: config.GET_DINING_MENU_TOOL_ENABLED,
        relevance=lambda u: 9 if "dining" in (getattr(u, "cooking_situation", "") or "").lower() else 3,
        used=None,
        reveal_when="they mention a dining hall or ask what to eat on campus",
    ),
    Capability(
        id="receipts",
        what="send me a grocery receipt and i'll log what you've got so dinner ideas use it",
        how="a photo of the receipt — i read the lines; 'out of chicken' or 'what do i have' keeps it current",
        tools=(),
        enabled=lambda u: config.RECEIPTS_ENABLED and config.READ_IMAGE_ENABLED,
        relevance=lambda u: 7 if "cook" in (getattr(u, "cooking_situation", "") or "").lower() else 4,
        used=lambda session, u: session.query(__import__("models").PantryItem.id).filter_by(user_id=u.id).first() is not None,
        reveal_when="they mention groceries, a store run, or not knowing what to cook",
    ),
    Capability(
        id="cook_from_fridge",
        what="i'll tell you what to cook with what you've got",
        how="tell me what's in the fridge and how long you've got",
        tools=(),
        enabled=lambda u: True,
        relevance=lambda u: 8 if "cook" in (getattr(u, "cooking_situation", "") or "").lower() else 4,
        used=None,
        reveal_when="they say they don't know what to make or are about to order out",
    ),
    Capability(
        id="rsf_line",
        what="when rsf's packed and you're heading over, i'll send you the virtual-line link so you're in before you get there",
        how="just text me 'heading to the gym' — if the line's on you get the link, one tap",
        tools=(),
        enabled=lambda u: config.RSF_METER_ENABLED,
        relevance=lambda u: 6,
        used=lambda session, u: session.query(__import__("models").Message.id).filter_by(user_id=u.id, direction="out", message_type="gym_line_d1").first() is not None,
        reveal_when="they complain the gym's packed or ask about the crowd",
    ),
    Capability(
        id="campus_lookup",
        what="i can look things up for you — gym hours, a place, a class time",
        how="just ask, like 'what time does rsf close'",
        tools=("web_search",),  # the server-side search tool (agent_tools.WEB_SEARCH_TOOL)
        enabled=lambda u: config.WEB_SEARCH_TOOL_ENABLED,
        relevance=lambda u: 6,
        used=None,
        reveal_when="they wonder aloud about hours, a place, or a schedule",
    ),
    Capability(
        id="remembers",
        what="i remember what you tell me — classes, injuries, what you like, who you went with",
        how="just talk to me like a person; you never have to repeat yourself",
        tools=("remember",),
        enabled=lambda u: config.REMEMBER_TOOL_ENABLED,
        relevance=lambda u: 5,
        used=None,
        reveal_when="never as a pitch — show it by using it",
    ),
    Capability(
        id="reminders",
        what="i can text you at a set time — after class to go run, at 7 to take creatine",
        how="say 'remind me to X after class' or 'ping me at 7' and it'll show up on time",
        tools=("set_reminder", "cancel_reminder"),
        enabled=lambda u: config.REMINDERS_ENABLED,
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower() == "student" else 4,
        used=_reminders_set,
        reveal_when="they mention forgetting things, or a fixed daily/weekly slot they want held",
    ),
    Capability(
        id="calendar",
        what="i can hold dates — a midterm, a trip, a game — and plan around them",
        how="tell me 'midterm thursday' or 'home this weekend' and i'll work with it",
        tools=("log_event",),
        enabled=lambda u: config.LOG_EVENT_TOOL_ENABLED,
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower() == "student" else 4,
        used=_events_logged,
        reveal_when="they mention something coming up with a date",
    ),
    Capability(
        id="weigh_ins",
        what="tell me your weight now and then and i'll track the trend and tune your calories off real data",
        how="'weighed in at 141' or a scale screenshot; once a week is plenty",
        tools=("log_weight",),
        enabled=lambda u: config.LOG_WEIGHT_TOOL_ENABLED and not getattr(u, "weigh_in_opt_out", False),
        relevance=lambda u: 7 if _goal_has(u, "fat_loss", "muscle") else 4,
        used=lambda session, u: _weighed_in(session, u),
        reveal_when="they mention the scale, their weight, or ask if the numbers are right",
    ),
    Capability(
        id="fix_a_log",
        what="if i log something wrong you can just tell me and i fix it",
        how="'that wasn't 850 cal' or 'delete that workout'",
        tools=("manage_log",),
        enabled=lambda u: config.MANAGE_LOG_TOOL_ENABLED,
        relevance=lambda u: 4,
        used=None,
        reveal_when="right after a log they might want to correct",
    ),
    Capability(
        id="day_reset",
        what="if you eat late and count it as the night before, i can roll your day over on your schedule",
        how="just say it — 'count my after-midnight meals as yesterday' or 'my day starts at 4am'",
        tools=("set_day_reset",),
        enabled=lambda u: config.SET_DAY_RESET_TOOL_ENABLED,
        relevance=lambda u: 3,
        used=lambda session, u: bool(getattr(u, "day_reset_hour", 0)),
        reveal_when="they push back that a late-night meal landed on the wrong day",
    ),
    Capability(
        id="adjust_targets",
        what="your targets can move a bit if the number doesn't feel doable",
        how="ask for a number within about 15% of what i set and it's yours",
        tools=("set_targets",),
        enabled=lambda u: config.SET_TARGETS_TOOL_ENABLED,
        relevance=lambda u: 6 if getattr(u, "targets_source", None) != "user" else 2,
        used=None,
        reveal_when="they push back on a target",
    ),
    Capability(
        id="check_ins",
        what="i'll check in on my own — mornings, after your usual training slot, and when something's slipping",
        how="nothing to do; reply when you can, i don't need an answer every time",
        tools=(),
        enabled=lambda u: config.HEARTBEAT_ENABLED,
        relevance=lambda u: 8 if (getattr(u, "biggest_obstacle", "") or "") in ("consistency", "motivation") else 6,
        used=None,
        reveal_when="never — it just happens",
    ),
    Capability(
        id="profile_page",
        what="your profile page shows everything i have on you and your day",
        how="the link i sent; ask me any time and i'll resend it",
        tools=(),
        enabled=lambda u: True,
        relevance=lambda u: 3,
        used=None,
        reveal_when="they ask what you know about them",
    ),
    # Logger bridge (food_logger.py) — the two user-facing halves.
    Capability(
        id="app_screenshot_logging",
        what="still on mfp or mynetdiary? screenshot your day and i take the numbers straight off it",
        how="send a screenshot of the diary — no re-typing, i read the printed numbers",
        tools=(),  # rides on log_meal / manage_log with from_app
        enabled=lambda u: config.FOOD_LOGGER_BRIDGE_ENABLED and config.LOG_MEAL_TOOL_ENABLED
                          and getattr(config, "READ_IMAGE_ENABLED", True),
        relevance=lambda u: 9 if getattr(u, "food_logger", None) else 3,
        used=_app_meals_logged,
        reveal_when="they mention another food app, or ask to connect one",
    ),
    Capability(
        id="food_logger_state",
        what="if you keep another food app for now that's fine — i track around it until you drop it",
        how="tell me you're still using it, or that you've switched",
        tools=("set_food_logger",),
        enabled=lambda u: config.SET_FOOD_LOGGER_TOOL_ENABLED,
        relevance=lambda u: 6 if getattr(u, "food_logger", None) else 1,
        used=lambda s, u: bool(getattr(u, "food_logger_status", None)),
        reveal_when="they say they still log elsewhere, or that they deleted the other app",
    ),
]

# Tools the loop offers that are mechanics, not capabilities a user would be told about.
INTERNAL_TOOLS = frozenset({"react_to_message", "reply_in_thread"})

OBSTACLE_LINES = {
    "consistency": "you said staying consistent is the hard part — that's where i'll be on you most",
    "nutrition": "you said food is the hard part — that's where i'll earn my keep",
    "knowledge": "you said not knowing what to do is the hard part — ask me anything, no dumb questions",
    "time": "you said time is the hard part — i'll keep everything quick and fit it around your day",
    "motivation": "you said motivation is the hard part — i'll be the reason you show up on the off days",
    "injuries": "you said injuries are the hard part — i'll work around them, not through them",
}


def available(user) -> list[Capability]:
    out = []
    for c in CAPABILITIES:
        try:
            if c.enabled(user):
                out.append(c)
        except Exception:  # noqa: BLE001 — a flag lookup must never break a turn
            continue
    return sorted(out, key=lambda c: -int(c.relevance(user)))


def rundown_context(user, top: int = 3) -> str:
    """Model-facing block for the post-onboarding rundown: the top few for this
    user, the rest in one line, and the obstacle line. Only enabled capabilities
    ever appear here — the bubble can't promise something that's off."""
    caps = available(user)
    lead, rest = caps[:top], caps[top:]
    lines = ["LEAD WITH (in this order, their words, one clause each):"]
    for c in lead:
        lines.append(f"- {c.what}. how: {c.how}")
    if rest:
        lines.append("ALSO TRUE (pick at most one, only if it fits them; otherwise skip): "
                     + "; ".join(c.what for c in rest))
    ob = OBSTACLE_LINES.get((getattr(user, "biggest_obstacle", "") or "").strip().lower())
    if ob:
        lines.append(f"CLOSE ON: {ob}")
    return "\n".join(lines)


def unused(user, session, limit: int = 3) -> list[Capability]:
    """Enabled capabilities with no evidence of use yet (or no durable evidence),
    most relevant first — the coach may mention ONE when the moment calls for it."""
    out = []
    for c in available(user):
        if c.used is None:
            continue  # nothing to detect; the voice rule alone governs these
        try:
            if not c.used(session, user):
                out.append(c)
        except Exception:  # noqa: BLE001
            continue
    return out[:limit]


def unused_context(user, session) -> str:
    caps = unused(user, session)
    if not caps:
        return ""
    lines = ["## THINGS THEY HAVEN'T USED YET",
             "Mention ONE of these only when the moment calls for it (see the hint), in one "
             "clause, never as a list, never twice — check RECENT CONVERSATION first."]
    for c in caps:
        lines.append(f"- {c.what} — bring it up when: {c.reveal_when}")
    return "\n".join(lines)
