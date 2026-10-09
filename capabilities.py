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
    # The rundown tier (founder, 2026-10-09). section = one of RUNDOWN_SECTIONS' keys and
    # rundown = the one sub-line under that headline, in the friend's voice. None = SHOW,
    # don't tell: the coach reveals it at the moment reveal_when describes, never in the
    # rundown. rundown_if = an extra gate beyond enabled (e.g. only if they named an app).
    section: str | None = None
    rundown: str | None = None
    rundown_if: Callable = lambda user: True
    rundown_order: int = 50          # lower first within a section (founder: hydration before "any reminder")


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


def _interval_reminder_set(session, user):
    from models import Reminder
    return (session.query(Reminder.id)
            .filter(Reminder.user_id == user.id, Reminder.every_hours.isnot(None)).first()) is not None


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


def _menu_saved(session, user):
    return bool(getattr(user, "saved_menus", None))


_FOOD_APPS = ("myfitnesspal", "mfp", "mynetdiary", "lose it", "loseit", "cronometer", "macrofactor", "noom")


def _names_a_food_app(user) -> bool:
    if getattr(user, "food_logger", None):
        return True
    tools = (getattr(user, "existing_tools", None) or "").lower()
    return any(a in tools for a in _FOOD_APPS)


def _is_student(user) -> bool:
    return "student" in (getattr(user, "occupation", None) or "").lower() or bool(getattr(user, "year", None))


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
        tools=("log_workout", "start_workout_session", "reset_workout_session", "save_routine", "set_lift_anchors", "set_card_delivery"),
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
        section="food", rundown="i know all the dining hall macros, just ask about any of them",
    ),
    Capability(
        id="saved_menus",
        what="send me your spot's menu once — your dorm, house, or a meal-prep list — and i log from it after",
        how="text me the menu (a pic works); when you eat something off it i log the right macros without asking",
        tools=("save_menu",),
        enabled=lambda u: config.SAVE_MENU_TOOL_ENABLED,
        relevance=lambda u: 6,
        used=_menu_saved,
        reveal_when="they mention a set menu, house/frat meals, or a meal plan they eat off of",
    ),
    Capability(
        id="receipts",
        what="send me a grocery receipt and i'll log what you've got so dinner ideas use it",
        how="a photo of the receipt — i read the lines; 'out of chicken' or 'what do i have' keeps it current",
        tools=("stock_pantry",),
        enabled=lambda u: config.RECEIPTS_ENABLED and config.READ_IMAGE_ENABLED,
        relevance=lambda u: 7 if "cook" in (getattr(u, "cooking_situation", "") or "").lower() else 4,
        used=lambda session, u: session.query(__import__("models").PantryItem.id).filter_by(user_id=u.id).first() is not None,
        reveal_when="they mention groceries, a store run, or not knowing what to cook",
        section="food", rundown="a pic of a grocery receipt and i know what u got at home",
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
        how="just text me 'heading to the gym' — if the line's on you get the link, one tap; or ask for it any time",
        tools=("send_gym_line_link",),
        enabled=lambda u: config.RSF_METER_ENABLED,
        relevance=lambda u: 6,
        used=lambda session, u: session.query(__import__("models").Message.id).filter_by(user_id=u.id, direction="out", message_type="gym_line_d1").first() is not None,
        reveal_when="they complain the gym's packed or ask about the crowd",
        section="campus", rundown="i know how full rsf is at any given time, and when it's packed i send u the line link",
    ),
    Capability(
        id="study_rooms",
        what="when you need somewhere to study i'll find you a real open room at moffitt and send the booking link, or tell you which libraries are open right now",
        how="text me 'need a room for 4 at 3' or 'what's open late tonight' — i check the live booking grid and the hours page, you tap the link and book with calnet",
        tools=("find_study_space", "send_study_room_link"),
        enabled=lambda u: config.FIND_STUDY_SPACE_TOOL_ENABLED,
        relevance=lambda u: 7 if (getattr(u, "occupation", "") or "").lower().startswith("student") else 3,
        used=lambda session, u: session.query(__import__("models").Message.id).filter_by(user_id=u.id, direction="out", message_type="study_room_link").first() is not None,
        reveal_when="they mention studying, a group project, a midterm coming up, or ask where to go / what's open",
        section="campus", rundown="i can find empty library rooms to book for study sessions",
    ),
    Capability(
        id="stat_cards",
        what="i can drop a quick card in the chat: how packed rsf is, where your macros are today, what your week looks like",
        how="just ask, like 'how packed is rsf' or 'how am i doing today'",
        tools=("send_stat_card",),
        enabled=lambda u: config.STAT_CARD_TOOL_ENABLED,
        relevance=lambda u: 5,
        used=lambda session, u: session.query(__import__("models").Message.id).filter(
            __import__("models").Message.user_id == u.id,
            __import__("models").Message.message_type.like("stat_card_%")).first() is not None,
        reveal_when="they ask how packed the gym is, how today's numbers look, or what their week looks like",
        section="workouts", rundown="ask 'how am i doing today' and i drop a quick card in the chat",
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
        section="extras", rundown="any reminder u want, just say 'remind me…' and it shows up on time",
    ),
    Capability(
        id="water_reminders",
        what="i can ping you to drink water every couple hours while you're up",
        how="say 'remind me to drink water' (or yes to my offer) and you'll get a short nudge every 2-3 hours between wake and bed",
        tools=("set_reminder",),
        enabled=lambda u: config.REMINDERS_ENABLED and config.WATER_REMINDERS_ENABLED,
        relevance=lambda u: 8 if getattr(u, "water_offer_status", None) in (None, "lapsed") else 3,
        used=_interval_reminder_set,
        reveal_when="they mention headaches, low energy, or forgetting to drink",
        section="extras", rundown="hydration: say 'remind me to drink water' and i nudge u every couple hours while ur up",
        rundown_order=10,
    ),
    Capability(
        id="checkin_level",
        what="you set how much i text you first — more, normal, or chill",
        how="say 'text me more' or 'chill with the texts' and i'll actually change it, not just say ok",
        tools=("set_checkin_level",),
        enabled=lambda u: config.SET_CHECKIN_LEVEL_TOOL_ENABLED,
        relevance=lambda u: 4,
        used=lambda session, u: bool(getattr(u, "checkin_level", None)),
        reveal_when="they say the check-ins feel too far apart, or too frequent",
    ),
    Capability(
        id="calendar",
        what="i can hold dates — a midterm, a trip, a game — and plan around them",
        how="tell me 'midterm thursday' or 'home this weekend' and i'll work with it",
        tools=("log_event", "lookup_events"),
        enabled=lambda u: config.LOG_EVENT_TOOL_ENABLED,
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower() == "student" else 4,
        used=_events_logged,
        reveal_when="they mention something coming up with a date",
    ),
    Capability(
        id="schedule_rundown",
        what="ask me what your week looks like and i'll lay out the whole thing — every day, every deadline",
        how="'what's my week', 'rest of the week', 'what's due' — i pull your full calendar and give you all of it, nothing dropped",
        tools=("schedule_rundown",),
        enabled=lambda u: config.SCHEDULE_RUNDOWN_ENABLED,
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower() == "student" else 4,
        used=None,
        reveal_when="they ask what's coming up, what their week looks like, or what's due",
    ),
    Capability(
        id="block_time",
        what="i can drop a gym or study block straight onto your google calendar",
        how="say 'block a lift at 4' or 'add a study block 2-4 before the exam' — i'll confirm, then add it",
        tools=("create_calendar_event",),
        enabled=lambda u: config.CALENDAR_WRITE_ENABLED,
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower() == "student" else 4,
        used=None,
        reveal_when="they say they need to make time for training or studying, or ask you to put something on their calendar",
    ),
    Capability(
        id="weigh_ins",
        what="tell me your weight now and then and i'll track the trend and tune your calories off real data",
        how="'weighed in at 141' or a scale screenshot; once a week is plenty",
        tools=("log_weight",),
        enabled=lambda u: config.LOG_WEIGHT_TOOL_ENABLED and not getattr(u, "weigh_in_opt_out", False),
        relevance=lambda u: 7 if _goal_has(u, "fat_loss", "muscle") else 4,
        used=lambda session, u: _weighed_in(session, u),
        reveal_when="never on day one and never as a pitch — after their first week, on a quiet morning, ask once for a weigh-in; or when they mention the scale or ask if the numbers are right",
    ),
    Capability(
        id="fix_a_log",
        what="if i log something wrong you can just tell me and i fix it — including moving a meal to another day",
        how="'that wasn't 850 cal', 'delete that workout', or 'move the eggs to yesterday'",
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
        section="food", rundown="still logging in another app? screenshot ur diary and i take the numbers as printed",
        rundown_if=lambda u: _names_a_food_app(u),
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
    Capability(
        id="connect_accounts",
        what="i can read your google calendar, your bcourses due dates, your fitbit or pixel watch (sleep, steps, heart rate), and strava so i plan around your week and how you're actually recovering",
        how="say the word and i text you a one-tap link — no app, no login. for bcourses just paste me your calendar feed link (or an access token if you want me to see what you've turned in)",
        tools=("send_connect_link", "set_google_account"),
        enabled=lambda u: (config.GCAL_ENABLED or config.STRAVA_READ_ENABLED
                           or config.BCOURSES_ENABLED or config.CANVAS_ENABLED
                           or config.GOOGLE_HEALTH_ENABLED),
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower() == "student" else 4,
        used=None,
        reveal_when="they mention their calendar, a busy week, strava, a run/ride, their fitbit or watch, how they slept, or their steps",
    ),
    Capability(
        id="weather",
        what="i keep an eye on the weather where you are — the morning brief calls it, and you can just ask 'do i need a jacket'",
        how="ask me the weather anytime; tell me where you are ('i'm in LA this week') and i'll switch to that city — otherwise i assume berkeley",
        tools=("get_weather", "set_weather_location"),
        enabled=lambda u: config.WEATHER_ENABLED,
        relevance=lambda u: 4,
        used=lambda session, u: getattr(u, "weather_place", None) is not None,
        reveal_when="never as a pitch — the morning brief shows it; answer when they ask about the weather, rain/cold/heat, travel, or say where they are",
    ),
    Capability(
        id="tasks",
        what="if i can't answer something right now, i'll go find out and text you — 'where's the midterm, tell me tonight' or 'let me know if a seat opens'",
        how="ask me to find out and text you later, or to keep an eye on something; i'll do the looking and message you when i have it",
        tools=("schedule_task", "cancel_task"),
        enabled=lambda u: config.TASKS_ENABLED,
        relevance=lambda u: 6 if (getattr(u, "occupation", "") or "").lower().startswith("student") else 4,
        used=None,
        reveal_when="they ask about something you can't see yet (a room not posted, a seat, a grade) or say 'tell me later'",
    ),
    Capability(
        id="fetch_page",
        what="send me a link — a syllabus, a course site, a place's hours page — and i'll actually read it and pull out what matters",
        how="text me the link (or name the class and i'll find its site); i'll grab exam dates, due dates, grading, hours — and put the dated stuff on your calendar",
        tools=("fetch_page",),
        enabled=lambda u: config.FETCH_PAGE_TOOL_ENABLED,
        relevance=lambda u: 7 if (getattr(u, "occupation", "") or "").lower().startswith("student") else 4,
        used=None,
        reveal_when="they paste a link, mention a syllabus or a course site, or ask when something is due / what a class's exam dates are",
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


# ─── The rundown (founder, 2026-10-09): numbered, tiered, two bubbles ────────────
# Headline sections in order with their fixed lines; capabilities with section= add one
# sub-line each when enabled (and rundown_if). A section appears only when its gate
# capability is enabled (or any of its sub-lines is). Everything with section=None is
# SHOW, don't tell. Weather is deliberately absent: the morning brief shows it.
RUNDOWN_INTRO = "oh and a quick rundown of how i work"


def _food_lines(user):
    return ["tell me what u ate (food scale = even better)", "or a pic of the plate"]


def _workout_lines(user):
    return ["say 'starting workout'",
            "a card shows up and u check off each set, like a checklist",
            "or just text me what u hit and i log it that way"]


def _connect_title(user):
    bc = config.BCOURSES_ENABLED and _is_student(user)
    return "i can connect to ur calendar and bcourses" if bc else "i can connect to ur google calendar"


def _connect_lines(user):
    bc = config.BCOURSES_ENABLED and _is_student(user)
    out = ["just tell me u want to connect it" + (" (both, or either)" if bc else ""),
           "i plan workouts around ur schedule"]
    if bc:
        out.append("remind u of due dates and meetings")
    if _is_student(user):
        out.append("help u plan around midterms and study sessions")
    try:
        from connect_offers import has_wearable, device_label
        # The wearable line waits for Google's API approval (GOOGLE_HEALTH_OFFER_ENABLED).
        if config.GOOGLE_HEALTH_ENABLED and getattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", False) and has_wearable(user):
            out.append(f"and ur {device_label(user)}: sleep, steps, heart rate, so i plan around how ur recovering")
    except Exception:  # noqa: BLE001
        pass
    return out


def _extras_tail(user):
    tools = (getattr(user, "existing_tools", None) or "").strip().lower()
    if tools and tools != "none":
        return ["and feel free to share anything from apps u already use, screenshots work"]
    return []


RUNDOWN_SECTIONS = [
    # key, title, fixed lines, gate capability ids (section shows if ANY is enabled), tail lines
    ("food", lambda u: "i track ur calories", _food_lines, ("log_meals",), lambda u: []),
    ("workouts", lambda u: "i track ur workouts and weights at the gym", _workout_lines, ("log_workouts",), lambda u: []),
    ("connect", _connect_title, _connect_lines, ("connect_accounts",), lambda u: []),
    ("campus", lambda u: "rsf, libraries, and study rooms", lambda u: [], (), lambda u: []),
    ("extras", lambda u: "and lastly, nice to haves", lambda u: [], (), _extras_tail),
]


def rundown_sections(user) -> list[tuple[str, str, list[str]]]:
    """[(key, title, lines)] for this user — only sections with something true in them."""
    enabled = {c.id: c for c in available(user)}
    out = []
    for key, title, fixed, gates, tail in RUNDOWN_SECTIONS:
        gate_ok = any(g in enabled for g in gates) if gates else False
        if gates and not gate_ok:
            continue
        subs = []
        for i, c in enumerate(CAPABILITIES):
            if c.section != key or not c.rundown or c.id not in enabled:
                continue
            try:
                if not c.rundown_if(user):
                    continue
            except Exception:  # noqa: BLE001
                continue
            subs.append((c.rundown_order, i, c.rundown))
        if not gates and not subs:
            continue          # a sub-line-only section with nothing enabled (the tail never stands alone)
        lines = list(fixed(user)) + [t for _o, _i, t in sorted(subs)] + list(tail(user))
        if not lines:
            continue
        out.append((key, title(user), lines))
    return out


def build_rundown(user) -> str:
    """The two rundown bubbles (joined with `---`), code-authored. Numbered sections,
    one dash line each thing; the first two sections in bubble one, the rest in bubble
    two, the obstacle line last. '' when nothing is enabled."""
    sections = rundown_sections(user)
    if not sections:
        return ""
    blocks = []
    for n, (_key, title, lines) in enumerate(sections, 1):
        blocks.append("\n".join([f"{n}. {title}"] + [f"- {ln}" for ln in lines]))
    first = "\n".join([RUNDOWN_INTRO] + blocks[:2])
    rest = blocks[2:]
    ob = OBSTACLE_LINES.get((getattr(user, "biggest_obstacle", "") or "").strip().lower())
    if rest:
        second = "\n".join(rest + ([ob] if ob else []))
        return f"{first}\n---\n{second}"
    return first + (f"\n{ob}" if ob else "")


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
