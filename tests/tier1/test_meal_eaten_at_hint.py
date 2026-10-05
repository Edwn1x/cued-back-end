"""
eaten_at hints (2026-10-03 rice-krispie): "also ate a Rice Krispie before my run earlier"
at 02:32 — the run was logged 01:33 — wrote the row at 02:32 with the timing in the
DESCRIPTION. The model now passes the cue verbatim as `eaten_at_hint`; code resolves it
(meal_time.py) on log_meal and on a manage_log edit. The clock is pinned through
agent_tools._naive_utcnow (the existing pattern); every assertion is in the user's LOCAL tz.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user


def _item(desc, cal=200, pro=5):
    return {"description": desc, "calories": cal, "protein_g": pro, "carbs_g": 0, "fat_g": 0}


def _pin(monkeypatch, tz_str, hour, minute=0):
    """Pin 'now' to today HH:MM in tz (naive UTC). Returns (now_utc, today_local_date)."""
    import agent_tools
    tz = ZoneInfo(tz_str)
    today = datetime.now(tz).date()
    now_utc = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz) \
        .astimezone(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(agent_tools, "_naive_utcnow", lambda: now_utc)
    return now_utc, today


def _local_at(tz_str, day, hour, minute=0):
    tz = ZoneInfo(tz_str)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz) \
        .astimezone(timezone.utc).replace(tzinfo=None)


def _meal(meal_id):
    from models import get_session, Meal
    s = get_session()
    try:
        return s.get(Meal, meal_id)
    finally:
        s.close()


def _mid(out):
    assert out.startswith("ok"), out
    return int(out.split("id=")[1].split(" ")[0].rstrip(")"))


def _eaten_local(meal_id, tz_str):
    m = _meal(meal_id)
    return m.eaten_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_str))


def _add_session(user_id, started_at, finished_at=None, status="done"):
    from models import get_session, WorkoutSession
    s = get_session()
    try:
        ws = WorkoutSession(user_id=user_id, template_key="push", status=status,
                            started_at=started_at, finished_at=finished_at, date=started_at)
        s.add(ws)
        s.commit()
        return ws.id
    finally:
        s.close()


def _add_legacy_workout(user_id, at, exercises=None, workout_type="cardio"):
    from models import get_session, Workout
    s = get_session()
    try:
        w = Workout(user_id=user_id, workout_type=workout_type, exercises=exercises or [],
                    completed=True, date=at)
        s.add(w)
        s.commit()
        return w.id
    finally:
        s.close()


def _cal_today(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, user_id).calories_today or 0
    finally:
        s.close()


LA = "America/Los_Angeles"


# ── the live case ──────────────────────────────────────────────────────────────────────

def test_live_rice_krispie_before_my_run_lands_30min_before_the_logged_run(db, monkeypatch):
    """02:32 PT: 'also ate a Rice Krispie before my run earlier'; the run was a text-logged
    cardio Workout row at 01:33 → eaten_at = 01:03 local, same day, description untouched."""
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 2, 32)
    _add_legacy_workout(user.id, _local_at(LA, today, 1, 33), exercises=[{"name": "run", "distance_miles": 2}])
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("rice krispie treat", 90, 1), "eaten_at_hint": "before my run earlier"})
    mid = _mid(out)
    local = _eaten_local(mid, LA)
    assert (local.hour, local.minute) == (1, 3), local
    assert local.date() == today
    assert _meal(mid).description == "rice krispie treat"
    assert "eaten at 01:03 local (from 'before my run earlier')" in out, out
    assert "DAY TOTAL NOW: 90 cal" in out, out          # same local day → today's total, unchanged format
    assert "couldn't read" not in out


def test_live_case_with_a_card_session_anchor(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 2, 32)
    _add_session(user.id, _local_at(LA, today, 1, 33), _local_at(LA, today, 2, 10))
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("rice krispie"), "eaten_at_hint": "before my run"})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == (1, 3)


# ── clock forms ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hint, expect", [
    ("1am", (1, 0)), ("2:30pm", (14, 30)), ("13:00", (13, 0)), ("at 1", (13, 0)),
    ("around 2ish", (14, 0)), ("noon", (12, 0)), ("12:45", (12, 45)), ("1:30", (13, 30)),
    ("3:15pm", (15, 15)),            # 15 min ahead of now: within the 30-min clock tolerance
])
def test_clock_forms_resolve_to_local_today(db, monkeypatch, hint, expect):
    user = make_user(db)
    _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": hint})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == expect, (hint, local)
    assert local.date() == datetime.now(ZoneInfo(LA)).date()


def test_clock_in_the_future_clamps_to_now_not_yesterday(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": "4pm"})
    assert _meal(_mid(out)).eaten_at == now_utc
    assert _eaten_local(_mid(out), LA).date() == today


def test_clock_with_explicit_yesterday_date_lands_on_that_day(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("late lunch"), "date": "yesterday", "eaten_at_hint": "2pm"})
    local = _eaten_local(_mid(out), LA)
    assert local.date() == today - timedelta(days=1)
    assert (local.hour, local.minute) == (14, 0)
    assert f"YESTERDAY ({(today - timedelta(days=1)).isoformat()}) TOTAL NOW: 200 cal" in out, out


# ── relative + named ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hint, minutes_back", [
    ("an hour ago", 60), ("2 hours ago", 120), ("30 min ago", 30), ("half an hour ago", 30),
    ("a couple hours ago", 120), ("an hour and a half ago", 90), ("earlier", 60),
    ("a bit ago", 60), ("just now", 0),
])
def test_relative_forms_subtract_from_now(db, monkeypatch, hint, minutes_back):
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": hint})
    assert _meal(_mid(out)).eaten_at == now_utc - timedelta(minutes=minutes_back), hint


def test_relative_never_crosses_into_yesterday(db, monkeypatch):
    """00:30 'an hour ago' would be 23:30 yesterday — the day only moves on an explicit cue."""
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 0, 30)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": "an hour ago"})
    local = _eaten_local(_mid(out), LA)
    assert local.date() == today and (local.hour, local.minute) == (0, 0)
    assert "DAY TOTAL NOW" in out and "YESTERDAY" not in out


@pytest.mark.parametrize("hint, expect", [
    ("this morning", (9, 0)), ("at breakfast", (9, 0)), ("at lunch", (12, 30)), ("brunch", (11, 0)),
    ("this afternoon", (15, 0)), ("when i woke up", (7, 0)),   # factory wake_time=07:00
])
def test_named_windows(db, monkeypatch, hint, expect):
    user = make_user(db)
    _pin(monkeypatch, LA, 16, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("food"), "eaten_at_hint": hint})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == expect, (hint, local)


def test_this_morning_clamps_to_now_when_it_is_still_early(db, monkeypatch):
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 8, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("oats"), "eaten_at_hint": "this morning"})
    assert _meal(_mid(out)).eaten_at == now_utc


def test_before_bed_and_evening_forms(db, monkeypatch):
    user = make_user(db)
    _pin(monkeypatch, LA, 23, 30)
    from agent_tools import handle_log_meal
    assert _eaten_local(_mid(handle_log_meal(user.id, {**_item("a"), "eaten_at_hint": "before bed"})), LA).hour == 23
    assert _eaten_local(_mid(handle_log_meal(user.id, {**_item("b"), "eaten_at_hint": "at dinner"})), LA).hour == 19


# ── workout anchors ────────────────────────────────────────────────────────────────────

def test_after_my_workout_with_finish_uses_finish_plus_offset(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    _add_session(user.id, _local_at(LA, today, 12, 0), _local_at(LA, today, 13, 0))
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("shake"), "eaten_at_hint": "after my workout"})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == (13, 15), local


def test_after_my_workout_without_finish_uses_start_plus_60(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    _add_session(user.id, _local_at(LA, today, 13, 0), None, status="active")
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("shake"), "eaten_at_hint": "post-workout"})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == (14, 0), local


def test_legacy_cardio_row_with_duration_gives_a_finish(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    _add_legacy_workout(user.id, _local_at(LA, today, 12, 0), exercises=[{"name": "run", "duration_min": 40}])
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("bagel"), "eaten_at_hint": "after the run"})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == (12, 55), local         # 12:40 finish + 15


def test_workout_hint_with_no_workout_falls_back_to_earlier_and_says_so(db, monkeypatch):
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("banana"), "eaten_at_hint": "before the gym"})
    assert _meal(_mid(out)).eaten_at == now_utc - timedelta(minutes=60)
    assert "no logged workout found" in out, out


def test_most_recent_workout_wins_and_yesterdays_is_ignored(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    _add_session(user.id, _local_at(LA, today - timedelta(days=1), 18, 0), _local_at(LA, today - timedelta(days=1), 19, 0))
    _add_session(user.id, _local_at(LA, today, 7, 0), _local_at(LA, today, 8, 0))
    _add_session(user.id, _local_at(LA, today, 12, 0), _local_at(LA, today, 13, 0))
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("bar"), "eaten_at_hint": "pre-lift"})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == (11, 30), local


def test_pre_workout_offset_is_configurable(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "MEAL_PRE_WORKOUT_OFFSET_MIN", 45)
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    _add_session(user.id, _local_at(LA, today, 12, 0), _local_at(LA, today, 13, 0))
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("bar"), "eaten_at_hint": "before training"})
    local = _eaten_local(_mid(out), LA)
    assert (local.hour, local.minute) == (11, 15), local


# ── 'last night' → previous local day + the #157 labeled total ─────────────────────────

def test_last_night_lands_on_yesterday_2100_with_yesterdays_labeled_total(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 9, 0)
    y = today - timedelta(days=1)
    from agent_tools import handle_log_meal
    handle_log_meal(user.id, {**_item("eggs", 300, 20)})                          # today: 300
    out = handle_log_meal(user.id, {**_item("pizza", 800, 30), "eaten_at_hint": "last night"})
    local = _eaten_local(_mid(out), LA)
    assert local.date() == y and (local.hour, local.minute) == (21, 0), local
    assert f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 800 cal, 30g protein — use this exact number for yesterday" in out, out
    assert "DAY TOTAL NOW" not in out, out
    assert f"dated {y.isoformat()}" in out, out
    assert _cal_today(user.id) == 300                       # today's cache untouched


def test_yesterday_at_time_and_late_last_night(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 9, 0)
    y = today - timedelta(days=1)
    from agent_tools import handle_log_meal
    a = _eaten_local(_mid(handle_log_meal(user.id, {**_item("a"), "eaten_at_hint": "yesterday at 3pm"})), LA)
    b = _eaten_local(_mid(handle_log_meal(user.id, {**_item("b"), "eaten_at_hint": "late last night"})), LA)
    assert a.date() == y and (a.hour, a.minute) == (15, 0)
    assert b.date() == y and (b.hour, b.minute) == (23, 0)


# ── unrecognized / absent ──────────────────────────────────────────────────────────────

def test_unrecognized_hint_logs_now_and_asks_for_a_clock_time(db, monkeypatch):
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": "idk sometime"})
    assert _meal(_mid(out)).eaten_at == now_utc
    assert "couldn't read the time 'idk sometime' — logged as now; say a clock time to fix" in out, out
    assert "DAY TOTAL NOW: 200 cal" in out


def test_relative_hint_on_an_explicit_past_day_is_ignored_with_a_note(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "date": "yesterday", "eaten_at_hint": "an hour ago"})
    local = _eaten_local(_mid(out), LA)
    assert local.date() == today - timedelta(days=1) and local.hour == 12   # the noon default stands
    assert "only works for today" in out, out


def test_no_hint_is_unchanged(db, monkeypatch):
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, _item("snack"))
    assert _meal(_mid(out)).eaten_at == now_utc
    assert "eaten at" not in out and "couldn't read" not in out
    assert out.startswith("ok: logged 'snack' id=") and "DAY TOTAL NOW: 200 cal, 5g protein — use this exact number" in out


def test_flag_off_ignores_the_hint(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "MEAL_EATEN_AT_HINT_ENABLED", False)
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": "1pm"})
    assert _meal(_mid(out)).eaten_at == now_utc and "eaten at" not in out


# ── batches: call-level default, per-item override ─────────────────────────────────────

def test_call_level_hint_applies_to_all_items_and_item_hint_overrides(db, monkeypatch):
    user = make_user(db)
    _pin(monkeypatch, LA, 16, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {"items": [_item("eggs"), _item("toast"),
                                              {**_item("apple"), "eaten_at_hint": "at lunch"}],
                                    "eaten_at_hint": "this morning"})
    assert out.startswith("ok: logged 3 items"), out
    import re
    ids = [int(x) for x in re.search(r"ids \[([\d, ]+)\]", out).group(1).split(",")]
    hours = [(_eaten_local(i, LA).hour, _eaten_local(i, LA).minute) for i in ids]
    assert hours == [(9, 0), (9, 0), (12, 30)], hours
    assert "eaten at 09:00 local (from 'this morning')" in out and "eaten at 12:30 local (from 'at lunch')" in out
    assert "DAY TOTAL NOW: 600 cal" in out


def test_item_hint_can_split_a_batch_across_days_and_both_totals_are_labeled(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 9, 0)
    y = today - timedelta(days=1)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {"items": [_item("oats", 300, 10),
                                              {**_item("cookie", 150, 2), "eaten_at_hint": "last night"}]})
    assert "DAY TOTAL NOW: 300 cal" in out and f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 150 cal" in out, out


# ── manage_log edit ────────────────────────────────────────────────────────────────────

def test_edit_eaten_at_hint_retimes_within_the_day_totals_untouched(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    _add_session(user.id, _local_at(LA, today, 12, 0), _local_at(LA, today, 13, 0))
    from agent_tools import handle_log_meal, handle_manage_log
    mid = _mid(handle_log_meal(user.id, _item("rice krispie", 90, 1)))
    assert _meal(mid).eaten_at == now_utc
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "fields": {"eaten_at_hint": "before my run"}})
    assert out.startswith("ok: edited meal"), out
    local = _eaten_local(mid, LA)
    assert local.date() == today and (local.hour, local.minute) == (11, 30), local
    assert "11:30 local" in out, out
    assert "DAY TOTAL NOW: 90 cal" in out and "YESTERDAY" not in out
    assert _cal_today(user.id) == 90
    m = _meal(mid)
    assert m.edits and m.edits[-1]["field"] == "eaten_at_hint" and m.edits[-1]["old"] == now_utc.isoformat()


def test_edit_last_night_moves_row_to_yesterday_and_returns_both_days(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 9, 0)
    y = today - timedelta(days=1)
    from agent_tools import handle_log_meal, handle_manage_log
    mid = _mid(handle_log_meal(user.id, _item("pizza", 800, 30)))
    _mid(handle_log_meal(user.id, _item("eggs", 300, 20)))
    assert _cal_today(user.id) == 1100
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "fields": {"eaten_at_hint": "last night"}})
    assert out.startswith("ok: edited meal"), out
    local = _eaten_local(mid, LA)
    assert local.date() == y and (local.hour, local.minute) == (21, 0), local
    assert f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 800 cal, 30g protein" in out, out
    assert "DAY TOTAL NOW: 300 cal, 20g protein" in out, out
    assert out.index("YESTERDAY") < out.index("DAY TOTAL NOW")
    assert _cal_today(user.id) == 300


def test_edit_date_plus_hint_in_one_call_lands_time_on_the_new_day(db, monkeypatch):
    user = make_user(db)
    now_utc, today = _pin(monkeypatch, LA, 15, 0)
    y = today - timedelta(days=1)
    from agent_tools import handle_log_meal, handle_manage_log
    mid = _mid(handle_log_meal(user.id, _item("wrap", 500, 30)))
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "fields": {"date": "yesterday", "eaten_at_hint": "2:30pm"}})
    assert out.startswith("ok: edited meal"), out
    local = _eaten_local(mid, LA)
    assert local.date() == y and (local.hour, local.minute) == (14, 30), local


def test_edit_unrecognized_hint_errors_and_changes_nothing(db, monkeypatch):
    user = make_user(db)
    now_utc, _ = _pin(monkeypatch, LA, 15, 0)
    from agent_tools import handle_log_meal, handle_manage_log
    mid = _mid(handle_log_meal(user.id, _item("wrap", 500, 30)))
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "fields": {"calories": 600, "eaten_at_hint": "whenever"}})
    assert out.startswith("error:") and "nothing changed" in out, out
    m = _meal(mid)
    assert m.calories == 500 and m.eaten_at == now_utc


# ── tz correctness ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tz_str", ["America/Los_Angeles", "Asia/Tokyo"])
def test_this_morning_is_the_users_own_0900_stored_as_naive_utc(db, monkeypatch, tz_str):
    user = make_user(db, user_timezone=tz_str)
    now_utc, today = _pin(monkeypatch, tz_str, 14, 0)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("oats"), "eaten_at_hint": "this morning"})
    m = _meal(_mid(out))
    assert m.eaten_at.tzinfo is None
    assert m.eaten_at == _local_at(tz_str, today, 9, 0)
    local = m.eaten_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_str))
    assert (local.hour, local.minute) == (9, 0) and local.date() == today
    assert "eaten at 09:00 local" in out


def test_tokyo_last_night_is_tokyos_yesterday(db, monkeypatch):
    tz_str = "Asia/Tokyo"
    user = make_user(db, user_timezone=tz_str)
    now_utc, today = _pin(monkeypatch, tz_str, 8, 0)
    y = today - timedelta(days=1)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("ramen", 600, 25), "eaten_at_hint": "last night"})
    assert _meal(_mid(out)).eaten_at == _local_at(tz_str, y, 21, 0)
    assert f"YESTERDAY ({y.isoformat()}) TOTAL NOW: 600 cal" in out, out


def test_day_reset_hour_keeps_a_small_hours_clock_on_the_running_day(db, monkeypatch):
    """day_reset_hour=4: at 02:30 the nutrition day is still yesterday's calendar date;
    '1am' must land at 01:00 on that running day (not on the previous calendar date)."""
    user = make_user(db, day_reset_hour=4)
    now_utc, today = _pin(monkeypatch, LA, 2, 30)
    from agent_tools import handle_log_meal
    out = handle_log_meal(user.id, {**_item("snack"), "eaten_at_hint": "1am"})
    assert _meal(_mid(out)).eaten_at == _local_at(LA, today, 1, 0)
    assert "DAY TOTAL NOW: 200 cal" in out and "YESTERDAY" not in out


# ── grammar table (pure) ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hint, kind, payload, prev", [
    ("before my run earlier", "workout", "pre", False),
    ("after the gym", "workout", "post", False),
    ("post workout", "workout", "post", False),
    ("Pre-Lift", "workout", "pre", False),
    ("1am", "clock", time(1, 0), False),
    ("2:30 pm", "clock", time(14, 30), False),
    ("12am", "clock", time(0, 0), False),
    ("12pm", "clock", time(12, 0), False),
    ("at 1", "clock", ("bare", 1, 0), False),
    ("an hour ago", "relative", 60, False),
    ("1.5 hours ago", "relative", 90, False),
    ("45 minutes ago", "relative", 45, False),
    ("earlier today", "relative", 60, False),
    ("this morning", "named", time(9, 0), False),
    ("last night", "named", time(21, 0), True),
    ("yesterday", "named", time(12, 0), True),
    ("yesterday morning", "named", time(9, 0), True),
    ("before bed", "named", time(23, 0), False),
    ("after class", None, None, False),
    ("", None, None, False),
])
def test_parse_hint_grammar(hint, kind, payload, prev):
    from meal_time import parse_hint
    got = parse_hint(hint)
    if kind is None:
        assert got is None, got
    else:
        assert got == (kind, payload, prev), got
