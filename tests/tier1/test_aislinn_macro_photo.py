"""
Aislinn macro-accuracy photo fixes (rewrite/aislinn-macro-photo/CHANGESPEC.md).

Tier-1: deterministic state + tool-result affordances + prompt pins. The model's
judgment (does it edit the row, name every guess, reconcile against the app) is
tier-2 (tests/tier2/test_aislinn_macro_photo_live.py).

  §1 portion_guessed → confidence="low" + a one-line naming affordance + block marker
  §2 source provenance: photo / text / app
  §3 diary screenshot: slot refusal in code, from_app writes, PARITY suffix + Signal
  §4 estimates carry all four macros (app rows exempt)
  §5 protein-left is for when it matters, not after every log (suffix wording)
  §6 prompt + schema pins
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from tests.factories import make_user

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}

FULL = dict(calories=100, protein_g=10, carbs_g=10, fat_g=2)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _loop_flags(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
              "READ_IMAGE_ENABLED", "MEAL_ESTIMATION_PROMPT_ENABLED"):
        monkeypatch.setattr(config, f, True)


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
        m = Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro, carbs_g=carbs,
                 fat_g=fat, source=source, log_type="user_reported", confidence=confidence,
                 eaten_at=when, logged_at=when)
        s.add(m)
        s.commit()
        return m.id
    finally:
        s.close()


def _local(user, hour, minute=0):
    """Naive-UTC instant for today at HH:MM in the user's local day."""
    from timefmt import local_day_bounds
    start, _ = local_day_bounds(user)
    return start + timedelta(hours=hour, minutes=minute)


def _fixed_now(monkeypatch, user, hour):
    """Pin log_meal's 'now' to today HH:00 local so the slot check is deterministic."""
    import agent_tools
    when = _local(user, hour)
    monkeypatch.setattr(agent_tools, "_naive_utcnow", lambda: when)
    return when


# ─── §1 portion provenance ───────────────────────────────────────────────────

def test_portion_guessed_is_stored_and_named_for_a_one_line_correction(db):
    from agent_tools import handle_log_meal
    user = make_user(db)
    out = handle_log_meal(user.id, {"items": [
        {"description": "scrambled eggs (~2)", "portion_guessed": True, **FULL},
        {"description": "greek yogurt (~3/4 cup)", "portion_guessed": True, **FULL},
        {"description": "sourdough toast, 2 slices (170 cal printed)", **FULL},
    ]})
    assert out.startswith("ok"), out
    rows = _meals(user.id)
    conf = {r.description: r.confidence for r in rows}
    assert conf["scrambled eggs (~2)"] == "low" and conf["greek yogurt (~3/4 cup)"] == "low"
    assert conf["sourdough toast, 2 slices (170 cal printed)"] is None
    # the affordance: every guessed item named with its id, and the ONE-line instruction
    ids = {r.description: r.id for r in rows}
    assert "portions guessed" in out and "ONE line" in out, out
    assert f"'scrambled eggs (~2)' [id {ids['scrambled eggs (~2)']}]" in out, out
    assert f"'greek yogurt (~3/4 cup)' [id {ids['greek yogurt (~3/4 cup)']}]" in out, out
    assert "sourdough" not in out.split("portions guessed")[1], "a stated/printed item is not a guess"


def test_no_guess_no_affordance(db):
    from agent_tools import handle_log_meal
    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "224g 93/7 ground turkey", **FULL})
    assert "portions guessed" not in out, out


def test_meals_block_marks_guessed_and_app_rows(db):
    from agent_loop import build_loop_context
    from models import get_session, User
    user = make_user(db)
    g = _seed_meal(user.id, "scrambled eggs (~2)", 180, 12, _now(), confidence="low")
    a = _seed_meal(user.id, "turkey sandwich", 551, 54, _now(), source="app", confidence="high")
    t = _seed_meal(user.id, "150g watermelon", 45, 1, _now(), source="text")
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    block = ctx.split("TODAY'S LOGGED MEALS")[1].split("## TODAY'S TOTALS")[0]
    line = {int(l.split("[id ")[1].split("]")[0]): l for l in block.splitlines() if "[id " in l}
    assert "(portion guessed)" in line[g], line[g]
    assert "(from their app)" in line[a], line[a]
    assert "(portion guessed)" not in line[t] and "(from their app)" not in line[t], line[t]


