"""
Aislinn burn-in fixes (rewrite/aislinn-burn-in/SPEC.md) — tier-1, red-first.

Seven live findings from user 32's first five days, each pinned deterministically:
  1. bodyweight templates + equipment-aware plan; weight-0 done sets count everywhere
  2. heartbeat TRAINING GAP standing condition (code-computed days + frequency)
  3. re-estimate write-back: usda affordance names the logged row; yesterday's meals in context
  4. OPEN THREAD: a past-day question is marked EXPIRED
  5. legacy classifier: word boundaries ("egg whites" is not "hit")
  6. /admin/send double-post guard
  7. pronoun-neutral facts; first weigh-in anchors weigh_in_day; protein_target_computed follows
"""

from __future__ import annotations

import re

import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", current_split="none",
               workout_days="3-4", workout_time="18:00", wake_time="06:50", sleep_time="00:00",
               height_ft=5, height_in=2, weight_lbs=180, age=16, gender="female", goal="fat_loss",
               activity_level="lightly_active", user_timezone="America/Los_Angeles")


def _sets(session_id):
    from models import get_session, SetLog
    s = get_session()
    try:
        return s.query(SetLog).filter_by(session_id=session_id).order_by(SetLog.id).all()
    finally:
        s.close()


def _done_all(session_id, *, reps=None):
    """Tap every set done as planned (the card's default tap: no weight typed)."""
    from models import get_session, SetLog, WorkoutSession
    from card_page import apply_set_update
    s = get_session()
    try:
        ws = s.get(WorkoutSession, session_id)
        for x in s.query(SetLog).filter_by(session_id=session_id).order_by(SetLog.id).all():
            apply_set_update(s, ws, x, done=True, actual_reps=reps, source="card")
    finally:
        s.close()


# ─── 1. bodyweight templates ─────────────────────────────────────────────────

def test_bodyweight_user_gets_bodyweight_templates_full_gym_unchanged(db):
    from workouts.templates import templates_for, TEMPLATES, BODYWEIGHT_TEMPLATES
    bw = make_user(db, **AISLINN)
    gym = make_user(db, equipment="full_gym")
    assert templates_for(bw) is BODYWEIGHT_TEMPLATES
    assert templates_for(gym) is TEMPLATES
    assert set(BODYWEIGHT_TEMPLATES) == set(TEMPLATES), "every split key must exist for bodyweight"
    for key, exs in BODYWEIGHT_TEMPLATES.items():
        assert exs, key
        for e in exs:
            assert e.default_weight == 0 and e.plate_step == 0, (key, e.slug)
            assert e.rep_step > 0, "bodyweight progresses by reps"
    # no barbell/machine slug leaks into the bodyweight set
    barbell = {e.slug for exs in TEMPLATES.values() for e in exs}
    for exs in BODYWEIGHT_TEMPLATES.values():
        for e in exs:
            assert e.slug not in barbell, e.slug


def test_build_session_for_bodyweight_user_plans_reps_only(db):
    from workouts.plan import build_session
    user = make_user(db, **AISLINN)
    ws = build_session(user, "full_body")
    rows = _sets(ws.id)
    assert rows, "a bodyweight full-body session must still have sets"
    assert all(float(r.planned_weight or 0) == 0 for r in rows)
    assert all((r.planned_reps or 0) > 0 for r in rows)
    slugs = {r.exercise for r in rows}
    assert not ({"squat", "bench_press", "barbell_row", "overhead_press", "romanian_deadlift"} & slugs), slugs


def test_bodyweight_progression_adds_reps_when_all_hit(db):
    from workouts.plan import build_session
    from workouts.templates import BODYWEIGHT_TEMPLATES
    user = make_user(db, **AISLINN)
    first = build_session(user, "push")
    _done_all(first.id)  # hit every planned rep at weight 0
    from workouts.summary import finish_session
    finish_session(first.id)
    second = build_session(user, "push")
    lead = BODYWEIGHT_TEMPLATES["push"][0]
    planned = [r for r in _sets(second.id) if r.exercise == lead.slug]
    assert planned and all(r.planned_reps == lead.reps + lead.rep_step for r in planned)
    assert all(float(r.planned_weight or 0) == 0 for r in planned)


