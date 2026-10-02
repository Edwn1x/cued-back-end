"""
Phase 3 — log_meal (note #2 vehicle). The model does the read-before-write
judgment (today's meals are injected into its context); this handler writes the
meal and records saw_similar so an intentional near-duplicate is auditable.
"""

from __future__ import annotations


def _active_meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).all()
    finally:
        s.close()


def test_log_meal_creates_and_recomputes(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    from models import get_session, User

    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "chicken bowl", "calories": 600, "protein_g": 45, "carbs_g": 0, "fat_g": 0})
    assert out.startswith("ok"), out

    s = get_session()
    try:
        u = s.query(User).get(user.id)
    finally:
        s.close()
    assert u.calories_today == 600 and u.protein_today == 45
    assert len(_active_meals(user.id)) == 1


def test_log_meal_legit_second_serving_records_saw_similar(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    from models import get_session, User

    user = make_user(db)
    handle_log_meal(user.id, {"description": "protein shake", "calories": 300, "protein_g": 30, "carbs_g": 0, "fat_g": 0})
    first_id = _active_meals(user.id)[0].id

    # model saw the first shake and judged this a distinct second serving
    out = handle_log_meal(user.id, {"description": "protein shake", "calories": 300,
                                    "protein_g": 30, "carbs_g": 0, "fat_g": 0, "saw_similar": [first_id]})
    assert "saw_similar" in out, out

    meals = _active_meals(user.id)
    assert len(meals) == 2, "a legitimate second serving must not be silently dropped"
    s = get_session()
    try:
        assert s.query(User).get(user.id).calories_today == 600  # both counted
    finally:
        s.close()
    assert any("saw_similar" in (m.notes or "") for m in meals), "saw_similar not recorded for audit"


def test_log_meal_via_loop(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("log_meal", {"description": "chipotle burrito bowl",
                                        "calories": 700, "protein_g": 50, "carbs_g": 0, "fat_g": 0})
        return "logged, that's 700 cal / 50g — solid lunch"

    anthropic_stub.reply_with(handler)
    user = make_user(db)
    driver.send(user, "just had a chipotle burrito bowl")
    assert len(_active_meals(user.id)) == 1, "log_meal tool did not persist the meal"


def test_log_meal_batch_items_recomputes_once(db):
    """A multi-item plate via `items` → one Meal each, totals recomputed once."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    from models import get_session, User

    user = make_user(db)
    out = handle_log_meal(user.id, {"items": [
        {"description": "chicken", "calories": 400, "protein_g": 40, "carbs_g": 0, "fat_g": 0},
        {"description": "rice", "calories": 200, "protein_g": 5, "carbs_g": 0, "fat_g": 0},
        {"description": "coke", "calories": 140, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
    ]})
    assert out.startswith("ok") and "3 items" in out, out
    assert len(_active_meals(user.id)) == 3
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.calories_today == 740 and u.protein_today == 45  # summed once from all three
    finally:
        s.close()


def test_log_meal_return_names_the_item(db):
    """The tool result must NAME what was logged (not just macros) so the coach's
    confirmation can say 'logged the chicken wrap, ~650 cal' — founder feedback
    2026-09-18: a macros-only confirmation can't be verified without asking."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "chicken caesar wrap", "calories": 650, "protein_g": 38, "carbs_g": 0, "fat_g": 0})
    assert "chicken caesar wrap" in out, out
    assert "650cal" in out

    # batch form names each item too
    out2 = handle_log_meal(user.id, {"items": [
        {"description": "banana", "calories": 100, "protein_g": 1, "carbs_g": 0, "fat_g": 0},
        {"description": "greek yogurt", "calories": 150, "protein_g": 15, "carbs_g": 0, "fat_g": 0},
    ]})
    assert "banana" in out2 and "greek yogurt" in out2, out2


def test_log_meal_returns_fresh_day_total(db):
    """The tool result must hand back the recomputed DAY TOTAL NOW so the coach quotes
    it instead of hand-adding to the (turn-start-stale) totals block — the 2026-09-19
    protein-drift fix. A running sequence must reflect the cumulative total."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    user = make_user(db, protein_target=140)
    out1 = handle_log_meal(user.id, {"description": "eggs", "calories": 300, "protein_g": 20, "carbs_g": 0, "fat_g": 0})
    assert "DAY TOTAL NOW: 300 cal, 20g protein" in out1, out1
    out2 = handle_log_meal(user.id, {"description": "chicken", "calories": 500, "protein_g": 45, "carbs_g": 0, "fat_g": 0})
    assert "DAY TOTAL NOW: 800 cal, 65g protein" in out2, out2   # cumulative, not just this meal
    assert "75g protein left of 140" in out2, out2


def test_manage_log_edit_meal_returns_fresh_day_total(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal, handle_manage_log
    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "bowl", "calories": 600, "protein_g": 45, "carbs_g": 0, "fat_g": 0})
    mid = int(out.split("id=")[1].split(" ")[0])
    edited = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                         "fields": {"calories": 400, "protein_g": 30, "carbs_g": 0, "fat_g": 0}})
    assert "DAY TOTAL NOW: 400 cal, 30g protein" in edited, edited


# ── past-day totals in the tool result (live 2026-10-02, after-midnight "yesterday's tab") ──
#
# The DAY TOTAL NOW affordance was today-only: a meal logged/moved/edited on a PAST day got
# either no total (log_meal) or TODAY's total (manage_log), so the coach ran the arithmetic
# itself and drifted (said 2490/2565 when yesterday was really 2745/2820). Every meal write
# now returns the AFFECTED day's labeled total, summed from the DB by LOCAL nutrition day.

def _yesterday_local(tz_str="America/Los_Angeles"):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz_str)).date() - timedelta(days=1)


def _db_day_total(user_id, day):
    """The DB truth for a LOCAL day: sum of active meals in that local day's UTC window."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from models import get_session, Meal, User, active
    from timefmt import local_day_bounds, resolve_tz
    s = get_session()
    try:
        u = s.get(User, user_id)
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=resolve_tz(u))
        start, end = local_day_bounds(u, now=noon)
        rows = active(s, Meal, user_id=user_id).filter(Meal.eaten_at >= start, Meal.eaten_at < end).all()
        return sum(m.calories or 0 for m in rows), sum(m.protein_g or 0 for m in rows)
    finally:
        s.close()


def _ids(out):
    import re
    m = re.search(r"ids \[([\d, ]+)\]", out)
    if m:
        return [int(x) for x in m.group(1).split(",")]
    return [int(out.split("id=")[1].split(" ")[0])]


def _item(desc, cal, pro):
    return {"description": desc, "calories": cal, "protein_g": pro, "carbs_g": 0, "fat_g": 0}


def test_log_meal_to_yesterday_returns_yesterdays_labeled_total_not_todays(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    user = make_user(db, protein_target=140)
    y = _yesterday_local()
    out1 = handle_log_meal(user.id, {**_item("late dinner", 385, 20), "date": "yesterday"})
    assert f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 385 cal, 20g protein — use this exact number for yesterday" in out1, out1
    assert "DAY TOTAL NOW" not in out1, out1               # no bare today total claiming the wrong day
    assert "protein left" not in out1, out1                # targets are today's; no protein-left on a past day
    out2 = handle_log_meal(user.id, {"items": [_item("pupusa", 300, 12), _item("thigh", 320, 28)],
                                     "date": y.isoformat()})
    assert f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 1005 cal, 60g protein" in out2, out2
    assert _db_day_total(user.id, y) == (1005, 60)


def test_live_sequence_yesterdays_tab_move_then_adds_then_edit_match_db_each_step(db, monkeypatch):
    """2026-10-02 founder, eating after midnight: 385 already on yesterday; 4 items (1365)
    logged today then MOVED to yesterday; three more batches added to yesterday; a salad
    edited 15→90. The coach stated 2490 → 2565 while the truth was 2745 → 2820. Every tool
    result must now carry yesterday's exact total: 1750 → 2370 → 2625 → 2745 → 2820."""
    import config
    monkeypatch.setattr(config, "MEAL_GROUP_ENABLED", True)
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_log_meal, handle_manage_log
    from models import get_session, User
    user = make_user(db)
    y = _yesterday_local()
    Y = f"YESTERDAY ({y.isoformat()}) TOTAL NOW: "

    handle_log_meal(user.id, {**_item("late bite", 385, 15), "date": "yesterday"})          # seed yesterday

    out = handle_log_meal(user.id, {"items": [_item("a", 400, 30), _item("b", 365, 20),
                                              _item("c", 300, 25), _item("d", 300, 10)]})   # 1365 today
    assert "DAY TOTAL NOW: 1365 cal, 85g protein" in out, out
    ids = _ids(out)
    assert len(ids) == 4

    moved = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": ids[0],
                                        "scope": "meal", "fields": {"date": "yesterday"}})
    assert moved.startswith("ok: moved meal (4 items"), moved
    assert Y + "1750 cal, 100g protein — use this exact number for yesterday" in moved, moved
    assert "DAY TOTAL NOW: 0 cal, 0g protein" in moved, moved                                 # both days
    assert moved.index("YESTERDAY") < moved.index("DAY TOTAL NOW"), "target day first"
    assert _db_day_total(user.id, y) == (1750, 100)

    out = handle_log_meal(user.id, {"items": [_item("pupusa", 250, 8), _item("thigh", 200, 20),
                                              _item("drumstick", 170, 15)], "date": "yesterday"})
    assert Y + "2370 cal" in out, out
    assert _db_day_total(user.id, y)[0] == 2370

    out = handle_log_meal(user.id, {"items": [_item("salad", 15, 1), _item("rice", 160, 3),
                                              _item("horchata", 80, 1)], "date": "yesterday"})
    assert Y + "2625 cal" in out, out
    salad_id = _ids(out)[0]
    assert _db_day_total(user.id, y)[0] == 2625

    out = handle_log_meal(user.id, {**_item("fried banana", 120, 1), "date": "yesterday"})
    assert Y + "2745 cal" in out, out
    assert _db_day_total(user.id, y)[0] == 2745

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": salad_id,
                                      "fields": {"calories": 90}})
    assert out.startswith("ok: edited"), out
    assert Y + "2820 cal" in out, out
    assert "DAY TOTAL NOW" not in out, out                  # a yesterday edit does not quote today
    assert _db_day_total(user.id, y)[0] == 2820

    s = get_session()
    try:
        u = s.get(User, user.id)
        assert (u.calories_today or 0) == 0, "today's cache must stay 0 through yesterday's writes"
    finally:
        s.close()


