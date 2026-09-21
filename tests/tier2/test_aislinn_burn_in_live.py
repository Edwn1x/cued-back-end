"""
Tier-2 (live) anchors for the Aislinn burn-in fixes (rewrite/aislinn-burn-in/SPEC.md).
Binary anchors per the founder rule: re-run (parametrized x3) and pass every time.

  1. YES-anchor: a bodyweight user who joined 5 days ago and never trained → the
     heartbeat speaks, and the text is about training (the TRAINING GAP block is the
     only material; before this PR the coach had no training signal at all for her).
  2. NO-REVIVE anchor: an unanswered question from a PREVIOUS local day is not brought
     back — the tick either stays silent for today's reasons or speaks about today.
  3. WRITE-BACK anchor: given the detail that re-estimates yesterday's logged muffin, the
     agent loop EDITS that row (manage_log) instead of only saying the new number.
Run: pytest tests/tier2/test_aislinn_burn_in_live.py --run-tier2 -s
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2

AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", current_split="none",
               workout_days="3-4", workout_time="18:00", wake_time="06:50", sleep_time="00:00",
               height_ft=5, height_in=2, weight_lbs=180, age=16, gender="female", goal="fat_loss",
               activity_level="lightly_active", user_timezone="America/Los_Angeles",
               calorie_target=1400, protein_target=173)

SUMMARY = ("## Coaching Decisions\n- 1400 cal, 173g protein/day, confirmed.\n"
           "- Training: 3-4 days/week, bodyweight in her room (no gym), nights after homework, "
           "20-30 min to start.\n## Recent Themes\n- Consistency is the whole reason she signed up "
           "('wanna take it seriously this time'); asked to be kept on it.\n"
           "## Workouts Completed\n- None yet.")

TRAINING_WORDS = re.compile(r"workout|train|session|push ?up|squat|lunge|plank|move|sweat|card|"
                            r"20 ?min|tonight|homework|body ?weight|reps|sets|exercise", re.I)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_msg(s, user_id, direction, when, body, message_type="freeform"):
    from models import Message
    s.add(Message(user_id=user_id, direction=direction, body=body, message_type=message_type, created_at=when))


def _daytime_tz():
    """A fixed-offset zone where the user's local time is ~2pm now, so the night gate
    the model applies on its own ('16yo, 6:50 alarm') can't be the reason for silence."""
    offset = (14 - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_heartbeat_speaks_on_never_trained_bodyweight_gap(db, monkeypatch, run):
    import config, heartbeat
    from models import get_session, WorkoutSession
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, **dict(AISLINN, user_timezone=_daytime_tz()), coaching_summary=SUMMARY,
                     created_at=_now() - timedelta(days=5))
    s = get_session()
    try:
        s.add(WorkoutSession(user_id=user.id, date=_now() - timedelta(days=5), template_key="full_body",
                             status="abandoned"))
        # a closed, non-question exchange ~20h ago: no live conversation, no open thread
        _seed_msg(s, user.id, "in", _now() - timedelta(hours=20), "Cooking 👌")
        _seed_msg(s, user.id, "out", _now() - timedelta(hours=20, minutes=-1), "nice, enjoy")
        s.commit()
    finally:
        s.close()

    spoke, payload, _ = heartbeat.decide(user.id)
    print(f"\n[GAP-ANCHOR run {run}] spoke={spoke} :: {payload!r}")
    assert spoke is True, f"YES-ANCHOR FAILED (run {run}): 5 days, never trained, card untouched — silence: {payload!r}"
    assert TRAINING_WORDS.search(payload), f"spoke, but not about training: {payload!r}"
    assert not re.search(r"bench|barbell|deadlift|gym", payload, re.I), f"barbell/gym talk for a bodyweight user: {payload!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_heartbeat_does_not_revive_an_expired_question(db, monkeypatch, run):
    import config, heartbeat
    from models import get_session, Workout
    from timefmt import local_day_bounds
    from tests.factories import make_user

    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    user = make_user(db, **dict(AISLINN, user_timezone=_daytime_tz()), coaching_summary=SUMMARY)
    s = get_session()
    try:
        start, _ = local_day_bounds(user)
        s.add(Workout(user_id=user.id, workout_type="full_body", completed=True, date=_now() - timedelta(days=1)))
        _seed_msg(s, user.id, "in", start - timedelta(hours=8), "Nah bruh")
        _seed_msg(s, user.id, "out", start - timedelta(hours=8, minutes=-1), "wait what's off")
        _seed_msg(s, user.id, "in", start - timedelta(hours=7), "1k Cals and 77 g of protein")
        _seed_msg(s, user.id, "out", start - timedelta(hours=7, minutes=-1), "gap's mostly the mcmuffin protein")
        _seed_msg(s, user.id, "out", start - timedelta(hours=7, minutes=-2), "how many egg whites and how much ham")
        s.commit()
    finally:
        s.close()

    spoke, payload, _ = heartbeat.decide(user.id)
    print(f"\n[NO-REVIVE run {run}] spoke={spoke} :: {payload!r}")
    low = payload.lower()
    if spoke:
        assert not re.search(r"muffin|egg white|ham\b|how much|how many", low), f"revived yesterday's question: {payload!r}"
    else:
        assert not re.search(r"(her|their|his) turn|muffin", low), f"silent BECAUSE of the expired question: {payload!r}"