def test_weight_zero_done_sets_count_in_state_summary_and_texts(db, sms_capture):
    from workouts.plan import build_session
    from workouts.summary import summarize, format_summary
    from workouts.card import card_layout
    from card_page import build_state
    from models import get_session, WorkoutSession
    user = make_user(db, **AISLINN)
    ws = build_session(user, "full_body")
    _done_all(ws.id)
    s = get_session()
    try:
        row = s.get(WorkoutSession, ws.id)
        state = build_state(s, row)
        summ = summarize(s, row)
    finally:
        s.close()
    n = len(_sets(ws.id))
    assert state["done_count"] == n, "a done bodyweight set is done"
    assert state["bodyweight"] is True
    assert all(e["bodyweight"] is True for e in state["exercises"])
    assert summ["sets_done"] == n
    assert summ["lines"], "reps-only lines must still render"
    for line in summ["lines"]:
        assert "0×" not in line and "0 ×" not in line, line
    text = format_summary(summ)
    assert "lb" not in text and "sets" in text.splitlines()[-1], text
    sub = card_layout(state)["subcaption"]
    assert "lb" not in sub, sub


def test_sms_bodyweight_exercise_messages_are_reps_only(db, sms_capture, monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", False)
    from workouts.start import start_workout_session
    user = make_user(db, **AISLINN)
    r = start_workout_session(user.id)
    assert r["template_key"] == "full_body" and r["surface"] == "messages"
    bodies = [b for _p, b in sms_capture]
    exercise_lines = [b for b in bodies if "×" in b]
    assert exercise_lines, bodies
    for b in exercise_lines:
        assert " · 0 ×" not in b and " · 0×" not in b, b   # "10 × 3" is fine; "· 0 × 10 × 3" is the bug
        assert b.count("×") == 1, f"reps × sets only: {b}"


# ─── 2. heartbeat: TRAINING GAP ──────────────────────────────────────────────

def _ctx_user(db, **kw):
    from models import get_session, User
    u = make_user(db, **kw)
    s = get_session()
    return s, s.get(User, u.id)


def test_training_gap_never_trained_since_joining(db):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN, created_at=_now() - timedelta(days=5))
    try:
        sig = heartbeat._training_gap_signal(user, s)
    finally:
        s.close()
    assert sig and "TRAINING GAP" in sig
    assert "5 days" in sig and "3" in sig, sig
    assert "never" in sig.lower() or "no completed" in sig.lower(), sig


def test_training_gap_names_an_untouched_card(db):
    import heartbeat
    from models import get_session, WorkoutSession
    s, user = _ctx_user(db, **AISLINN, created_at=_now() - timedelta(days=6))
    try:
        s.add(WorkoutSession(user_id=user.id, date=_now() - timedelta(days=5), template_key="full_body",
                             status="abandoned"))
        s.commit()
        sig = heartbeat._training_gap_signal(user, s)
    finally:
        s.close()
    assert sig and "card" in sig.lower() and "5 days" in sig, sig


def test_training_gap_silent_when_recently_trained_or_too_new(db):
    import heartbeat
    from models import get_session, Workout
    s, user = _ctx_user(db, **AISLINN, created_at=_now() - timedelta(days=30))
    try:
        s.add(Workout(user_id=user.id, workout_type="full_body", completed=True, date=_now() - timedelta(days=1)))
        s.commit()
        assert heartbeat._training_gap_signal(user, s) is None
    finally:
        s.close()
    s, fresh = _ctx_user(db, **AISLINN, created_at=_now() - timedelta(days=1))
    try:
        assert heartbeat._training_gap_signal(fresh, s) is None, "a day-old signup is not a gap"
    finally:
        s.close()


