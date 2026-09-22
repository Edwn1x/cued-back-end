"""
Tier-2 (live) anchors for the Aislinn macro-accuracy photo fixes
(rewrite/aislinn-macro-photo/CHANGESPEC.md). Binary anchors per the founder rule:
parametrized x3 and every run must pass.

  a1 Sep 19 replay: a photo-estimated lunch row + a MyNetDiary lunch screenshot → ONE row,
     calories == 551 as printed (not re-estimated), source app, reply never says ~980.
  a2 breakfast photo: every portion the model guessed is named in the reply with a
     portion marker (one-line correction, not "how many eggs" and nothing about the toast).
  a3 "nah bruh my app says 1000 and 77g": no row edited on the dispute, the reply lists the
     logged items and asks; "the oats were 550 and 46g" → oats row edited, reply quotes 1000.
  a4 protein-once: the gap was said this morning; a new mid-morning log doesn't restate it.
  a5 calories-only screenshot: app rows have protein NULL; a protein number in the reply
     is marked as a guess in the same sentence.

Run: pytest tests/tier2/test_aislinn_macro_photo_live.py --run-tier2 -s
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2

_FIXDIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", current_split="none",
               height_ft=5, height_in=2, weight_lbs=172, age=16, gender="female", goal="fat_loss",
               diet="omnivore", calorie_target=1400, protein_target=173)


def _img(name) -> dict:
    with open(os.path.join(_FIXDIR, name), "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _enable(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
              "READ_IMAGE_ENABLED", "MEAL_ESTIMATION_PROMPT_ENABLED", "MEAL_ROUTING_PROMPT_ENABLED",
              "REMEMBER_TOOL_ENABLED", "PROMPT_CACHING_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _tz_at(local_hour: int) -> str:
    """A fixed-offset zone where the user's local time is ~local_hour now."""
    offset = (local_hour - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).order_by(Meal.id).all()
    finally:
        s.close()


def _seed_meal(user_id, desc, cal, pro, when, *, source="photo", confidence=None, carbs=None, fat=None):
    from models import get_session, Meal
    s = get_session()
    try:
        m = Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro, carbs_g=carbs, fat_g=fat,
                 source=source, log_type="user_reported", confidence=confidence, eaten_at=when, logged_at=when)
        s.add(m)
        s.commit()
        return m.id
    finally:
        s.close()


def _seed_msg(user_id, direction, when, body):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction=direction, body=body, message_type="freeform", created_at=when))
        s.commit()
    finally:
        s.close()


def _run(user_id, body, image=None):
    from agent_loop import run_agent_loop
    from models import get_session, User
    s = get_session()
    try:
        return run_agent_loop(s.get(User, user_id), body, "freeform", image_data=image)
    finally:
        s.close()


# ─── a1 the Sep 19 replay ────────────────────────────────────────────────────

@pytest.mark.parametrize("run", [1, 2, 3])
def test_diary_screenshot_replaces_the_photo_estimate(db, monkeypatch, run):
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **AISLINN, user_timezone=_tz_at(13))
    est = _seed_meal(user.id, "chicken + egg sandwich: 2 slices whole grain toast, grilled chicken ~3oz, "
                              "fried egg, cheese slice, lettuce", 430, 43, _now() - timedelta(minutes=70),
                     confidence="low", carbs=30, fat=17)
    _seed_msg(user.id, "in", _now() - timedelta(minutes=70), "[image attached]")
    _seed_msg(user.id, "out", _now() - timedelta(minutes=69), "logged it, ~430 cal 43g protein")
    _seed_msg(user.id, "in", _now() - timedelta(minutes=2), "Can I connect my calorie tracking app?")
    _seed_msg(user.id, "out", _now() - timedelta(minutes=1),
              "no connection, but screenshot the meal from ur app and i'll take the numbers as printed")

    reply = _run(user.id, "", _img("diary_screenshot.png"))
    rows = _meals(user.id)
    print(f"\n[A1 run {run}] {reply!r}\n  rows={[(r.id, r.description[:30], r.calories, r.protein_g, r.source) for r in rows]}")
    assert len(rows) == 1, f"the screenshot became a second row: {[(r.id, r.calories) for r in rows]}"
    row = rows[0]
    assert row.id == est, "edit the estimate, don't delete-and-relog"
    assert row.calories == 551, f"printed 551, stored {row.calories} (re-estimated?)"
    assert row.protein_g == 54 and row.source == "app", (row.protein_g, row.source)
    assert not re.search(r"\b9[5-9]\d\b|\b10[0-4]\d\b", reply), f"quoted the double-counted total: {reply!r}"
    assert "551" in reply, f"reply doesn't carry the printed total: {reply!r}"