def test_reestimate_writes_back_to_the_logged_row(db, monkeypatch):
    import config, usda
    from agent_loop import run_agent_loop
    from models import get_session, Meal, User, active
    from timefmt import local_day_bounds
    from tests.factories import make_user

    for f in ("SINGLE_AGENT_LOOP_ENABLED", "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
              "USDA_LOOKUP_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    # live model, mocked external HTTP: the lookup is deterministic, the judgment is not
    monkeypatch.setattr(config, "USDA_API_KEY", "test-key")
    monkeypatch.setattr(usda, "search_usda", lambda q: (
        [{"description": "Egg, white, raw, fresh", "data_type": "Foundation", "calories": 52,
          "protein_g": 10.9, "carbs_g": 0.7, "fat_g": 0.2}] if "egg" in q.lower() else
        [{"description": "Turkey, breast, deli, sliced", "data_type": "Foundation", "calories": 104,
          "protein_g": 17.0, "carbs_g": 3.5, "fat_g": 1.7}]))

    user = make_user(db, **AISLINN, coaching_summary=SUMMARY)
    s = get_session()
    try:
        start, _ = local_day_bounds(user)
        y = start - timedelta(hours=7)
        m = Meal(user_id=user.id, description="homemade McMuffin, ham + egg whites on english muffin",
                 calories=260, protein_g=22, carbs_g=28, fat_g=6, source="text", log_type="user_reported",
                 eaten_at=y, logged_at=y)
        s.add(m)
        _seed_msg(s, user.id, "in", y, "During lunch I made a homemade McMuffin with ham and only egg whites")
        _seed_msg(s, user.id, "out", y + timedelta(minutes=1), "logged, ~260 cal 22g protein")
        _seed_msg(s, user.id, "in", y + timedelta(hours=1), "Nah bruh")
        _seed_msg(s, user.id, "out", y + timedelta(hours=1, minutes=1), "wait what's off")
        _seed_msg(s, user.id, "in", y + timedelta(hours=2), "1k Cals and 77 g of protein")
        _seed_msg(s, user.id, "out", y + timedelta(hours=2, minutes=1), "gap's mostly the mcmuffin protein")
        _seed_msg(s, user.id, "out", y + timedelta(hours=2, minutes=2), "how many egg whites and how much ham")
        s.commit()
        meal_id = m.id
    finally:
        s.close()

    s = get_session()
    try:
        u = s.get(User, user.id)
        reply = run_agent_loop(u, "90g egg white and 55g of turkey breast", "freeform")
    finally:
        s.close()
    print(f"\n[WRITE-BACK] {reply!r}")

    s = get_session()
    try:
        rows = active(s, Meal, user_id=user.id).all()
        row = s.get(Meal, meal_id)
    finally:
        s.close()
    assert len(rows) == 1, f"edit, not relog: {[(r.id, r.description) for r in rows]}"
    assert row.edits, "the re-estimate was never written back (no manage_log edit on the muffin row)"
    assert row.protein_g != 22, f"protein unchanged after the re-estimate: {row.protein_g}"
    assert 20 <= (row.protein_g or 0) <= 40 and 150 <= (row.calories or 0) <= 400, (row.calories, row.protein_g)