@pytest.mark.parametrize("days_str,per_week", [("3-4", 3), ("mon/wed/fri", 3), ("5", 5), ("", 3), ("mon, tue, wed, thu", 4)])
def test_training_gap_threshold_follows_committed_frequency(db, days_str, per_week):
    import heartbeat
    from models import get_session, Workout
    threshold = math.ceil(7 / per_week) + 1
    kw = dict(AISLINN, workout_days=days_str, created_at=_now() - timedelta(days=60))
    for gap, expect in ((threshold - 1, False), (threshold, True)):
        s, user = _ctx_user(db, **kw)
        try:
            s.add(Workout(user_id=user.id, workout_type="x", completed=True, date=_now() - timedelta(days=gap)))
            s.commit()
            sig = heartbeat._training_gap_signal(user, s)
        finally:
            s.close()
        assert bool(sig) is expect, (days_str, gap, sig)
        if sig:
            assert f"{gap} days" in sig and str(per_week) in sig


def test_training_gap_reaches_proactive_context(db):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN, created_at=_now() - timedelta(days=5))
    try:
        ctx = heartbeat._proactive_context(user, s)
    finally:
        s.close()
    assert "TRAINING GAP" in ctx


# ─── 4. heartbeat: OPEN THREAD expires at the day boundary ───────────────────

def _seed(db, user_id, direction, when, body, message_type="freeform"):
    from models import Message
    db.add(Message(user_id=user_id, direction=direction, body=body, message_type=message_type, created_at=when))
    db.commit()


def test_open_thread_today_is_open_not_expired(db):
    import heartbeat
    from timefmt import local_day_bounds
    s, user = _ctx_user(db, **AISLINN)
    try:
        # "today" is the user's LOCAL day: between local midnight and ~03:00 (07:00–10:00
        # UTC in CI) a "2 hours ago" seed lands on yesterday and reads EXPIRED. Keep both
        # seeds inside today's local window (CI failed 2026-09-23 08:00 UTC).
        start, _end = local_day_bounds(user)
        asked = max(_now() - timedelta(hours=2), start + timedelta(minutes=30))
        _seed(s, user.id, "in", asked - timedelta(hours=1) if asked - timedelta(hours=1) >= start else start + timedelta(minutes=5),
              "During lunch I made a homemade McMuffin")
        _seed(s, user.id, "out", asked, "how many egg whites and how much ham")
        sig = heartbeat._open_thread_signal(user, s)
    finally:
        s.close()
    assert sig and "OPEN THREAD" in sig and "EXPIRED" not in sig, sig
    assert re.search(r"\b\d+(\.\d+)? ?h", sig) or "hour" in sig, sig  # carries its age


def test_open_thread_from_a_previous_local_day_is_expired(db):
    import heartbeat
    from timefmt import local_day_bounds
    s, user = _ctx_user(db, **AISLINN)
    try:
        start, _end = local_day_bounds(user)
        _seed(s, user.id, "in", start - timedelta(hours=8), "Nah bruh")
        _seed(s, user.id, "out", start - timedelta(hours=7), "how many egg whites and how much ham")
        sig = heartbeat._open_thread_signal(user, s)
        ctx = heartbeat._proactive_context(user, s)
    finally:
        s.close()
    assert sig and "EXPIRED" in sig, sig
    assert "reopen" in sig.lower() or "re-ask" in sig.lower(), sig
    assert "EXPIRED" in ctx


def test_open_thread_absent_when_answered_or_not_a_question(db):
    import heartbeat
    s, user = _ctx_user(db, **AISLINN)
    try:
        _seed(s, user.id, "out", _now() - timedelta(hours=5), "how many egg whites and how much ham")
        _seed(s, user.id, "in", _now() - timedelta(hours=4), "90g egg white and 55g of turkey breast")
        assert heartbeat._open_thread_signal(user, s) is None
        _seed(s, user.id, "out", _now() - timedelta(hours=3), "logged, ~240 cal 27g protein")
        assert heartbeat._open_thread_signal(user, s) is None
    finally:
        s.close()


# ─── 3. re-estimate write-back ───────────────────────────────────────────────

def _seed_meal(db, user_id, desc, cal, pro, when):
    from models import Meal
    m = Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro, carbs_g=20, fat_g=6,
             source="text", log_type="user_reported", eaten_at=when, logged_at=when)
    db.add(m)
    db.commit()
    return m.id


