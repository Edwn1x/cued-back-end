"""
Sources on file + routines (sources.py, agent_tasks kind "routine", agent_tools.save_source).

The general shape behind "grab Alexey's house menu each week" (2026-11-02): the coach
remembers WHERE a thing is posted; a routine fetches it cheaply on schedule, and only when
the page changed runs a model turn that may SHOW (send_image) and STAGE (save_menu behind
the confirm gate) and asks in one line; the user's yes in a normal turn does the write.

Pinned here (tier 1, model mocked, HTTP mocked):
  * the store: dedupe by url, url policy, caps, the context block, recheck→routine wiring
  * the clock: weekly = Mondays at the local time, daily = every day; first look math
  * the run: unchanged fingerprint → no model call + same-cycle retry, then next cycle;
    changed → model turn with the propose tools and the background note; report → text
    sent + next cycle; nothing → fingerprint remembered + retry; already saved this week →
    skip; a background run can stage but never confirm
  * the WAITING ON THEIR YES block makes the next-turn yes actionable
"""
from __future__ import annotations

import io
import socket
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
import webfetch
from tests.factories import make_user
from tests._fake_anthropic import ToolUse
from tests.tier1.test_fetch_page import _Resp, _patch_http

LA = ZoneInfo("America/Los_Angeles")
MENU_PAGE = "https://grizzlychefs.com/weekly-menu"
MENU_IMG = "https://cdn.example/content/Fall+11%3A2%3A2026+Menu+.png"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _png(w=200, h=100) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _page(img_url=MENU_IMG, text="ORDER NOW"):
    return f"<html><head><title>Menu 1 — GRIZZLY CHEFS</title></head><body><main><img data-src='{img_url}'><p>{text}</p></main></body></html>"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for f in ("TASKS_ENABLED", "ROUTINES_ENABLED", "SOURCES_ENABLED", "FETCH_PAGE_TOOL_ENABLED",
              "FETCH_PAGE_MEDIA_ENABLED", "SEND_IMAGE_TOOL_ENABLED", "SAVE_MENU_TOOL_ENABLED",
              "SAVE_MENU_CONFIRM_WEB_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])

    def fake_gai(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(webfetch.socket, "getaddrinfo", fake_gai)
    webfetch._CACHE.clear()
    yield
    webfetch._CACHE.clear()


def _task(db, task_id):
    from models import AgentTask
    db.expire_all()
    return db.get(AgentTask, task_id)


def _user(db, **kw):
    return make_user(db, name="Alexey", user_timezone="America/Los_Angeles", **kw)


def _sources(db, uid):
    from models import User
    db.expire_all()
    return list(db.get(User, uid).sources or [])


# ─── the store ──────────────────────────────────────────────────────────────

def test_save_source_dedupes_by_url_validates_and_renders(db, monkeypatch):
    import agent_tools
    monkeypatch.setattr(config, "ROUTINES_ENABLED", False)
    u = _user(db)
    out = agent_tools.dispatch_tool("save_source", {"name": "Grizzly Chefs weekly menu", "url": MENU_PAGE,
                                                    "purpose": "house menu"}, u.id)
    assert out.startswith("ok: on file — Grizzly Chefs weekly menu") and "no re-checks" in out
    out = agent_tools.dispatch_tool("save_source", {"name": "grizzly menu", "url": MENU_PAGE + "/#top",
                                                    "purpose": "house menu (dinners)"}, u.id)
    assert out.startswith("ok:")
    rows = _sources(db, u.id)
    assert len(rows) == 1 and rows[0]["name"] == "grizzly menu" and rows[0]["purpose"] == "house menu (dinners)"
    assert rows[0]["id"] == 1 and rows[0]["recheck"] == "none"
    # policy: a private host / junk url is not a source
    bad = agent_tools.dispatch_tool("save_source", {"name": "x", "url": "http://127.0.0.1/menu"}, u.id)
    assert bad.startswith("error:") and "can't be a source" in bad
    assert agent_tools.dispatch_tool("save_source", {"name": "", "url": MENU_PAGE}, u.id).startswith("error:")
    from sources import render_sources_block
    blk = render_sources_block(_sources(db, u.id))
    assert "SOURCES ON FILE" in blk and "grizzly menu — house menu (dinners): https://grizzlychefs.com/weekly-menu" in blk
    # context carries it
    from agent_loop import build_loop_context
    from models import get_session, User
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, u.id), s)
    finally:
        s.close()
    assert "SOURCES ON FILE" in ctx


