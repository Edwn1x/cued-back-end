"""
Phase 3 tool 4 — get_dining_menu. On-demand read of today's scraped hall menu
(replaces always-on context injection).

Menu-first (2026-10-04): "what's at crossroads" is a request for the MENU. The tool
result is a compact GROUPED menu (mains → sides → salad/deli → other → sweets), each
item "name — cal / protein", capped with "+N more", meal period defaulted from the
user's local clock, and a header that tells the model to relay the list first.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _today_pacific():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _seed_menu(hall="crossroads", period="lunch"):
    from models import get_session, DiningMenuItem
    s = get_session()
    try:
        today = _today_pacific()
        s.add(DiningMenuItem(scraped_date=today, hall=hall, meal_period=period,
                             item_name="grilled chicken breast", calories=200, protein_g=38.0))
        s.add(DiningMenuItem(scraped_date=today, hall=hall, meal_period=period,
                             item_name="brown rice", calories=220, protein_g=5.0))
        s.commit()
    finally:
        s.close()


def _seed_rows(rows, hall="crossroads", period="dinner", date=None):
    """rows: (station, name, cal, protein) — stations deliberately out of 'mains first'
    order (alphabetical: Desserts < Entrees < Salad Bar < Sides) so grouping is tested."""
    from models import get_session, DiningMenuItem
    s = get_session()
    try:
        today = date or _today_pacific()
        for station, name, cal, pro in rows:
            s.add(DiningMenuItem(scraped_date=today, hall=hall, meal_period=period,
                                 station=station, item_name=name, calories=cal, protein_g=pro))
        s.commit()
    finally:
        s.close()


_CROSSROADS_DINNER = [
    ("Desserts", "chocolate chip cookie", 210, 2.0),
    ("Salad Bar", "mixed greens", 15, 1.0),
    ("Sides", "jamaican rice and peas", 240, 6.0),
    ("Sides", "steamed broccoli", 35, 3.0),
    ("Entrees", "halal chicken breast", 161, 29.0),
    ("Entrees", "halal jerk chicken thigh", 155, 22.0),
    ("Entrees", "jerk tofu", 223, 19.0),
    ("Pizza", "sausage and onion pizza", 317, 14.0),
    ("Street Food", "beef swedish meatballs", 430, 16.0),
]


def _item_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith("- ")]


# ---------------------------------------------------------------- legacy shape


def test_get_dining_menu_returns_todays_items(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    _seed_menu()
    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "crossroads", "meal_period": "lunch"})
    assert out.startswith("ok"), out
    assert "grilled chicken breast" in out and "38g protein" in out


def test_get_dining_menu_empty_is_error(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "foothill"})
    assert out.startswith("error")


# ---------------------------------------------------------------- grouped shape


def test_menu_is_grouped_mains_first_with_cal_and_protein(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    _seed_rows(_CROSSROADS_DINNER)
    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "crossroads", "meal_period": "dinner"})
    assert out.startswith("ok: crossroads dinner menu today"), out
    # header: hall + meal period note, and the relay instruction (affordance-in-result)
    head = out.splitlines()[0]
    assert "note: hall=crossroads, meal=dinner" in head
    assert "relay this list first" in head and "pick" in head

    # group order: mains (Entrees + Pizza) → sides → salad → other (Street Food) → sweets
    i_mains = out.index("MAINS")
    i_sides = out.index("SIDES")
    i_salad = out.index("SALAD/DELI")
    i_other = out.index("OTHER")
    i_sweet = out.index("SWEETS/DRINKS")
    assert i_mains < i_sides < i_salad < i_other < i_sweet, out
    # the first item line is a main, even though the DB order is alphabetical by station
    lines = _item_lines(out)
    assert lines[0].startswith("- halal chicken breast") or lines[0].startswith("- halal jerk") \
        or lines[0].startswith("- jerk tofu"), lines[0]
    # each item carries cal / protein in the compact form
    assert "- halal chicken breast — 161 cal / 29g protein" in out
    assert "- jamaican rice and peas — 240 cal / 6g protein" in out
    assert "- beef swedish meatballs — 430 cal / 16g protein" in out
    # the dessert is listed last
    assert lines[-1].startswith("- chocolate chip cookie")
    assert "+" not in out.splitlines()[-1]  # under the cap: no "+N more" tail
    assert f"{len(_CROSSROADS_DINNER)} items" in head


def test_menu_caps_with_n_more_and_keeps_mains(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu, _MENU_ITEM_CAP

    rows = [("Entrees", f"main dish {i:02d}", 300 + i, 20.0) for i in range(6)]
    rows += [("Sides", f"side dish {i:02d}", 100 + i, 3.0) for i in range(_MENU_ITEM_CAP)]
    rows += [("Desserts", f"sweet {i:02d}", 200 + i, 2.0) for i in range(4)]
    _seed_rows(rows)
    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "crossroads", "meal_period": "dinner"})
    lines = _item_lines(out)
    assert len(lines) == _MENU_ITEM_CAP, len(lines)
    omitted = len(rows) - _MENU_ITEM_CAP
    assert out.splitlines()[-1] == f"+{omitted} more — want the rest?", out.splitlines()[-1]
    # every main survived the cap; what got dropped is tail-of-list sides + sweets
    assert sum(1 for ln in lines if ln.startswith("- main dish")) == 6
    assert not any(ln.startswith("- sweet") for ln in lines)
    assert f"{len(rows)} items" in out.splitlines()[0]


def test_menu_dedupes_all_day_rows_and_unknown_macros(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    _seed_rows([("Entrees", "halal chicken breast", 161, 29.0)], period="dinner")
    _seed_rows([("Entrees", "halal chicken breast", 161, 29.0),
                (None, "mystery stew", None, None)], period="all_day")
    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "crossroads", "meal_period": "dinner"})
    assert out.count("halal chicken breast") == 1
    assert "- mystery stew — ? cal / ? protein" in out
    assert "OTHER:" in out  # no station → other tier, after the mains


# ---------------------------------------------------------------- default meal period


def test_default_meal_period_by_local_clock():
    from agent_tools import _default_meal_period
    tz = ZoneInfo("America/Los_Angeles")
    mon = datetime(2026, 10, 5, tzinfo=tz)  # a Monday
    sat = datetime(2026, 10, 3, tzinfo=tz)  # a Saturday
    assert _default_meal_period(mon.replace(hour=8)) == "breakfast"
    assert _default_meal_period(mon.replace(hour=12)) == "lunch"
    assert _default_meal_period(mon.replace(hour=15, minute=0)) == "lunch"
    assert _default_meal_period(mon.replace(hour=16, minute=0)) == "dinner"
    assert _default_meal_period(mon.replace(hour=23)) == "dinner"
    assert _default_meal_period(sat.replace(hour=9)) == "brunch"
    assert _default_meal_period(sat.replace(hour=13)) == "brunch"
    assert _default_meal_period(sat.replace(hour=19)) == "dinner"


def _freeze_local(monkeypatch, when_local: datetime):
    """Pin agent_tools' clock so `datetime.now(tz)` is `when_local` in any tz."""
    import agent_tools as at
    real = at.datetime

    class _Fixed(real):
        @classmethod
        def now(cls, tz=None):
            return when_local.astimezone(tz) if tz else when_local.replace(tzinfo=None)

    monkeypatch.setattr(at, "datetime", _Fixed)


