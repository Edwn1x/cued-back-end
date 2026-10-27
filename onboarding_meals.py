"""
Meals mid-setup (founder, 2026-10-27: "it should still log it and the functionality should
work as normal, acknowledge the request and then continue with the flow").

Before this, every text and photo from a user still in onboarding went to the intake agent,
which has no tools: "had a burrito" got a friendly reaction and no log. Now:

  1. is_meal_turn(): a photo is always the loop's (it reads menus, receipts, food — as
     normal). A text passes a cheap cue regex ("ate", "had a", "for lunch", "just grabbed"…)
     and then a one-word Haiku gate: is this a specific thing they ate / are eating now, or
     a habit / preference / restriction answer ("i eat at crossroads mostly", "no allergies")
     that the questionnaire asked for? Gate failure → not a meal turn (intake answers, as before).
  2. app.py runs the normal agent loop for that turn (log_meal, the numbers, menus, receipts —
     everything), with context_block() telling it there are NO targets yet and to keep it to
     one line with no setup questions.
  3. absorb_stats(): code-parsed stats in the same text ("had a burrito, im 5'10 170") still
     land on the profile.
  4. continue_line(): unless the loop's reply already ends on a question, code asks the next
     intake field, so the setup keeps moving.

The re-engage food-first window (onboarding_reengage.py) is the special case of this for a
nudged user and keeps its own copy + hook line; it runs first in app.py.
"""

from __future__ import annotations

import logging
import re

import config

logger = logging.getLogger("cued.onboarding_meals")

# Cheap pre-filter: an eating verb / meal word / "log" ask. Habit answers can pass this
# ("i eat at crossroads") — the gate below sorts those out; nothing here is final.
_CUE_RE = re.compile(
    r"\b(ate|eaten|eating|had|having|grabbed|got|just got|finished|drank|drinking|snacked|"
    r"breakfast|lunch|dinner|brunch|snack|meal|log(?: this| it| that)?|calories|cals|protein)\b",
    re.I)

GATE_PROMPT = (
    "A user mid sign-up texted a fitness coach. Is this message reporting a SPECIFIC food, drink "
    "or meal they ate, are eating now, or want logged (e.g. 'had a chicken burrito', 'just ate "
    "2 eggs and toast', 'log a protein shake', 'lunch was a bowl from crossroads')? Answer NO for "
    "habits, preferences, restrictions, plans or where they usually eat ('i eat at the dining "
    "hall', 'i mostly cook', 'no allergies', 'im vegetarian', 'ill eat after the gym'), and NO for "
    "anything that isn't about food. Reply with exactly one word: yes or no.\n\nMessage: "
)

_turns: set[int] = set()     # user ids whose current loop turn is a mid-setup meal turn
_client = None


def enabled() -> bool:
    return bool(getattr(config, "ONBOARDING_MEALS_ENABLED", True))


def _gate(text: str, user_id: int | None = None) -> bool:
    """One Haiku word. Any failure → False (the intake agent answers, as before)."""
    try:
        from llm_client import make_client
        from cost_tracking import track as track_usage
        global _client
        if _client is None:
            _client = make_client()
        r = _client.messages.create(model=config.HAIKU_MODEL, max_tokens=3,
                                    messages=[{"role": "user", "content": GATE_PROMPT + (text or "")[:600]}])
        try:
            track_usage(user_id, "onboarding_meals.gate", config.HAIKU_MODEL, r)
        except Exception:  # noqa: BLE001
            pass
        word = "".join(getattr(b, "text", "") for b in (r.content or [])).strip().lower()
        return word.startswith("yes")
    except Exception as e:  # noqa: BLE001
        logger.warning("ONBOARDING_MEAL_GATE_FAILED user=%s err=%s", user_id, e)
        return False


def is_meal_turn(text: str, *, has_image: bool, user_id: int | None = None) -> bool:
    if not enabled():
        return False
    if has_image:
        return True
    body = (text or "").strip()
    if not body or not _CUE_RE.search(body):
        return False
    verdict = _gate(body, user_id)
    logger.info("ONBOARDING_MEAL_GATE user=%s verdict=%s body=%r", user_id, verdict, body[:60])
    return verdict


def begin_turn(user_id: int) -> None:
    _turns.add(user_id)


def end_turn(user_id: int) -> None:
    _turns.discard(user_id)


def is_turn(user_id: int) -> bool:
    return user_id in _turns


def context_block(user) -> str | None:
    """The block the loop sees on a mid-setup meal turn."""
    if not is_turn(getattr(user, "id", None)):
        return None
    name = (user.name or "they").split()[0] if getattr(user, "name", None) else "they"
    return (f"## STILL MID-SETUP — A MEAL CAME IN\n{name} is still answering the setup questions, so there are NO "
            f"targets yet — no 'remaining', no daily number, no 'ur on track'. Everything else works as normal: "
            f"log_meal what they sent (text or photo), read a menu or receipt the way you always do.\n"
            f"- Reply in ONE short line like a friend: what it was, ~cal and protein. Logged, done.\n"
            f"- Do NOT ask a follow-up about the meal (portion, which one) — log your best estimate; they can fix it later.\n"
            f"- Do NOT ask any setup question and do not mention setup: code asks the next one right after your line.\n"
            f"- If it turns out not to be food at all, answer it briefly, no question.")


def absorb_stats(user_id: int, text: str) -> dict:
    """Code-parsed stats riding along with the meal ('had a burrito, im 5'10 170') → profile."""
    try:
        from onboarding_agent import parse_stats, _store_extracted_data
        found = parse_stats(text or "")
        if found:
            _store_extracted_data(user_id, found)
            logger.info("ONBOARDING_MEAL_TURN_STATS user=%s found=%s", user_id, sorted(found))
        return found
    except Exception as e:  # noqa: BLE001
        logger.warning("ONBOARDING_MEAL_TURN_STATS_FAILED user=%s err=%s", user_id, e)
        return {}


def continue_line(user) -> str | None:
    """Code's line after the meal: the next intake ask (None when nothing is left — the next
    text completes setup through the intake agent as usual)."""
    from onboarding_agent import _get_missing_fields, _plain, _BIG_ASK_NEVER
    missing = _get_missing_fields(user)
    if not missing:
        return None
    field = next((f for f in missing if f[0] not in _BIG_ASK_NEVER), missing[0])
    return f"logged. back to setup real quick: {_plain(field)}?"