# ─── §2 source provenance ────────────────────────────────────────────────────

def test_source_is_photo_on_an_image_turn_and_text_otherwise(db, monkeypatch, anthropic_stub):
    from tests._fake_anthropic import ToolUse
    from agent_loop import run_agent_loop
    from models import get_session, User
    _loop_flags(monkeypatch)
    user = make_user(db)
    n = {"i": 0}

    def handler(kw):
        n["i"] += 1
        if n["i"] % 2 == 1:
            return ToolUse("log_meal", {"description": f"meal {n['i']}", **FULL})
        return "logged"

    anthropic_stub.reply_with(handler)
    s = get_session()
    try:
        run_agent_loop(s.get(User, user.id), "brunch", "freeform", image_data=_IMG)
        run_agent_loop(s.get(User, user.id), "and a banana", "freeform")
    finally:
        s.close()
    rows = _meals(user.id)
    assert [r.source for r in rows] == ["photo", "text"], [(r.description, r.source) for r in rows]


def test_from_app_write_is_tagged_app_reported(db):
    from agent_tools import handle_log_meal
    user = make_user(db)
    out = handle_log_meal(user.id, {"from_app": "mynetdiary", "items": [
        {"description": "Liquid egg whites, 130 g", "calories": 68, "protein_g": 14, "carbs_g": 1, "fat_g": 0},
        {"description": "Herb roasted turkey breast, 2 servings", "calories": 120, "protein_g": 24,
         "carbs_g": 2, "fat_g": 2},
    ]})
    assert out.startswith("ok"), out
    assert "source: their mynetdiary screenshot" in out, out
    for r in _meals(user.id):
        assert (r.source, r.log_type, r.confidence) == ("app", "app_reported", "high"), r.description
        assert "from_app=mynetdiary" in (r.notes or "")


# ─── §3 slot refusal + replace-not-add ───────────────────────────────────────

def test_slot_refusal_names_the_estimate_and_writes_nothing(db, monkeypatch):
    """The Sep 19 case: a photo-estimated lunch row exists; a diary screenshot of the same
    slot must NOT become a second row — code refuses and points at the edit."""
    from agent_tools import handle_log_meal
    from models import get_session, User
    user = make_user(db, calorie_target=1400, protein_target=173)
    when = _fixed_now(monkeypatch, user, 13)                    # 13:00 local = lunch
    est = _seed_meal(user.id, "chicken + egg sandwich: 2 slices whole grain toast, grilled chicken ~3oz",
                     430, 43, _local(user, 12, 8), confidence="low")
    out = handle_log_meal(user.id, {"from_app": "mynetdiary", "items": [
        {"description": "Sprouted multigrain bread, 2 slices", "calories": 160},
        {"description": "Liquid egg whites, 130 g", "calories": 68},
    ]})
    assert out.startswith("error"), out
    assert f"[id {est}]" in out and "lunch" in out and "manage_log edit" in out and "saw_similar" in out, out
    assert "your own estimate" in out and "portion guessed" in out, out
    assert len(_meals(user.id)) == 1, "the screenshot must not add a row"
    s = get_session()
    try:
        assert s.get(User, user.id).calories_today in (None, 0, 430)   # totals untouched
    finally:
        s.close()


def test_slot_refusal_bypass_with_saw_similar_and_other_slot_writes(db, monkeypatch):
    from agent_tools import handle_log_meal
    user = make_user(db)
    _fixed_now(monkeypatch, user, 13)
    est = _seed_meal(user.id, "chicken + egg sandwich", 430, 43, _local(user, 12, 8), confidence="low")
    # they ate both → the model says so explicitly
    out = handle_log_meal(user.id, {"from_app": "mynetdiary", "saw_similar": [est],
                                    "description": "protein bar", "calories": 200})
    assert out.startswith("ok"), out
    # a breakfast screenshot at 13:00 (explicit slot) doesn't collide with the lunch estimate
    out2 = handle_log_meal(user.id, {"from_app": "mynetdiary", "slot": "breakfast",
                                     "description": "overnight oats", "calories": 350})
    assert out2.startswith("ok"), out2
    # clock-slot: 18:00 local is dinner → no lunch collision
    _fixed_now(monkeypatch, user, 18)
    out3 = handle_log_meal(user.id, {"from_app": "mynetdiary", "description": "turkey bowl", "calories": 615})
    assert out3.startswith("ok"), out3
    assert len(_meals(user.id)) == 4