def test_omitted_period_defaults_from_user_local_time(db, monkeypatch):
    """15:00 PT on 2026-10-04 (the incident's clock, a Sunday → brunch is the daytime
    period; dinner rows are what the hall has) — no meal_period on the call."""
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 10, 5, 12, 0, tzinfo=tz)  # Monday noon → lunch
    _freeze_local(monkeypatch, now)
    date = now.strftime("%Y-%m-%d")
    _seed_rows([("Entrees", "lunch chicken", 200, 30.0)], period="lunch", date=date)
    _seed_rows([("Entrees", "dinner meatballs", 430, 16.0)], period="dinner", date=date)
    user = make_user(db, user_timezone="America/Los_Angeles")
    out = handle_get_dining_menu(user.id, {"hall": "crossroads"})
    assert out.startswith("ok: crossroads lunch menu"), out
    assert "defaulted from the time of day" in out.splitlines()[0]
    assert "lunch chicken" in out and "dinner meatballs" not in out

    # 19:00 local → dinner
    _freeze_local(monkeypatch, now.replace(hour=19))
    out = handle_get_dining_menu(user.id, {"hall": "crossroads"})
    assert out.startswith("ok: crossroads dinner menu"), out
    assert "dinner meatballs" in out and "lunch chicken" not in out