def _usda_stub(monkeypatch):
    import config, usda
    monkeypatch.setattr(config, "USDA_API_KEY", "test-key")
    monkeypatch.setattr(usda, "search_usda", lambda q: [
        {"description": "Egg, white, raw, fresh", "data_type": "Foundation", "calories": 52,
         "protein_g": 10.9, "carbs_g": 0.7, "fat_g": 0.2}])


def test_usda_lookup_names_the_logged_row_it_may_correct(db, monkeypatch):
    from agent_tools import handle_usda_food_lookup
    from timefmt import local_day_bounds
    _usda_stub(monkeypatch)
    user = make_user(db, **AISLINN)
    start, _ = local_day_bounds(user)
    mid = _seed_meal(db, user.id, "homemade McMuffin, ham + egg whites on english muffin", 260, 22,
                     start - timedelta(hours=7))  # yesterday, local
    out = handle_usda_food_lookup(user.id, {"query": "egg white raw"})
    assert out.startswith("ok:")
    assert f"[id {mid}]" in out and "manage_log" in out, out
    assert "260" in out and "22" in out, "the current numbers must be quoted so the diff is visible"


def test_usda_lookup_stays_quiet_without_an_overlapping_row(db, monkeypatch):
    from agent_tools import handle_usda_food_lookup
    from models import get_session, Meal
    _usda_stub(monkeypatch)
    user = make_user(db, **AISLINN)
    _seed_meal(db, user.id, "overnight protein oats", 350, 25, _now() - timedelta(hours=2))
    out = handle_usda_food_lookup(user.id, {"query": "egg white raw"})
    assert "manage_log" not in out, out
    # a soft-deleted match is not offered either
    mid = _seed_meal(db, user.id, "egg whites and toast", 200, 20, _now() - timedelta(hours=1))
    s = get_session()
    try:
        s.get(Meal, mid).deleted_at = _now(); s.commit()
    finally:
        s.close()
    assert "manage_log" not in handle_usda_food_lookup(user.id, {"query": "egg white raw"})


def test_context_lists_yesterdays_meals_separately_from_todays_totals(db):
    from agent_loop import build_loop_context
    from models import get_session, User
    from timefmt import local_day_bounds
    user = make_user(db, **AISLINN)
    start, _ = local_day_bounds(user)
    y = _seed_meal(db, user.id, "homemade McMuffin, ham + egg whites", 260, 22, start - timedelta(hours=7))
    t = _seed_meal(db, user.id, "overnight protein oats", 350, 25, start + timedelta(hours=2))
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "YESTERDAY'S LOGGED MEALS" in ctx
    yblock = ctx.split("YESTERDAY'S LOGGED MEALS", 1)[1].split("##", 1)[0]
    assert f"[id {y}]" in yblock and f"[id {t}]" not in yblock
    assert "calories: 350 |" in ctx, "today's totals exclude yesterday"
    assert "[id %d]" % t in ctx.split("TODAY'S LOGGED MEALS", 1)[1].split("##", 1)[0]


def test_voice_carries_the_reestimate_and_expired_question_rules():
    text = open("prompts/voice.md", encoding="utf-8").read().lower()
    assert "re-estimate" in text and "manage_log" in text
    assert "previous day" in text or "past day" in text


# ─── 5. legacy classifier word boundaries ─────────────────────────────────────

@pytest.mark.parametrize("body,expected", [
    ("During lunch I made a homemade McMuffin with ham and only egg whites then I drank a coffee", "freeform"),
    ("90g egg white and 55g of turkey breast", "freeform"),
    ("the recipe was a hit with my roommates", "workout_log"),   # a real word still matches
    ("hit 185 on bench today", "workout_log"),
    ("did 3 sets of pushups", "workout_log"),
    ("upset stomach after the impressive dinner", "freeform"),  # 'set' in upset, 'press' in impressive
])
def test_classifier_keywords_are_whole_words(body, expected):
    from app import classify_message
    assert classify_message(body) == expected


# ─── 6. /admin/send double-post guard ─────────────────────────────────────────