def test_slot_refusal_also_guards_an_existing_app_row(db, monkeypatch):
    """A second screenshot of the same slot (the macro view) edits the app row, never adds."""
    from agent_tools import handle_log_meal
    user = make_user(db)
    _fixed_now(monkeypatch, user, 13)
    app_row = _seed_meal(user.id, "turkey sandwich", 551, None, _local(user, 12, 30), source="app",
                         confidence="high")
    out = handle_log_meal(user.id, {"from_app": "mynetdiary", "description": "turkey sandwich",
                                    "calories": 551, "protein_g": 54})
    assert out.startswith("error") and f"[id {app_row}]" in out and "already from their app" in out, out
    assert len(_meals(user.id)) == 1


def test_manage_log_edit_from_app_replaces_estimate_with_parity(db, monkeypatch):
    from agent_tools import handle_manage_log
    from models import get_session, Meal, Signal, User
    user = make_user(db, calorie_target=1400, protein_target=173)
    est = _seed_meal(user.id, "chicken + egg sandwich", 430, 43, _local(user, 12, 8), confidence="low",
                     carbs=30, fat=17)
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": est, "from_app": "mynetdiary",
                                      "fields": {"calories": 551, "protein_g": 54, "carbs_g": 44, "fat_g": 17,
                                                 "description": "turkey + egg white sandwich (MyNetDiary)"}})
    assert out.startswith("ok"), out
    assert "PARITY" in out and "430" in out and "551" in out and "+28%" in out, out
    assert "never argue" in out, out
    assert "DAY TOTAL NOW: 551 cal, 54g protein" in out, out
    s = get_session()
    try:
        row = s.get(Meal, est)
        assert (row.calories, row.protein_g, row.source, row.log_type, row.confidence) == \
               (551, 54, "app", "app_reported", "high")
        assert "from_app=mynetdiary" in (row.notes or "")
        assert row.edits and any(e["field"] == "calories" and e["old"] == 430 for e in row.edits)
        sig = s.query(Signal).filter_by(user_id=user.id, kind="parity").one()
        assert sig.payload["meal_id"] == est and sig.payload["cued_cal"] == 430 \
            and sig.payload["app_cal"] == 551 and sig.payload["delta_pct"] == 28
        assert s.get(User, user.id).calories_today == 551
    finally:
        s.close()
    assert len(_meals(user.id)) == 1


def test_parity_close_branch_and_no_parity_without_from_app(db):
    from agent_tools import handle_manage_log
    from models import get_session, Signal
    user = make_user(db)
    a = _seed_meal(user.id, "bowl", 640, 40, _now(), confidence="low")
    b = _seed_meal(user.id, "wrap", 600, 30, _now(), confidence="low")
    close = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": a, "from_app": "myfitnesspal",
                                        "fields": {"calories": 610}})
    assert "PARITY" in close and "-5%" in close and "close" in close, close
    plain = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": b,
                                        "fields": {"calories": 500}})
    assert "PARITY" not in plain, plain
    s = get_session()
    try:
        assert s.query(Signal).filter_by(user_id=user.id, kind="parity").count() == 1
    finally:
        s.close()


def test_from_app_partial_macros_stay_null(db, monkeypatch):
    """Printed-only: a screenshot that shows calories but no macros → NULL, never a guess
    written as a number; and the §4 all-four rule does not apply to app rows."""
    from agent_tools import handle_log_meal
    user = make_user(db)
    _fixed_now(monkeypatch, user, 9)
    out = handle_log_meal(user.id, {"from_app": "mynetdiary", "description": "Lunch total", "calories": 551})
    assert out.startswith("ok"), out
    row = _meals(user.id)[0]
    assert row.calories == 551 and row.protein_g is None and row.carbs_g is None and row.fat_g is None