# ─── a2 every guessed portion named ──────────────────────────────────────────

_PORTION = re.compile(r"\d+\s*(?:egg|slice|cup|oz|g\b|gram|tbsp|scoop|piece)|~\s*\d|about \d|half|couple|"
                      r"\b(?:one|two|three)\b", re.I)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_photo_reply_names_every_guessed_portion_in_one_line(db, monkeypatch, run):
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **AISLINN, user_timezone=_tz_at(8))
    reply = _run(user.id, "", _img("breakfast_scene.png"))
    rows = _meals(user.id)
    guessed = [r for r in rows if r.confidence == "low"]
    print(f"\n[A2 run {run}] {reply!r}\n  rows={[(r.description, r.calories, r.confidence) for r in rows]}")
    assert rows, "nothing logged from the breakfast photo"
    assert guessed, f"a photo estimate with no portion_guessed flag: {[(r.description, r.confidence) for r in rows]}"
    low = reply.lower()
    for r in guessed:
        head = re.split(r"[^a-z]+", r.description.lower())
        words = [w for w in head if len(w) >= 3 and w not in ("scrambled", "plated", "with", "and", "the")]
        assert any(w[:4] in low for w in words), f"guessed item {r.description!r} not named in reply: {reply!r}"
    assert _PORTION.search(reply), f"no portion stated in the reply: {reply!r}"


# ─── a3 "your app says X" ────────────────────────────────────────────────────

@pytest.mark.parametrize("run", [1, 2, 3])
def test_app_total_dispute_lists_items_then_writes_the_answer(db, monkeypatch, run):
    from tests.factories import make_user
    from models import get_session, Meal
    _enable(monkeypatch)
    user = make_user(db, **AISLINN, user_timezone=_tz_at(15))
    t0 = _now() - timedelta(minutes=30)
    oats = _seed_meal(user.id, "overnight protein oats", 350, 25, t0, source="text", carbs=45, fat=8)
    _seed_meal(user.id, "homemade McMuffin, ham + egg whites on english muffin", 260, 22, t0, source="text",
               carbs=28, fat=6)
    _seed_meal(user.id, "Vietnamese instant black coffee", 5, 0, t0, source="text", carbs=1, fat=0)
    _seed_meal(user.id, "1.5 mini butter chocolate croissants (Costco)", 185, 3, t0, source="text", carbs=20, fat=11)
    _seed_msg(user.id, "in", t0, "Alr morning I ate some overnight protein oats")
    _seed_msg(user.id, "out", t0 + timedelta(minutes=1), "logged, ~350 cal 25g protein")
    _seed_msg(user.id, "in", t0 + timedelta(minutes=3), "During lunch I made a homemade McMuffin with ham and "
              "only egg whites then a Vietnamese instant black coffee and 1 1/2 mini butter croissants from Costco")
    _seed_msg(user.id, "out", t0 + timedelta(minutes=4), "logged, ~450 cal 25g protein for the three")
    _seed_msg(user.id, "out", t0 + timedelta(minutes=4), "that puts u at 800 for the day, 50g protein")

    r1 = _run(user.id, "Nah bruh my app says 1000 cals and 77g protein")
    print(f"\n[A3 run {run} turn 1] {r1!r}")
    edited = [r for r in _meals(user.id) if r.edits]
    assert not edited, f"guessed the gap and wrote it: {[(r.description, r.edits) for r in edited]}"
    low = r1.lower()
    named = sum(1 for w in ("oats", "muffin", "croissant", "coffee") if w in low)
    assert named >= 2, f"didn't list the logged items: {r1!r}"
    # plain voice asks without a question mark ("which one's off, or just screenshot the app")
    assert "?" in r1 or re.search(r"\bwhich\b|screenshot|send", low), f"didn't ask which is off: {r1!r}"
    assert not re.search(r"double.?count|mostly the|probably the", low), f"invented a cause: {r1!r}"

    r2 = _run(user.id, "the oats were 550 and 46g protein, the rest matches")
    print(f"[A3 run {run} turn 2] {r2!r}")
    s = get_session()
    try:
        row = s.get(Meal, oats)
        assert (row.calories, row.protein_g) == (550, 46), f"oats not written: {(row.calories, row.protein_g)} {row.edits}"
    finally:
        s.close()
    assert "1000" in r2 or "1,000" in r2 or "1k" in r2.lower(), f"didn't quote the fresh total: {r2!r}"