def test_admin_send_dedupes_a_double_submit(db, client, sms_capture):
    import app as appmod
    appmod._admin_send_recent.clear()
    user = make_user(db)
    r1 = client.post("/admin/send", data={"user_id": user.id, "body": "hey, just checking in"})
    r2 = client.post("/admin/send", data={"user_id": user.id, "body": "hey, just checking in"})
    assert r1.status_code == 200 and r1.get_json()["status"] == "ok"
    assert r2.status_code == 200 and r2.get_json()["status"] == "duplicate"
    assert len(sms_capture) == 1
    r3 = client.post("/admin/send", data={"user_id": user.id, "body": "got any food you want me to log?"})
    assert r3.get_json()["status"] == "ok" and len(sms_capture) == 2


def test_admin_send_forms_guard_in_flight():
    import app as appmod, admin_dashboard
    for src in (appmod.USER_DETAIL_TEMPLATE if hasattr(appmod, "USER_DETAIL_TEMPLATE") else "",
                open("app.py", encoding="utf-8").read(), open("admin_dashboard.py", encoding="utf-8").read()):
        pass
    html = open("app.py", encoding="utf-8").read() + open("admin_dashboard.py", encoding="utf-8").read()
    assert html.count("/admin/send") >= 4  # 3 forms + the route
    assert html.count("_sendInFlight") >= 3, "every send form must carry the in-flight guard"


# ─── 7. small ─────────────────────────────────────────────────────────────────

def test_sanitizer_neutralizes_gendered_pronouns():
    from memory import sanitize_facts
    facts = [
        {"action": "add", "category": "training_preferences",
         "text": "tracks calories mentally but often doesn't log them in his calorie-counting app"},
        {"action": "add", "category": "constraints", "text": "hurt her knee skiing", "safety_critical": True},
        {"action": "add", "category": "identity", "text": "lives with himself and a cat"},
    ]
    kept, rejected = sanitize_facts(facts)
    assert rejected == 0
    texts = [f["text"] for f in kept]
    assert texts[0].endswith("their calorie-counting app"), texts[0]
    assert texts[1] == "hurt their knee skiing"
    assert "themself" in texts[2]
    assert not any(w in " ".join(texts).split() for w in ("his", "her", "him", "himself"))


def test_extractor_prompt_asks_for_pronoun_neutral_facts():
    src = open("app.py", encoding="utf-8").read()
    assert "his/her" in src or "gendered pronoun" in src.lower()


def test_first_weigh_in_anchors_weigh_in_day_and_computed_protein_follows(db):
    from agent_tools import handle_log_weight
    from models import get_session, User
    from macro_calculator import calculate_targets
    user = make_user(db, **AISLINN, protein_target=180, protein_target_computed=180, targets_source="computed",
                     calorie_target=1400, calorie_target_computed=1400)
    tz = ZoneInfo(AISLINN["user_timezone"])
    today_name = datetime.now(tz).strftime("%A").lower()
    out = handle_log_weight(user.id, {"weight_lbs": 172.6})
    assert out.startswith("ok:") and today_name in out, out
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.weigh_in_day == today_name
        expected = calculate_targets(u)["protein"]
        assert u.protein_target == expected and u.protein_target_computed == expected
        assert u.protein_target_computed != 180
    finally:
        s.close()
    # a later reading never moves an existing anchor
    handle_log_weight(user.id, {"weight_lbs": 171.0})
    s = get_session()
    try:
        assert s.get(User, user.id).weigh_in_day == today_name
    finally:
        s.close()


def test_weigh_in_day_preset_is_respected(db):
    from agent_tools import handle_log_weight
    from models import get_session, User
    user = make_user(db, **AISLINN, weigh_in_day="monday")
    handle_log_weight(user.id, {"weight_lbs": 172.6})
    s = get_session()
    try:
        assert s.get(User, user.id).weigh_in_day == "monday"
    finally:
        s.close()


# ─── 3b. write-back guard: quoting a re-estimate without editing → one forced follow-up ──