def test_sep19_replay_through_the_loop(db, monkeypatch, anthropic_stub):
    """Harness fidelity: the refusal reaches the model as a tool result, the model's edit
    lands on the estimate row, and the final state is ONE row at the printed numbers."""
    from tests._fake_anthropic import ToolUse
    from agent_loop import run_agent_loop
    from models import get_session, User
    _loop_flags(monkeypatch)
    user = make_user(db, calorie_target=1400, protein_target=173)
    _fixed_now(monkeypatch, user, 13)
    est = _seed_meal(user.id, "chicken + egg sandwich", 430, 43, _local(user, 12, 8), confidence="low")
    calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        calls.append(kw["messages"][-1])
        n = len(calls)
        if n == 1:
            return ToolUse("log_meal", {"from_app": "mynetdiary", "items": [
                {"description": "turkey + egg white sandwich", "calories": 551, "protein_g": 54,
                 "carbs_g": 44, "fat_g": 17}]})
        if n == 2:
            res = calls[1]["content"][0]["content"]
            assert res.startswith("error") and f"id {est}" in res, res
            return ToolUse("manage_log", {"action": "edit", "entity": "meal", "id": est, "from_app": "mynetdiary",
                                          "fields": {"calories": 551, "protein_g": 54, "carbs_g": 44, "fat_g": 17,
                                                     "description": "turkey + egg white sandwich"}})
        res = calls[2]["content"][0]["content"]
        assert "PARITY" in res and "+28%" in res, res
        return "551 for the day. i had it as chicken at 430, ur app's right"

    anthropic_stub.reply_with(handler)
    s = get_session()
    try:
        reply = run_agent_loop(s.get(User, user.id), "", "freeform", image_data=_IMG)
    finally:
        s.close()
    assert reply.startswith("551")
    rows = _meals(user.id)
    assert len(rows) == 1 and rows[0].id == est and rows[0].calories == 551 and rows[0].source == "app"


# ─── §4 all four macros on an estimate ───────────────────────────────────────

def test_estimate_missing_a_macro_is_rejected_atomically(db):
    from agent_tools import handle_log_meal
    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "90g egg whites", "calories": 47, "protein_g": 10})
    assert out.startswith("error") and "90g egg whites" in out and "fat_g" in out and "0 is fine" in out, out
    assert _meals(user.id) == []
    batch = handle_log_meal(user.id, {"items": [
        {"description": "chicken", **FULL},
        {"description": "rice", "calories": 200, "protein_g": 4, "carbs_g": 45},   # fat missing
    ]})
    assert batch.startswith("error") and "'rice'" in batch, batch
    assert _meals(user.id) == [], "a batch with one bad item writes nothing"
    ok = handle_log_meal(user.id, {"description": "90g egg whites", "calories": 47, "protein_g": 10,
                                   "carbs_g": 1, "fat_g": 0})
    assert ok.startswith("ok"), ok


# ─── §5 suffix wording ───────────────────────────────────────────────────────

def test_day_total_suffix_keeps_the_number_but_not_the_reflex(db):
    from agent_tools import handle_log_meal
    user = make_user(db, protein_target=173)
    out = handle_log_meal(user.id, {"description": "eggs", "calories": 147, "protein_g": 13, "carbs_g": 1,
                                    "fat_g": 10})
    assert "160g protein left of 173" in out, out
    assert "not a line after every log" in out, out


# ─── §6 prompt + schema pins ─────────────────────────────────────────────────

def test_prompt_and_schema_pins():
    import os
    from agent_tools import LOG_MEAL_TOOL, MANAGE_LOG_TOOL
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    voice = open(os.path.join(root, "prompts", "voice.md")).read()
    est = open(os.path.join(root, "prompts", "meal_estimation.md")).read()
    # §3 diary screenshot rule + the honest "connect my app" answer
    assert "screenshot of another food app" in voice.lower() or "food app's diary" in voice.lower(), \
        "voice.md has no diary-screenshot image rule"
    assert "from_app" in voice and "printed numbers" in voice.lower()
    assert "connect" in voice.lower() and "screenshot" in voice.lower()
    # §6 their tracker's total vs yours
    assert "their own tracker" in voice.lower() or "their app's total" in voice.lower() or \
        "their tracker" in voice.lower(), "voice.md has no 'your app says X' reconcile rule"
    # §5 protein gap once
    assert "after every" in voice.lower() and "protein" in voice.lower()
    # §1 estimation prompt: flag the guess, name every guess in one line, log now
    assert "portion_guessed" in est and "one line" in est.lower()
    assert "diary" in est.lower(), "meal_estimation.md must hand a diary screenshot to the voice.md rule"
    props = LOG_MEAL_TOOL["input_schema"]["properties"]
    assert "from_app" in props and "slot" in props
    assert "portion_guessed" in props["items"]["items"]["properties"] and "portion_guessed" in props
    assert "from_app" in MANAGE_LOG_TOOL["input_schema"]["properties"]