def test_sources_cap_keeps_the_newest(db, monkeypatch):
    from sources import apply_source
    monkeypatch.setattr(config, "SOURCES_MAX_PER_USER", 3)
    items = None
    for i in range(5):
        items, _ = apply_source(items, name=f"s{i}", url=f"https://x{i}.example/p")
    assert [r["name"] for r in items] == ["s2", "s3", "s4"]


def test_flag_off_hides_the_tool_and_the_handler(db, monkeypatch):
    import agent_tools, agent_loop
    monkeypatch.setattr(config, "SOURCES_ENABLED", False)
    u = _user(db)
    assert agent_tools.dispatch_tool("save_source", {"name": "a", "url": MENU_PAGE}, u.id).startswith("error:")
    src = open(agent_loop.__file__).read()
    assert "if config.SOURCES_ENABLED:" in src and "SAVE_SOURCE_TOOL" in src


# ─── the clock ──────────────────────────────────────────────────────────────

def test_weekly_recheck_creates_a_monday_routine_and_none_cancels_it(db):
    import agent_tools
    from models import AgentTask
    u = _user(db)
    out = agent_tools.dispatch_tool("save_source", {"name": "Grizzly Chefs weekly menu", "url": MENU_PAGE,
                                                    "purpose": "house menu", "recheck": "weekly"}, u.id)
    assert "code will check it weekly" in out and "first look" in out and "ask before saving" in out
    row = _sources(db, u.id)[0]
    assert row["recheck"] == "weekly" and row["task_id"]
    t = _task(db, row["task_id"])
    assert t.kind == "routine" and t.source_url == MENU_PAGE and t.weekday == 0 and t.at_local == "09:30"
    local = t.run_at.replace(tzinfo=timezone.utc).astimezone(LA)
    assert local.weekday() == 0 and (local.hour, local.minute) == (9, 30) and t.run_at > _utcnow()
    assert t.cycle_at == t.run_at and t.status == "pending"
    # the TASKS block names it
    from agent_tasks import context_block
    from models import get_session, User
    s = get_session()
    try:
        blk = context_block(s.get(User, u.id), s)
    finally:
        s.close()
    assert "routine, every monday ~09:30" in blk and MENU_PAGE in blk
    # re-saving replaces the routine (one per source), 'none' cancels it
    agent_tools.dispatch_tool("save_source", {"name": "grizzly", "url": MENU_PAGE, "recheck": "daily"}, u.id)
    rows = db.query(AgentTask).filter(AgentTask.user_id == u.id, AgentTask.kind == "routine").order_by(AgentTask.id).all()
    db.expire_all()
    assert [r.status for r in rows] == ["cancelled", "pending"] and rows[1].weekday is None
    agent_tools.dispatch_tool("save_source", {"name": "grizzly", "url": MENU_PAGE, "recheck": "none"}, u.id)
    db.expire_all()
    assert all(r.status == "cancelled" for r in db.query(AgentTask).filter(AgentTask.user_id == u.id).all())
    assert _sources(db, u.id)[0]["task_id"] is None


def test_next_cycle_math():
    from agent_tasks import next_cycle_at
    # Tue 2026-11-03 08:00 LA → weekly Monday 09:30 = Mon 2026-11-09; daily = Tue 09:30 today
    now = datetime(2026, 11, 3, 8, 0, tzinfo=LA).astimezone(timezone.utc).replace(tzinfo=None)
    wk = next_cycle_at(now, LA, at_local="09:30", weekday=0).replace(tzinfo=timezone.utc).astimezone(LA)
    assert (wk.month, wk.day, wk.hour, wk.minute) == (11, 9, 9, 30)
    dy = next_cycle_at(now, LA, at_local="09:30", weekday=None).replace(tzinfo=timezone.utc).astimezone(LA)
    assert (dy.month, dy.day, dy.hour) == (11, 3, 9)
    # Mon 09:00 → today 09:30 (still ahead); Mon 10:00 → next Monday
    mon9 = datetime(2026, 11, 2, 9, 0, tzinfo=LA).astimezone(timezone.utc).replace(tzinfo=None)
    assert next_cycle_at(mon9, LA, at_local="09:30", weekday=0).replace(tzinfo=timezone.utc).astimezone(LA).day == 2
    mon10 = datetime(2026, 11, 2, 10, 0, tzinfo=LA).astimezone(timezone.utc).replace(tzinfo=None)
    assert next_cycle_at(mon10, LA, at_local="09:30", weekday=0).replace(tzinfo=timezone.utc).astimezone(LA).day == 9
    # a bad at_local falls back to 09:30
    assert next_cycle_at(mon9, LA, at_local="nope", weekday=None).replace(tzinfo=timezone.utc).astimezone(LA).minute == 30


