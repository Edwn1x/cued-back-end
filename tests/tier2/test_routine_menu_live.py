"""
Tier-2 (live) — a ROUTINE end to end with the real model and the real page.

The Alexey shape (2026-11-02): his house's food is Grizzly Chefs; the weekly menu is a PNG
on grizzlychefs.com/weekly-menu; he gets breakfast + dinner (the public page shows lunch +
dinner). A due weekly routine on that source should: open the page, look at the current
week's menu picture, send it (sidecar stubbed here), STAGE the menu (save_menu with
source_url — held by the confirm gate, never saved by the background run) and text ONE
line asking. Then his 👍 in a normal turn writes it from the staged payload.

Run: pytest --run-tier2 -s tests/tier2/test_routine_menu_live.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.tier2

MENU_PAGE = "https://grizzlychefs.com/weekly-menu"


def _flags(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "TASKS_ENABLED", "ROUTINES_ENABLED", "SOURCES_ENABLED",
              "FETCH_PAGE_TOOL_ENABLED", "FETCH_PAGE_MEDIA_ENABLED", "SEND_IMAGE_TOOL_ENABLED",
              "SAVE_MENU_TOOL_ENABLED", "SAVE_MENU_CONFIRM_WEB_ENABLED", "IMESSAGE_CHANNEL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "SIDECAR_URL", "http://sidecar.test:8080")
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s")


def test_weekly_menu_routine_shows_stages_asks_and_their_yes_saves(db, monkeypatch, sms_capture):
    import sms, webfetch
    from tests.factories import make_user
    from agent_tasks import fire_due
    from agent_tools import _pending_list, render_pending_block
    from models import AgentTask, User, Message
    from sources import save_source_for_user
    webfetch._CACHE.clear()
    _flags(monkeypatch)
    monkeypatch.setattr(sms, "outage_active", lambda: False)
    attachments = []
    monkeypatch.setattr(sms, "_send_imessage_attachment",
                        lambda phone, data, name, mime: attachments.append((name, mime, len(data))) or "att-live-1")
    texts = []
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body, reply_to=None: texts.append(body) or f"im-{len(texts)}")

    user = make_user(db, name="Alexey", occupation="student", user_timezone="America/Los_Angeles",
                     preferred_channel="imessage",
                     food_context=("Lives in a fraternity house: gets a LIMITED number of set-size meals a week from "
                                   "the house (breakfast + dinner only, no lunch). Food from Grizzly Chefs."))
    r = save_source_for_user(user.id, name="Grizzly Chefs weekly menu", url=MENU_PAGE, purpose="house menu", recheck="weekly")
    assert r.get("ok") and r.get("routine"), r
    row = db.get(AgentTask, r["routine"]["id"])
    row.run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    row.cycle_at = row.run_at
    row.watch_last = "never-seen"
    db.commit()

    sent = fire_due()
    db.expire_all()
    row = db.get(AgentTask, row.id)
    print(f"\n[ROUTINE] sent={sent} status={row.status} result={row.result!r} next={row.run_at}")
    print(f"[ROUTINE] attachments={attachments}")
    print(f"[ROUTINE] texts={texts}")
    staged = _pending_list(user.id)
    print(f"[ROUTINE] staged={[(p.get('kind'), p.get('name'), p.get('week_of'), p.get('n')) for p in staged]}")
    for p in staged:
        print("[ROUTINE] items:", [i['item'] for i in p.get('items', [])])

    assert sent == 1 and texts, "the routine should have asked them in one line"
    assert attachments and attachments[0][1] in ("image/png", "image/jpeg", "image/webp"), "the menu picture should have gone out"
    assert not (db.get(User, user.id).saved_menus or {}), "a background run must never SAVE a menu"
    assert staged and staged[0]["kind"] == "save_menu" and staged[0].get("source_url"), "the menu should be STAGED behind the gate"
    low = texts[-1].lower()
    assert "saved" not in low or "not saved" in low or "once" in low, f"never claim it's saved: {texts[-1]!r}"
    assert row.status == "pending" and row.run_at > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    blk = render_pending_block(user.id)
    assert "WAITING ON THEIR YES" in blk and "confirmed=true" in blk

    # their yes, in a normal turn → written from the staged payload
    from agent_loop import run_agent_loop
    db.add(Message(user_id=user.id, direction="out", body=texts[-1], message_type="task", channel="imessage"))
    db.commit()
    reply = run_agent_loop(db.get(User, user.id), "yeah that's it 👍", "freeform", message_id="live-yes-1")
    print(f"[YES] reply: {reply!r}")
    db.expire_all()
    menus = db.get(User, user.id).saved_menus or {}
    print(f"[YES] saved menus: {[(k, v.get('week_of'), len(v.get('items', []))) for k, v in menus.items()]}")
    assert menus, "their yes should have written the staged menu"
    assert _pending_list(user.id) == []