def test_edit_and_delete_yesterday_meal_return_yesterdays_total_today_rows_unchanged(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal, handle_manage_log
    user = make_user(db)
    y = _yesterday_local()
    Y = f"YESTERDAY ({y.isoformat()}) TOTAL NOW: "
    y1 = _ids(handle_log_meal(user.id, {**_item("y-a", 500, 30), "date": "yesterday"}))[0]
    y2 = _ids(handle_log_meal(user.id, {**_item("y-b", 300, 10), "date": "yesterday"}))[0]
    t1 = _ids(handle_log_meal(user.id, _item("t-a", 600, 45)))[0]
    t2 = _ids(handle_log_meal(user.id, _item("t-b", 200, 5)))[0]

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": y1, "fields": {"calories": 450}})
    assert Y + "750 cal, 40g protein" in out and "DAY TOTAL NOW" not in out, out
    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": y2})
    assert Y + "450 cal, 30g protein" in out and "DAY TOTAL NOW" not in out, out
    assert _db_day_total(user.id, y) == (450, 30)

    # today rows: byte-identical existing behavior
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": t1, "fields": {"calories": 400, "protein_g": 30}})
    assert out.endswith(" | DAY TOTAL NOW: 600 cal, 35g protein — use this exact number"), out
    assert "YESTERDAY" not in out
    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": t2})
    assert out == f"ok: deleted meal id={t2} | DAY TOTAL NOW: 400 cal, 30g protein — use this exact number", out


def test_single_item_date_move_returns_target_day_then_source_day(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_log_meal, handle_manage_log
    user = make_user(db)
    y = _yesterday_local()
    handle_log_meal(user.id, {**_item("y-base", 100, 5), "date": "yesterday"})
    mid = _ids(handle_log_meal(user.id, _item("eggs", 140, 12)))[0]
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid, "fields": {"date": "yesterday"}})
    assert f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 240 cal, 17g protein" in out, out
    assert "DAY TOTAL NOW: 0 cal, 0g protein" in out, out
    assert out.index("YESTERDAY") < out.index("DAY TOTAL NOW")