def test_omitted_period_falls_through_to_the_period_that_exists(db, monkeypatch):
    """Monday 08:00 → breakfast by the clock, but the hall only has a dinner menu
    scraped: a defaulted call still returns a list (dinner), an explicit one errors."""
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    tz = ZoneInfo("America/Los_Angeles")
    now = datetime(2026, 10, 5, 8, 0, tzinfo=tz)
    _freeze_local(monkeypatch, now)
    date = now.strftime("%Y-%m-%d")
    _seed_rows([("Entrees", "dinner meatballs", 430, 16.0)], period="dinner", date=date)
    user = make_user(db, user_timezone="America/Los_Angeles")
    out = handle_get_dining_menu(user.id, {"hall": "crossroads"})
    assert out.startswith("ok: crossroads dinner menu"), out
    out = handle_get_dining_menu(user.id, {"hall": "crossroads", "meal_period": "breakfast"})
    assert out.startswith("error: no breakfast menu for crossroads"), out
    assert "periods with a menu: dinner" in out


# ---------------------------------------------------------------- through the loop


def _menu_first_reply(tool_result: str) -> str:
    """What the coach should say: the (grouped) menu, then ONE pick line."""
    items = [ln[2:] for ln in tool_result.splitlines() if ln.startswith("- ")]
    return ("mains at crossroads rn:\n" + "\n".join(f"- {it}" for it in items[:4])
            + "\nsides: jamaican rice and peas 240/6, broccoli 35/3\n"
              "the halal breast + jerk thigh is the protein play — ur move")


def test_whats_at_hall_yields_menu_first(db, driver, monkeypatch, anthropic_stub):
    """'what's at crossroads' → the model is offered get_dining_menu, the tool result it
    receives is the grouped menu (mains first, with the relay-list-first note), and the
    reply lists ≥3 menu items BEFORE any recommendation line."""
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "GET_DINING_MENU_TOOL_ENABLED", True)
    _seed_rows(_CROSSROADS_DINNER)

    seen = {"offered": False, "tool_result": None}

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        names = {t.get("name") for t in kw["tools"]}
        if "get_dining_menu" in names:
            seen["offered"] = True
        # the turn after our tool_use carries the tool_result back to the model
        for msg in kw.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        seen["tool_result"] = block.get("content")
        if seen["tool_result"] is None:
            return ToolUse("get_dining_menu", {"hall": "crossroads", "meal_period": "dinner"})
        return _menu_first_reply(seen["tool_result"])

    anthropic_stub.reply_with(handler)
    user = make_user(db, which_gym="rsf")
    replies = driver.send(user, "what's at crossroads")

    assert seen["offered"], "get_dining_menu was not offered to the model"
    tr = seen["tool_result"]
    assert tr and tr.startswith("ok: crossroads dinner menu"), tr
    assert tr.index("MAINS") < tr.index("SIDES"), tr
    assert "relay this list first" in tr.splitlines()[0]

    reply = "\n".join(replies)
    menu_idx = [reply.index(n) for n in ("halal chicken breast", "halal jerk chicken thigh", "jerk tofu")]
    pick_idx = reply.index("ur move")
    assert len(menu_idx) >= 3 and max(menu_idx) < pick_idx, reply
    assert reply.splitlines()[0].startswith("mains at crossroads"), reply


def test_what_should_i_get_still_allows_pick_first(db, driver, monkeypatch, anthropic_stub):
    """Regression guard for the other intent: 'what should i get' → pick first is fine."""
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "GET_DINING_MENU_TOOL_ENABLED", True)
    _seed_rows(_CROSSROADS_DINNER)

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("get_dining_menu", {"hall": "crossroads", "meal_period": "dinner"})
        return ("halal chicken breast + jerk thigh is ur move — ~51g protein for 316 cal. "
                "add the rice and peas and ur at a real meal")

    anthropic_stub.reply_with(handler)
    user = make_user(db, which_gym="rsf")
    replies = driver.send(user, "what should i get at crossroads")
    assert len(replies) >= 1
    assert "ur move" in "\n".join(replies)


def test_get_dining_menu_via_loop(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "GET_DINING_MENU_TOOL_ENABLED", True)
    _seed_menu()

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("get_dining_menu", {"hall": "crossroads", "meal_period": "lunch"})
        return "grab the grilled chicken — 200cal/38g, best protein on the line"

    anthropic_stub.reply_with(handler)
    user = make_user(db, which_gym="rsf")
    replies = driver.send(user, "what's good at crossroads for lunch")
    assert len(replies) >= 1
