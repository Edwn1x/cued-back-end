"""Inbound kinds initiative, steps 7 + 8 — polls (plumbing, use-case independent).

Outbound (polls.py + agent_tools.SEND_POLL_TOOL): the coach puts an iMessage poll in the
thread — 2–4 options, logistics / self-report only ("you prescribe; you don't poll" stays
in voice.md), never before setup is done, one open poll per user. The sidecar POST is
captured; SMS users get the same ask as a numbered list (same code path, same row).

Inbound (/internal/inbound `poll` field, sidecar INBOUND_EXTRA_KINDS=poll_option): a vote
arrives as text naming the pick plus the structured copy — stored as an inbound row and
answered like any reply; a deselect is dropped before any model turn.
"""
from __future__ import annotations

import pytest

import config
from tests.factories import make_user

SIDECAR = "http://sidecar.test:8080"
SECRET = "testsecret"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "SEND_POLL_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    import sms
    # The coach's reply to a vote goes out over iMessage: route it to a fake sidecar so a
    # real connect attempt never parks the line (note_outage is module state that would
    # leak into the next test file).
    monkeypatch.setattr(sms, "_send_imessage", lambda phone, body: f"photon-{abs(hash(body)) % 10**6}")
    monkeypatch.setattr(sms, "outage_active", lambda: False)
    yield
    sms.clear_outage()


@pytest.fixture
def poll_ok(monkeypatch):
    import sms
    calls: list = []

    def _fake(phone, title, options):
        calls.append((phone, title, list(options)))
        return f"POLL-GUID-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage_poll", _fake)
    return calls


def _rows(db, user_id, direction):
    from models import Message
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user_id, Message.direction == direction)
            .order_by(Message.id).all())


ASK = {"title": "gym time today?", "options": ["4", "5:30", "7", "not today"]}


# ── outbound: the tool ────────────────────────────────────────────────────────────

def test_imessage_user_gets_a_poll_bubble_and_the_row_carries_the_poll_id(db, poll_ok):
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    out = agent_tools.dispatch_tool("send_poll", ASK, u.id)
    assert out.startswith("ok: poll sent") and "don't repeat the options" in out
    assert poll_ok == [(u.phone, "gym time today?", ["4", "5:30", "7", "not today"])]
    (row,) = _rows(db, u.id, "out")
    assert row.message_type == "poll" and row.provider_sid == "POLL-GUID-1" and row.channel == "imessage"
    assert row.body == '[poll "gym time today?": 4 / 5:30 / 7 / not today]'


def test_sms_user_gets_the_same_ask_as_a_numbered_list(db, poll_ok, sms_capture):
    import agent_tools
    u = make_user(db, onboarding_step=3)          # no imessage preference → sms
    out = agent_tools.dispatch_tool("send_poll", ASK, u.id)
    assert out.startswith("ok:") and "numbered list" in out
    assert not poll_ok
    assert sms_capture and sms_capture[-1][1] == "gym time today?\n1. 4\n2. 5:30\n3. 7\n4. not today"
    (row,) = _rows(db, u.id, "out")
    assert row.message_type == "poll" and row.channel == "sms"


def test_gates_flag_setup_option_count_and_lengths(db, poll_ok, monkeypatch):
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    bad = [
        ({"title": "x?", "options": ["only"]}, "2 to 4 options"),
        ({"title": "x?", "options": ["1", "2", "3", "4", "5"]}, "2 to 4 options"),
        ({"title": "", "options": ["a", "b"]}, "title"),
        ({"title": "x?", "options": ["a", ""]}, "every option"),
        ({"title": "x?", "options": ["a", "b" * 41]}, "40 chars"),
        ({"title": "q" * 121, "options": ["a", "b"]}, "120 chars"),
        ({"title": "x?", "options": ["a", "a"]}, "distinct"),
    ]
    for ti, needle in bad:
        out = agent_tools.dispatch_tool("send_poll", ti, u.id)
        assert out.startswith("error:") and needle in out, (ti, out)
    mid_setup = make_user(db, preferred_channel="imessage", onboarding_step=1)
    out = agent_tools.dispatch_tool("send_poll", ASK, mid_setup.id)
    assert out.startswith("error:") and "setup" in out
    monkeypatch.setattr(config, "SEND_POLL_TOOL_ENABLED", False)
    out = agent_tools.dispatch_tool("send_poll", ASK, u.id)
    assert out.startswith("error:") and "not enabled" in out
    assert not poll_ok


def test_one_open_poll_per_user_until_they_pick_or_it_ages_out(db, poll_ok, monkeypatch):
    import agent_tools
    from datetime import datetime, timedelta, timezone
    from models import Message
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert agent_tools.dispatch_tool("send_poll", ASK, u.id).startswith("ok:")
    again = agent_tools.dispatch_tool("send_poll", {"title": "and lunch?", "options": ["bowl", "wrap"]}, u.id)
    assert again.startswith("error:") and "already open" in again and "gym time today?" in again
    # their pick closes it
    db.add(Message(user_id=u.id, direction="in", body='[picked "5:30" on the poll "gym time today?"]',
                   message_type="freeform", channel="imessage", provider_sid="POLL-GUID-1:+1:o:selected:1",
                   delivery_status="delivered"))
    db.commit()
    assert agent_tools.dispatch_tool("send_poll", {"title": "and lunch?", "options": ["bowl", "wrap"]}, u.id).startswith("ok:")
    # an unanswered poll older than the window no longer blocks
    db.expire_all()
    last = _rows(db, u.id, "out")[-1]
    last.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=config.POLL_OPEN_HOURS + 1)
    db.commit()
    assert agent_tools.dispatch_tool("send_poll", {"title": "dinner?", "options": ["in", "out"]}, u.id).startswith("ok:")
    assert len(poll_ok) == 3