def test_today_only_log_day_total_text_is_byte_identical(db):
    """Regression guard for the today path: no label, no date, same text as before."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    user = make_user(db, protein_target=140)
    out = handle_log_meal(user.id, _item("eggs", 300, 20))
    assert out == ("ok: logged 'eggs' id=1 (300cal/20g) | DAY TOTAL NOW: 300 cal, 20g protein, "
                   "120g protein left of 140 — use this exact number (quote the total; the "
                   "protein-left figure is for when they ask, at the evening meal, or when planning "
                   "what to eat — not a line after every log)"), out
    user2 = make_user(db)
    out = handle_log_meal(user2.id, _item("toast", 200, 6))
    assert out.endswith(" | DAY TOTAL NOW: 200 cal, 6g protein — use this exact number"), out


def test_older_past_day_is_labeled_by_its_date_and_summed_by_local_day(db):
    """Not-yesterday → '{date} TOTAL NOW'. Windowing is by LOCAL day: a 23:30-local meal in
    LA sits on the NEXT UTC date; a 00:13-local meal in Tokyo sits on the PREVIOUS UTC date.
    Both must count toward the local calendar day they fall on, not the UTC date."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from tests.factories import make_user
    from agent_tools import _day_total_suffix
    from models import get_session, Meal

    def seed(user_id, local_dt_aware, cal, pro):
        s = get_session()
        try:
            s.add(Meal(user_id=user_id, description="x", calories=cal, protein_g=pro, carbs_g=0, fat_g=0,
                       eaten_at=local_dt_aware.astimezone(timezone.utc).replace(tzinfo=None),
                       source="text", log_type="user_reported"))
            s.commit()
        finally:
            s.close()

    la = make_user(db, user_timezone="America/Los_Angeles")
    tz = ZoneInfo("America/Los_Angeles")
    d = datetime.now(tz).date() - timedelta(days=3)
    late = datetime(d.year, d.month, d.day, 23, 30, tzinfo=tz)           # UTC date = d+1
    assert late.astimezone(timezone.utc).date() == d + timedelta(days=1)
    seed(la.id, late, 700, 40)
    seed(la.id, datetime(d.year, d.month, d.day, 0, 13, tzinfo=tz), 100, 5)
    seed(la.id, datetime(d.year, d.month, d.day, 12, 0, tzinfo=tz) + timedelta(days=1), 999, 99)  # next day
    out = _day_total_suffix(la.id, d)
    assert out == f" | {d.isoformat()} TOTAL NOW: 800 cal, 45g protein — use this exact number for {d.isoformat()}", out
    assert "YESTERDAY" not in out

    tokyo = make_user(db, user_timezone="Asia/Tokyo")
    tzt = ZoneInfo("Asia/Tokyo")
    yt = datetime.now(tzt).date() - timedelta(days=1)
    early = datetime(yt.year, yt.month, yt.day, 0, 13, tzinfo=tzt)       # UTC date = yt-1
    assert early.astimezone(timezone.utc).date() == yt - timedelta(days=1)
    seed(tokyo.id, early, 250, 10)
    seed(tokyo.id, datetime(yt.year, yt.month, yt.day, 19, 0, tzinfo=tzt), 600, 30)
    out = _day_total_suffix(tokyo.id, yt)
    assert out == f" | YESTERDAY ({yt.isoformat()}) TOTAL NOW: 850 cal, 40g protein — use this exact number for yesterday", out


def test_day_total_suffix_today_or_none_is_unchanged_and_fails_open(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from tests.factories import make_user
    from agent_tools import _day_total_suffix, handle_log_meal
    user = make_user(db)
    handle_log_meal(user.id, _item("eggs", 300, 20))
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    assert _day_total_suffix(user.id, today) == _day_total_suffix(user.id) == _day_total_suffix(user.id, None)
    assert _day_total_suffix(user.id, "not-a-date") == ""      # past-day path never breaks the tool result
    assert _day_total_suffix(999999, today) == ""