def test_schedule_task_cannot_make_a_routine(db):
    from agent_tasks import create_task
    u = _user(db)
    assert "error" in create_task(u.id, "check the menu page weekly", delay_minutes=5, kind="routine")


# ─── the run ────────────────────────────────────────────────────────────────

def _routine(db, user, *, fp=None, due=True):
    from models import AgentTask
    now = _utcnow()
    row = AgentTask(user_id=user.id, goal="check Grizzly Chefs weekly menu (house menu) for anything new",
                    kind="routine", run_at=now - timedelta(minutes=1) if due else now + timedelta(hours=1),
                    every_minutes=7 * 24 * 60, status="pending", source="source", runs=0,
                    source_url=MENU_PAGE, at_local="09:30", weekday=0,
                    cycle_at=now - timedelta(minutes=1), watch_last=fp)
    db.add(row)
    db.commit()
    return row.id


def test_unchanged_source_makes_no_model_call_and_retries_inside_the_cycle_then_next_cycle(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due, source_fingerprint
    _patch_http(monkeypatch, {MENU_PAGE: _Resp(_page())})
    fp, err = source_fingerprint(MENU_PAGE)
    assert fp and not err
    u = _user(db)
    tid = _routine(db, u, fp=fp)
    before = _utcnow()
    assert fire_due() == 0
    assert not anthropic_stub.calls and not sms_capture
    t = _task(db, tid)
    assert t.status == "pending" and timedelta(hours=5.9) <= (t.run_at - before) <= timedelta(hours=6.1)
    assert t.cycle_at < before                       # same cycle
    # past the cycle window → the next Monday
    t.cycle_at = before - timedelta(hours=31)
    t.run_at = before - timedelta(minutes=1)
    db.commit()
    assert fire_due() == 0
    t = _task(db, tid)
    local = t.run_at.replace(tzinfo=timezone.utc).astimezone(LA)
    assert local.weekday() == 0 and t.run_at > before + timedelta(hours=12) and t.cycle_at == t.run_at


def test_unreachable_source_retries_quietly(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    _patch_http(monkeypatch, default=_Resp(b"", status=500))
    u = _user(db)
    tid = _routine(db, u)
    assert fire_due() == 0 and not anthropic_stub.calls and not sms_capture
    assert _task(db, tid).status == "pending" and _task(db, tid).run_at > _utcnow()


def test_changed_source_runs_the_model_with_propose_tools_and_the_ask_goes_out(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    from agent_tools import _pending_list
    import sms
    png = _png()
    _patch_http(monkeypatch, {MENU_PAGE: _Resp(_page()), MENU_IMG: _Resp(png, ctype="image/png")})
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s")
    monkeypatch.setattr(sms, "outage_active", lambda: False)
    attachments = []
    monkeypatch.setattr(sms, "_send_imessage_attachment", lambda phone, data, name, mime: attachments.append((name, mime)) or "att-1")
    u = _user(db, preferred_channel="imessage")
    tid = _routine(db, u, fp="old-fingerprint")
    anthropic_stub.push(
        ToolUse("fetch_page", {"url": MENU_PAGE}, id="t1"),
        ToolUse("fetch_page", {"url": MENU_IMG}, id="t2"),
        ToolUse("send_image", {"url": MENU_IMG, "caption": "grizzly chefs menu 11/2"}, id="t3"),
        ToolUse("save_menu", {"name": "house menu", "source_url": MENU_IMG, "week_of": "2026-11-02",
                              "items": [{"item": "Mon dinner: Bruschetta Chicken"}, {"item": "Tue dinner: Taco Bar"}],
                              "confirmed": True}, id="t4"),          # a background run's confirmed=true is inert
        ToolUse("report", {"message": "this week's house menu — that the right one? 👍 and i'll log dinners off it"}, id="t5"),
    )
    before = _utcnow()
    assert fire_due() == 1
    # the run saw the routine prompt + the propose tools
    first = anthropic_stub.calls[0]
    names = {t.get("name") for t in first["tools"]}
    assert {"fetch_page", "send_image", "save_menu", "report", "nothing_to_report"} <= names
    assert not ({"log_meal", "remember", "set_reminder", "schedule_task", "save_source"} & names)
    assert "ROUTINE CHECK" in str(first["system"]) and MENU_PAGE in str(first["system"])
    # the picture was looked at (image block fed back) and sent
    fed = [c for m in anthropic_stub.calls[-1]["messages"] if isinstance(m.get("content"), list)
           for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]
    assert any(isinstance(c.get("content"), list) and c["content"][1]["type"] == "image" for c in fed)
    assert attachments == [("Fall+11:2:2026+Menu+.png", "image/png")]
    # the menu is STAGED, not saved — confirmed=true from a background run was ignored
    from models import User
    db.expire_all()
    assert not (db.get(User, u.id).saved_menus or {})
    staged = _pending_list(u.id)
    assert len(staged) == 1 and staged[0]["kind"] == "save_menu" and staged[0]["n"] == 2
    # the ask went out as the routine's one line, typed "task"
    assert sms_capture and "that the right one" in sms_capture[-1][1]
    from models import Message
    out = db.query(Message).filter(Message.user_id == u.id, Message.direction == "out").order_by(Message.id).all()
    assert any(m.body.startswith("[sent picture:") and m.message_type == "freeform" for m in out)
    assert out[-1].message_type == "task" and "that the right one" in out[-1].body
    # the row remembered the fingerprint and re-armed for the NEXT Monday
    t = _task(db, tid)
    assert t.status == "pending" and t.watch_last and t.watch_last != "old-fingerprint"
    assert t.run_at.replace(tzinfo=timezone.utc).astimezone(LA).weekday() == 0 and t.run_at > before + timedelta(days=1)
    # …and their yes next turn is actionable: the WAITING block names the exact confirming call
    from agent_tools import render_pending_block
    blk = render_pending_block(u.id)
    assert "WAITING ON THEIR YES" in blk and f"save_menu(name='house menu', source_url={MENU_IMG!r}, confirmed=true)" in blk
    # the yes (a normal turn, a different message id) writes it from the staged payload
    from agent_tools import handle_save_menu
    ok = handle_save_menu(u.id, {"name": "house menu", "source_url": MENU_IMG, "confirmed": True}, message_id="m-yes")
    assert ok.startswith("ok: saved 'house menu' (week of Nov 2) with 2 items")
    assert render_pending_block(u.id) == ""


def test_nothing_new_remembers_the_fingerprint_and_retries(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due, source_fingerprint
    _patch_http(monkeypatch, {MENU_PAGE: _Resp(_page(text="last week's menu still up"))})
    fp, _ = source_fingerprint(MENU_PAGE)
    u = _user(db)
    tid = _routine(db, u, fp="old")
    anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "this week's not posted yet"}, id="n1"))
    before = _utcnow()
    assert fire_due() == 0 and not sms_capture
    t = _task(db, tid)
    assert t.watch_last == fp and t.status == "pending"
    assert timedelta(hours=5.9) <= (t.run_at - before) <= timedelta(hours=6.1)
    # the next sweep on the same content is free (no model call)
    t.run_at = before - timedelta(minutes=1)
    db.commit()
    n = len(anthropic_stub.calls)
    assert fire_due() == 0 and len(anthropic_stub.calls) == n


def test_already_saved_this_week_skips_the_run(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    from saved_menus import save_menu_for_user
    _patch_http(monkeypatch, {MENU_PAGE: _Resp(_page())})
    u = _user(db)
    today = datetime.now(LA).date()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    save_menu_for_user(u.id, "house menu", [{"item": "Mon dinner: x"}], source_url=MENU_PAGE, week_of=monday)
    tid = _routine(db, u, fp="old")
    assert fire_due() == 0 and not anthropic_stub.calls and not sms_capture
    t = _task(db, tid)
    assert t.watch_last and t.run_at.replace(tzinfo=timezone.utc).astimezone(LA).weekday() == 0


def test_model_failure_keeps_the_content_for_a_retry_and_never_texts(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    _patch_http(monkeypatch, {MENU_PAGE: _Resp(_page())})
    u = _user(db)
    tid = _routine(db, u, fp="old")
    anthropic_stub.push(ToolUse("report", {"message": ""}, id="r0"))     # empty report = failed run
    assert fire_due() == 0 and not sms_capture
    t = _task(db, tid)
    assert t.watch_last == "old" and t.status == "pending" and t.run_at > _utcnow()


def test_routines_flag_off_parks_the_row(db, anthropic_stub, sms_capture, monkeypatch):
    from agent_tasks import fire_due
    monkeypatch.setattr(config, "ROUTINES_ENABLED", False)
    u = _user(db)
    tid = _routine(db, u)
    assert fire_due() == 0 and not anthropic_stub.calls
    t = _task(db, tid)
    assert t.status == "pending" and t.run_at > _utcnow() + timedelta(hours=23)


def test_gone_user_cancels_the_routine_like_any_task(db, anthropic_stub, sms_capture):
    from agent_tasks import fire_due
    u = _user(db, opted_out=True)
    tid = _routine(db, u)
    fire_due()
    assert _task(db, tid).status == "cancelled"


def test_prompts_carry_the_source_rule():
    voice = open("prompts/voice.md").read()
    assert "Where their stuff is posted" in voice and "save_source" in voice and "SOURCES ON FILE" in voice
    import agent_tasks
    assert "you cannot confirm it yourself" in agent_tasks._ROUTINE_PROMPT


# ─── review fixes (2026-11-02) ───────────────────────────────────────────────

def test_a_routines_staged_menu_survives_an_afternoon_yes(db):
    """The ask goes out at 09:30 unprompted; the 👍 comes at 13:00. The calendar's 3h stale
    window would have thrown the staged menu away — menus wait up to 36h."""
    from agent_tools import handle_save_menu, _pending_list, _save_pending, render_pending_block
    u = _user(db)
    handle_save_menu(u.id, {"name": "house menu", "source_url": MENU_IMG, "week_of": "2026-11-02",
                            "items": [{"item": "Mon dinner: x"}]}, message_id=None)
    items = _pending_list(u.id)
    items[0]["staged_at"] = (_utcnow() - timedelta(hours=5)).isoformat()
    _save_pending(u.id, items)
    assert "WAITING ON THEIR YES" in render_pending_block(u.id)
    ok = handle_save_menu(u.id, {"name": "house menu", "source_url": MENU_IMG, "confirmed": True}, message_id="m-yes")
    assert ok.startswith("ok: saved")
    # but a calendar-style proposal keeps the 3h rule
    from agent_tools import _pending_stale_for
    assert _pending_stale_for({"kind": "create"}) == timedelta(hours=3)
    assert _pending_stale_for({"kind": "save_menu"}) == timedelta(hours=36)


def test_confirm_writes_the_staged_payload_even_when_items_are_repassed_short(db):
    from agent_tools import handle_save_menu
    from models import User
    u = _user(db)
    handle_save_menu(u.id, {"name": "house menu", "source_url": MENU_IMG, "week_of": "2026-11-02",
                            "items": [{"item": "Mon dinner: a"}, {"item": "Tue dinner: b"}, {"item": "Wed dinner: c"}]},
                     message_id="m1")
    ok = handle_save_menu(u.id, {"name": "house menu", "source_url": MENU_IMG, "confirmed": True,
                                 "items": [{"item": "Mon dinner: a"}]}, message_id="m2")   # re-typed, shortened, no week_of
    assert ok.startswith("ok: saved 'house menu' (week of Nov 2) with 3 items")
    db.expire_all()
    saved = db.get(User, u.id).saved_menus["house menu"]
    assert len(saved["items"]) == 3 and saved["week_of"] == "2026-11-02"


def test_live_turn_defers_the_routine_and_a_stale_state_does_not(db, anthropic_stub, sms_capture, monkeypatch):
    import agent_tools
    from agent_tasks import fire_due
    _patch_http(monkeypatch, {MENU_PAGE: _Resp(_page())})
    u = _user(db)
    tid = _routine(db, u, fp="old")
    agent_tools.begin_turn(u.id)                       # their reply is mid-loop
    try:
        before = _utcnow()
        assert fire_due() == 0 and not anthropic_stub.calls
        t = _task(db, tid)
        assert timedelta(minutes=9) <= (t.run_at - before) <= timedelta(minutes=11)
        # a leaked state from long ago is not a live turn
        agent_tools._TURN_STATE[u.id]["started_at"] -= 3600
        t.run_at = before - timedelta(minutes=1)
        db.commit()
        anthropic_stub.push(ToolUse("nothing_to_report", {"reason": "same menu"}, id="n1"))
        fire_due()
        assert anthropic_stub.calls
    finally:
        agent_tools.pop_turn_state(u.id)


def test_evicted_source_loses_its_routine(db, monkeypatch):
    import agent_tools
    from models import AgentTask
    monkeypatch.setattr(config, "SOURCES_MAX_PER_USER", 2)
    u = _user(db)
    agent_tools.dispatch_tool("save_source", {"name": "a", "url": "https://a.example/menu", "recheck": "weekly"}, u.id)
    first_task = _sources(db, u.id)[0]["task_id"]
    agent_tools.dispatch_tool("save_source", {"name": "b", "url": "https://b.example/menu"}, u.id)
    agent_tools.dispatch_tool("save_source", {"name": "c", "url": "https://c.example/menu"}, u.id)
    assert [r["name"] for r in _sources(db, u.id)] == ["b", "c"]
    db.expire_all()
    assert db.get(AgentTask, first_task).status == "cancelled"