def test_sidecar_failure_is_an_honest_error_and_no_row(db, monkeypatch):
    import agent_tools, sms
    monkeypatch.setattr(sms, "_send_imessage_poll",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sidecar /poll 502: polls unsupported on this chat")))
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    out = agent_tools.dispatch_tool("send_poll", ASK, u.id)
    assert out.startswith("error:") and "couldn't send the poll" in out and "ask in words" in out
    assert not _rows(db, u.id, "out")


def test_loop_offers_the_tool_only_when_on_and_the_rules_are_pinned():
    import agent_loop, agent_tools
    src = open(agent_loop.__file__).read()
    assert "if config.SEND_POLL_TOOL_ENABLED:" in src and "SEND_POLL_TOOL" in src
    desc = agent_tools.SEND_POLL_TOOL["description"]
    assert "2" in desc and "4" in desc and "never" in desc.lower()
    assert "prescribe" in desc.lower()
    voice = open("prompts/voice.md").read()
    assert "send_poll" in voice and "you don't poll" in voice


# ── inbound: the vote ─────────────────────────────────────────────────────────────

def _payload(phone, text, **extra):
    return {"phone": phone, "text": text, "provider_message_id": extra.pop("pid", "POLL-GUID-1:%s:opt-2:selected:1" % phone),
            "chat_guid": f"any;-;{phone}", "service": "iMessage", "line_phone": "+15102646604",
            "timestamp": "2026-11-14T12:00:00.000Z", "attachments": [], **extra}


def _post(client, payload):
    return client.post("/internal/inbound", json=payload, headers={"X-Internal-Secret": SECRET})


def _turns(store):
    def handler(kw):
        store.append(kw.get("messages") or [])
        return "ok"
    return handler


def test_a_vote_is_stored_and_answered_like_a_reply(db, client, driver, anthropic_stub, poll_ok):
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    agent_tools.dispatch_tool("send_poll", ASK, u.id)
    seen = []
    anthropic_stub.reply_with(_turns(seen))
    text = '[picked "5:30" on the poll "gym time today?"]'
    r = _post(client, _payload(u.phone, text, kinds=["poll_option"],
                               poll={"poll_id": "POLL-GUID-1", "title": "gym time today?", "option": "5:30",
                                     "selected": True, "options": ASK["options"]}))
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True, "poll_vote": True}
    driver.flush(u.phone)
    assert [m.body for m in _rows(db, u.id, "in")] == [text]
    assert seen and seen[0][0]["content"] == text, "the model sees the pick as the user's message"
    # and the poll is no longer open
    assert agent_tools.dispatch_tool("send_poll", {"title": "lunch?", "options": ["bowl", "wrap"]}, u.id).startswith("ok:")


def test_a_deselect_is_dropped_before_any_model_turn(db, client, driver, anthropic_stub):
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    seen = []
    anthropic_stub.reply_with(_turns(seen))
    r = _post(client, _payload(u.phone, '[un-picked "5:30" on the poll "gym time today?"]', kinds=["poll_option"],
                               pid="POLL-GUID-1:%s:opt-2:deselected:2" % u.phone,
                               poll={"poll_id": "POLL-GUID-1", "title": "gym time today?", "option": "5:30",
                                     "selected": False, "options": ASK["options"]}))
    assert r.status_code == 200 and r.get_json() == {"ok": True, "known": True, "ignored": "poll_unvote"}
    driver.flush(u.phone)
    assert _rows(db, u.id, "in") == [] and seen == []


# ── admin: the founder's live render check ─────────────────────────────────────────

def test_admin_poll_send_bypasses_the_tool_flag_but_not_the_archive_guard(db, client, poll_ok, monkeypatch):
    monkeypatch.setattr(config, "SEND_POLL_TOOL_ENABLED", False)
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    r = client.post("/admin/poll/send", data={"user_id": u.id, "title": "renders in a dm?", "options": "yes | no"})
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert poll_ok == [(u.phone, "renders in a dm?", ["yes", "no"])]
    (row,) = _rows(db, u.id, "out")
    assert row.message_type == "poll"
    from datetime import datetime, timezone
    u2 = make_user(db, preferred_channel="imessage", onboarding_step=3)
    u2.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    r = client.post("/admin/poll/send", data={"user_id": u2.id, "title": "x?", "options": "a|b"})
    assert r.status_code == 400
    r = client.post("/admin/poll/send", data={"user_id": u.id, "title": "x?", "options": "only"})
    assert r.status_code == 400