def _loop_flags(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
              "USDA_LOOKUP_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def test_writeback_guard_forces_the_edit_once(db, monkeypatch, anthropic_stub):
    """usda named the logged row; the model replies with a new number and no edit →
    the loop sends ONE code-check message; the model then edits and replies."""
    from tests._fake_anthropic import ToolUse
    from agent_loop import run_agent_loop
    from models import get_session, User, Meal
    from timefmt import local_day_bounds
    _loop_flags(monkeypatch); _usda_stub(monkeypatch)
    user = make_user(db, **AISLINN)
    start, _ = local_day_bounds(user)
    mid = _seed_meal(db, user.id, "homemade McMuffin, ham + egg whites", 260, 22, start - timedelta(hours=7))

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(kw["messages"][-1])   # snapshot: the loop mutates `messages` in place
        n = len(loop_calls)
        if n == 1:
            return ToolUse("usda_food_lookup", {"query": "egg white raw"})
        if n == 2:
            return "comes out ~240 cal, 24g protein. where'd 77 come from"   # number, no edit
        if n == 3:
            return ToolUse("manage_log", {"action": "edit", "entity": "meal", "id": mid,
                                          "fields": {"calories": 240, "protein_g": 24}})
        return "updated it, 240 cal 24g"

    anthropic_stub.reply_with(handler)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, user.id), "90g egg white and 55g of turkey breast", "freeform")
    finally:
        s.close()
    assert reply == "updated it, 240 cal 24g"
    assert len(loop_calls) == 4
    nudge = loop_calls[2]["content"]
    assert isinstance(nudge, str) and "code check" in nudge and f"id {mid}" in nudge and "manage_log" in nudge
    s = get_session()
    try:
        row = s.get(Meal, mid)
        assert row.calories == 240 and row.protein_g == 24 and row.edits
    finally:
        s.close()


def test_writeback_guard_never_loops_and_skips_when_edited_or_numberless(db, monkeypatch, anthropic_stub):
    from tests._fake_anthropic import ToolUse
    from agent_loop import run_agent_loop
    from models import get_session, User
    from timefmt import local_day_bounds
    _loop_flags(monkeypatch); _usda_stub(monkeypatch)
    user = make_user(db, **AISLINN)
    start, _ = local_day_bounds(user)
    mid = _seed_meal(db, user.id, "homemade McMuffin, ham + egg whites", 260, 22, start - timedelta(hours=7))

    # (a) the model ignores the nudge and restates the number: ONE nudge, then the reply goes out
    calls = []

    def stubborn(kw):
        if not kw.get("tools"):
            return "freeform"
        calls.append(kw)
        return ToolUse("usda_food_lookup", {"query": "egg white raw"}) if len(calls) == 1 else "~240 cal, 24g"
    anthropic_stub.reply_with(stubborn)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, user.id), "90g egg white", "freeform")
    finally:
        s.close()
    assert reply == "~240 cal, 24g" and len(calls) == 3, "exactly one forced follow-up, never a loop"

    # (b) edited first → no nudge at all
    calls.clear()

    def diligent(kw):
        if not kw.get("tools"):
            return "freeform"
        calls.append(kw)
        if len(calls) == 1:
            return ToolUse("usda_food_lookup", {"query": "egg white raw"})
        if len(calls) == 2:
            return ToolUse("manage_log", {"action": "edit", "entity": "meal", "id": mid, "fields": {"protein_g": 24}})
        return "updated, 24g"
    anthropic_stub.reply_with(diligent)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, user.id), "90g egg white", "freeform")
    finally:
        s.close()
    assert reply == "updated, 24g" and len(calls) == 3

    # (c) no number in the reply → nothing to write back, no nudge
    calls.clear()

    def numberless(kw):
        if not kw.get("tools"):
            return "freeform"
        calls.append(kw)
        return ToolUse("usda_food_lookup", {"query": "egg white raw"}) if len(calls) == 1 else "how much turkey was it"
    anthropic_stub.reply_with(numberless)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, user.id), "90g egg white", "freeform")
    finally:
        s.close()
    assert reply == "how much turkey was it" and len(calls) == 2
