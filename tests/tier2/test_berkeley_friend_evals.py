"""
Tier-2 (live) — the Berkeley-friend first-reply evals (founder, 2026-09-11).

Eight Berkeley-flavored first replies to the hook, each run through the REAL
onboarding path (/webhook → buffer → handle_onboarding_reply: Haiku extractor +
the identity system prompt + Opus reply, web search on). Every reply is saved to
rewrite/evals/berkeley-friend-first-replies.md for hand review.

Pass criteria (founder): references the specific thing, reads as a person, at
most one question, no field asked without a reason. The first two are judged by
hand (the file is the artifact); the mechanical checks here are the floor: reply
exists, at most one '?', no links, a keyword from the inbound shows up, and for
the two search cases a WEB_SEARCH_QUERY was logged.

Run: pytest tests/tier2/test_berkeley_friend_evals.py --run-tier2 -s
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.tier2

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "rewrite", "evals", "berkeley-friend-first-replies.md")

HOOK = "hey I'm your cued coach, how's your day going?"

# (label, inbound, keywords any-of which should appear in the reply, expect_search)
CASES = [
    ("quiz stress", "ugh not great, have a 70 quiz at 4 and i'm so behind",
     ["70", "quiz", "discrete", "4"], False),
    ("just ate at crossroads", "pretty good, just ate at crossroads lol",
     ["crossroads", "xroads"], False),
    ("hungover", "hungover as hell ngl", ["hungover", "hangover", "last night", "water", "electrolyte"], False),
    ("gym is packed", "was gonna lift but rsf is packed rn", ["rsf", "packed", "rack", "busy", "rush", "peak", "war", "crowd", "zoo"], False),
    ("idk what to eat", "idk what to eat", ["eat", "food", "dining", "cook"], False),
    ("nothing much", "nothing much", [], False),
    ("cs70 midterm", "cs70 midterm is next tues", ["70", "midterm", "tues"], True),
    ("golden bear cafe", "thinking about going to golden bear cafe rn", ["gbc", "golden bear", "cafe"], True),
]

INTAKE = dict(height_ft=None, weight_lbs=None, occupation=None, activity_level=None,
              avg_steps=None, workout_days=None, workout_time=None, current_split=None,
              cooking_situation=None, diet=None, injuries=None, wake_time=None,
              sleep_time=None, existing_tools=None)


def _fresh_signup(db, name="Nau"):
    from tests.factories import make_user
    from models import get_session, Message
    user = make_user(db, name=name, age=20, goal="muscle_building", experience="beginner",
                     onboarding_step=1, **INTAKE)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body=HOOK, message_type="onboarding"))
        s.commit()
    finally:
        s.close()
    return user


PINNED_NOW = "Friday, Sep 11, 2026, 2:30pm"  # a Friday afternoon — the founder's example hour


def test_berkeley_friend_first_replies(db, driver, monkeypatch, caplog):
    import config, onboarding_agent
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    # Pin the wall clock: run at 4am and every reply is about the hour.
    monkeypatch.setattr(onboarding_agent, "_now_local",
                        lambda tz: datetime(2026, 9, 11, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles")))

    rows = []
    failures = []
    for label, inbound, keywords, expect_search in CASES:
        user = _fresh_signup(db)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            replies = driver.send(user, inbound)
        queries = [r.getMessage().split("query=", 1)[1] for r in caplog.records
                   if "WEB_SEARCH_QUERY" in r.getMessage() and "onboarding.generate" in r.getMessage()]
        reply = "\n".join(replies)
        print(f"\n[{label}] user: {inbound}\n[{label}] coach: {reply}\n[{label}] searches: {queries}")

        checks = {
            "one message": len(replies) == 1,
            "≤1 question": reply.count("?") <= 1,
            "no links": not re.search(r"https?://|www\.", reply.lower()),
            "references the thing": (not keywords) or any(k in reply.lower() for k in keywords),
            "searched" if expect_search else "no search needed": (bool(queries) if expect_search else True),
        }
        rows.append((label, inbound, reply, queries, checks))
        for name, ok in checks.items():
            if not ok:
                failures.append(f"[{label}] {name}: {reply!r}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# Berkeley-friend first replies — live eval\n\n")
        f.write(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} through the real "
                f"onboarding path (/webhook → handle_onboarding_reply), model `{config.COACH_MODEL}`, "
                f"web search on (cap {config.WEB_SEARCH_MAX_USES}/reply).\n\n")
        f.write("Hook the user is replying to: *" + HOOK + "*  \nClock pinned to " + PINNED_NOW + " (Berkeley).\n\n")
        f.write("Founder pass criteria: references the specific thing · reads as a person · at most one "
                "question · no field asked without a reason. **Hand review** column is filled in by a human.\n\n")
        for label, inbound, reply, queries, checks in rows:
            f.write(f"## {label}\n\n")
            f.write(f"**user:** {inbound}\n\n")
            f.write(f"**coach:** {reply}\n\n")
            if queries:
                f.write("**searched:** " + " · ".join(queries) + "\n\n")
            f.write("mechanical: " + " · ".join(f"{'✓' if ok else '✗'} {n}" for n, ok in checks.items()) + "\n\n")
            f.write("hand review: _pending_\n\n")
    print(f"\n[EVAL] wrote {OUT}")

    assert not failures, "\n".join(failures)


def test_asking_for_the_list_gets_the_big_ask_in_the_friend_voice(db, driver, monkeypatch, caplog):
    """The kept exception: when they ask what you need, the big ask is the right move.
    It should still react to them first and read as a friend, not a form."""
    import config, onboarding_agent
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    monkeypatch.setattr(onboarding_agent, "_now_local",
                        lambda tz: datetime(2026, 9, 11, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles")))
    user = _fresh_signup(db)
    inbound = "lol ok just tell me what you need from me and i'll send it"
    with caplog.at_level(logging.INFO):
        replies = driver.send(user, inbound)
    reply = "\n".join(replies)
    modes = [r.getMessage() for r in caplog.records if "ONBOARDING_REPLY mode=" in r.getMessage()]
    print(f"\n[big ask] user: {inbound}\n[big ask] coach: {reply}\n[big ask] {modes}")

    with open(OUT, "a", encoding="utf-8") as f:
        f.write("\n---\n\n## the kept exception: they ask for the list → big ask\n\n")
        f.write(f"**user:** {inbound}\n\n**coach:** {reply}\n\n")
        f.write("mechanical: " + ("✓" if len(replies) == 1 else "✗") + " one message · "
                + ("✓" if "mode=big_ask" in " ".join(modes) else "✗") + " big_ask mode · "
                + ("✓" if not re.search(r"^\s*\d+[.)]", reply, re.M) else "✗") + " not a numbered list\n\n")
        f.write("hand review: _pending_\n")

    assert len(replies) == 1
    assert any("mode=big_ask" in m for m in modes), modes
    assert not re.search(r"^\s*\d+[.)]", reply, re.M), "numbered list — that's a form"
    low = reply.lower()
    assert any(k in low for k in ("height", "weight", "sleep", "food", "gym", "train", "eat")), reply


def test_extractor_does_not_turn_an_anecdote_into_a_fact(db, monkeypatch):
    """Live (user 27): Haiku read 'we got malatang after' + 'wait for a table' as
    cooking_situation=mostly_eat_out and invented diet=omnivore. The explicit
    statement 30s later must extract as mix, with diet still unknown."""
    import onboarding_agent
    from tests.factories import make_user
    user = make_user(db, name="Nau", onboarding_step=2, **INTAKE)

    anecdote = ("We ended just coming back to Berkeley and getting malatang after that\n"
                "We were gonna wait that long for a table")
    a = onboarding_agent._extract_data_from_message(
        anecdote, user, last_coach_message="did you end up bailing and eating somewhere else or just calling it")
    print(f"\n[EXTRACT anecdote] {a}")
    assert a.get("cooking_situation") is None, a
    assert a.get("diet") is None, a

    explicit = "Uhh, I mostly cook and buy my groceries, but sometimes I eat out with friends or if I'm feeling lazy lol"
    b = onboarding_agent._extract_data_from_message(
        explicit, user, last_coach_message="you cooking most of your food or is it dining hall for you?")
    print(f"[EXTRACT explicit] {b}")
    assert b.get("cooking_situation") in ("mix", "cook_myself"), b
    assert b.get("diet") is None, b


def test_extractor_keeps_a_2am_bedtime_as_sleep_time(db, monkeypatch):
    """Live (user 27): 'anywhere from like 2-5am' / 'wake up 11am - 2pm' was stored as
    wake=02:00 sleep=11:00 — swapped. The heartbeat reads those hours to decide when it
    may text, so a swap means 3am texts and afternoon silence."""
    import onboarding_agent
    from tests.factories import make_user
    user = make_user(db, name="Nau", onboarding_step=2, **INTAKE)
    msg = ("Hmm\nLowkey anywhere from like 2-5am\n"
           "And then I wake up like anywhere from 11am - 2pm or something. Like that ngl")
    out = onboarding_agent._extract_data_from_message(
        msg, user, last_coach_message="roughly when do you end up crashing and waking up these days?")
    print(f"\n[EXTRACT late schedule] wake={out.get('wake_time')} sleep={out.get('sleep_time')}")
    def _h(t):
        return int(str(t).split(":")[0])
    assert out.get("sleep_time") and 1 <= _h(out["sleep_time"]) <= 5, out
    assert out.get("wake_time") and 11 <= _h(out["wake_time"]) <= 14, out