# ─── a4 protein gap once ─────────────────────────────────────────────────────

# A restated GAP: "134g to go", "still 131g", "need 140g more". NOT a total ("29g total so far").
_GAP = re.compile(r"\d+\s*g?\s*(?:protein\s*)?(?:to go|left|short|under|away|more)\b|"
                  r"(?:need|chase|hit|load)\w*\s+\d+\s*g\b|"
                  r"\bstill\s+\d+\s*g\b|\d+\s*g\s+(?:protein\s+)?(?:still|to go|to hit)\b", re.I)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_mid_morning_log_does_not_restate_the_protein_gap(db, monkeypatch, run):
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **AISLINN, user_timezone=_tz_at(10))
    t0 = _now() - timedelta(minutes=40)
    _seed_meal(user.id, "133g raw broccoli", 45, 4, t0, source="text", carbs=9, fat=0)
    _seed_msg(user.id, "in", t0, "breakfast today: 133g raw broccoli")
    _seed_msg(user.id, "out", t0 + timedelta(minutes=1), "logged, 45 cal")
    _seed_msg(user.id, "out", t0 + timedelta(minutes=1), "169g protein to go today, front-load it")

    reply = _run(user.id, "log 103g of egg and 117g of egg whites")
    print(f"\n[A4 run {run}] {reply!r}")
    assert _meals(user.id), "nothing logged"
    assert len(_meals(user.id)) >= 2, "the eggs weren't logged"
    assert not _GAP.search(reply), f"restated the protein gap after a mid-morning log: {reply!r}"


# ─── a5 calories-only screenshot: printed only ──────────────────────────────

@pytest.mark.parametrize("run", [1, 2, 3])
def test_calories_only_screenshot_never_states_a_guessed_protein_as_fact(db, monkeypatch, run):
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **AISLINN, user_timezone=_tz_at(13))
    reply = _run(user.id, "", _img("diary_calories_only.png"))
    rows = _meals(user.id)
    print(f"\n[A5 run {run}] {reply!r}\n  rows={[(r.description[:28], r.calories, r.protein_g, r.source) for r in rows]}")
    assert rows, "nothing logged from the screenshot"
    assert all(r.source == "app" for r in rows), [(r.description, r.source) for r in rows]
    assert sum(r.calories or 0 for r in rows) == 551, f"printed 551, stored {sum(r.calories or 0 for r in rows)}"
    assert all(r.protein_g is None for r in rows), f"a guessed protein was written as a number: " \
                                                  f"{[(r.description, r.protein_g) for r in rows]}"
    for sent in re.split(r"(?<=[.!?\n])\s+", reply):
        if re.search(r"\d+\s*g\b.*protein|protein.*\d+\s*g\b", sent, re.I):
            assert re.search(r"guess|~|about|roughly|maybe|ballpark|est", sent, re.I), \
                f"protein number stated as fact: {sent!r} in {reply!r}"
