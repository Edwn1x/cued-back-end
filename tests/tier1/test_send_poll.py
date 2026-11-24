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
    sent: list = []

    def _fake_imessage(phone, body):
        sent.append((phone, body))
        return f"photon-{len(sent)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake_imessage)
    monkeypatch.setattr(sms, "outage_active", lambda: False)
    yield sent
    sms.clear_outage()


@pytest.fixture
def imessage_sends(_on):
    """(phone, body) of every iMessage text the code sent — the poll's question bubble lands here."""
    return _on


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

def test_imessage_user_gets_a_poll_bubble_plus_the_question_and_the_row_carries_the_poll_id(db, poll_ok, imessage_sends):
    """iMessage polls have no title (live 2026-11-14: choices, no question — and a person-to-person poll shows none either), so the
    question goes out as its own text bubble; the poll row carries the poll id for vote routing."""
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    out = agent_tools.dispatch_tool("send_poll", ASK, u.id)
    assert out.startswith("ok: poll sent") and "never restate the question" in out
    assert poll_ok == [(u.phone, "gym time today?", ["4", "5:30", "7", "not today"])]
    assert imessage_sends == [(u.phone, "gym time today?")], "the question is its own bubble"
    q, row = _rows(db, u.id, "out")
    assert (q.message_type, q.body) == ("poll_question", "gym time today?")
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
    assert row.message_type == "poll" and row.channel == "sms", "the list carries the question — no separate bubble"


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
        ({"title": "x?", "options": ["a", "b"], "topic": "t" * 61}, "topic"),
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


def _age(db, row, hours):
    from datetime import datetime, timedelta, timezone
    row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    db.commit()


def _vote(db, user, title, option="x"):
    from models import Message
    db.add(Message(user_id=user.id, direction="in", body=f'[picked "{option}" on the poll "{title}"]',
                   message_type="freeform", channel="imessage", provider_sid=f"v-{title[:8]}-{option}",
                   delivery_status="delivered"))
    db.commit()


def test_same_topic_still_open_is_blocked_a_different_topic_is_not(db, poll_ok):
    """The gate keys on the QUESTION, not on 'a poll exists': an unanswered 'gym time?' must
    not stop 'how'd legs feel?' hours later, but re-asking gym time while it's open is nagging."""
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert agent_tools.dispatch_tool("send_poll", {**ASK, "topic": "gym time"}, u.id).startswith("ok:")
    again = agent_tools.dispatch_tool("send_poll", {"title": "when u hitting the gym?", "options": ["now", "later"],
                                                    "topic": "Gym Time!"}, u.id)
    assert again.startswith("error:") and "already" in again and "gym time today?" in again
    other = agent_tools.dispatch_tool("send_poll", {"title": "how'd legs feel?", "options": ["easy", "about right", "brutal"],
                                                    "topic": "session feel"}, u.id)
    assert other.startswith("ok:")
    assert len(poll_ok) == 2


def test_topic_defaults_to_the_title(db, poll_ok):
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert agent_tools.dispatch_tool("send_poll", ASK, u.id).startswith("ok:")
    same = agent_tools.dispatch_tool("send_poll", {"title": "Gym time today", "options": ["4", "7"]}, u.id)
    assert same.startswith("error:") and "already" in same
    (row,) = [r for r in _rows(db, u.id, "out") if r.message_type == "poll"]
    assert row.body == '[poll "gym time today?": 4 / 5:30 / 7 / not today]', "no topic suffix when it's just the title"
    import polls
    assert polls.poll_topic(row.body) == polls.normalize_topic("gym time today?")


def test_a_pick_or_the_lifetime_closes_a_topic(db, poll_ok):
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert agent_tools.dispatch_tool("send_poll", {**ASK, "topic": "gym time"}, u.id).startswith("ok:")
    _vote(db, u, "gym time today?", "5:30")
    assert agent_tools.dispatch_tool("send_poll", {**ASK, "topic": "gym time"}, u.id).startswith("ok:")
    _age(db, [r for r in _rows(db, u.id, "out") if r.message_type == "poll"][-1], config.POLL_LIFETIME_HOURS + 1)
    assert agent_tools.dispatch_tool("send_poll", {**ASK, "topic": "gym time"}, u.id).startswith("ok:")
    assert len(poll_ok) == 3


def test_two_unanswered_polls_means_ask_in_words(db, poll_ok):
    """Per-topic alone would let the coach fire five polls at someone who isn't tapping. Two
    ignored polls is the signal: fall back to words until one is answered or ages out."""
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert agent_tools.dispatch_tool("send_poll", {"title": "gym time today?", "options": ["4", "7"], "topic": "gym"}, u.id).startswith("ok:")
    assert agent_tools.dispatch_tool("send_poll", {"title": "lunch?", "options": ["bowl", "wrap"], "topic": "lunch"}, u.id).startswith("ok:")
    third = agent_tools.dispatch_tool("send_poll", {"title": "sleep?", "options": ["good", "rough"], "topic": "sleep"}, u.id)
    assert third.startswith("error:") and "unanswered" in third and "ask in words" in third
    _vote(db, u, "lunch?", "bowl")
    assert agent_tools.dispatch_tool("send_poll", {"title": "sleep?", "options": ["good", "rough"], "topic": "sleep"}, u.id).startswith("ok:")
    assert len(poll_ok) == 3


def test_topic_rides_on_the_row_when_it_differs_from_the_title(db, poll_ok):
    import agent_tools, polls
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    agent_tools.dispatch_tool("send_poll", {"title": "when u hitting rsf?", "options": ["4", "7"], "topic": "gym time"}, u.id)
    (row,) = [r for r in _rows(db, u.id, "out") if r.message_type == "poll"]
    assert row.body == '[poll "when u hitting rsf?" (gym time): 4 / 7]'
    assert polls.poll_topic(row.body) == "gym time"
    assert polls.poll_title(row.body) == "when u hitting rsf?"


def test_sidecar_failure_is_an_honest_error_and_no_row(db, monkeypatch):
    import agent_tools, sms
    monkeypatch.setattr(sms, "_send_imessage_poll",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sidecar /poll 502: polls unsupported on this chat")))
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    out = agent_tools.dispatch_tool("send_poll", ASK, u.id)
    assert out.startswith("error:") and "poll itself didn't" in out and "options in words" in out
    rows = _rows(db, u.id, "out")
    assert [r.message_type for r in rows] == ["poll_question"], "the question went out; no poll row, so the gate stays open"


def test_allowlist_narrows_the_tool_to_named_users(db, poll_ok, client, driver, anthropic_stub, monkeypatch):
    """Staged rollout: SEND_POLL_USER_IDS set → the handler refuses other users AND the loop never
    offers them the tool; empty → the flag alone decides."""
    import agent_tools
    from polls import send_poll_enabled_for
    me = make_user(db, preferred_channel="imessage", onboarding_step=3)
    other = make_user(db, preferred_channel="imessage", onboarding_step=3)
    monkeypatch.setattr(config, "SEND_POLL_USER_IDS", frozenset({me.id}))
    assert send_poll_enabled_for(me.id) and not send_poll_enabled_for(other.id)
    assert agent_tools.dispatch_tool("send_poll", ASK, me.id).startswith("ok:")
    out = agent_tools.dispatch_tool("send_poll", ASK, other.id)
    assert out.startswith("error:") and "not enabled" in out
    assert len(poll_ok) == 1
    # the loop's offered tool set follows the same answer
    offered: set = set()
    def handler(kw):
        offered.update(t.get("name") for t in (kw.get("tools") or []))   # every model call this inbound made
        return "ok"
    anthropic_stub.reply_with(handler)
    driver.send(other, "yo")
    assert offered and "send_poll" not in offered
    offered.clear()
    driver.send(me, "yo")
    assert "send_poll" in offered
    monkeypatch.setattr(config, "SEND_POLL_USER_IDS", frozenset())
    assert send_poll_enabled_for(other.id)


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

# The REAL shape of a vote's Photon id (live 2026-11-14, 121 chars): poll guid : phone : option UUID : action : ms.
VOTE_ID = "spc-msg-363e8fae-728e-453b-886f-e67a65661a16:%s:066F1AD7-DAF6-45F6-8FCD-E83BD3E3E6A7:selected:1794705409446"


def _payload(phone, text, **extra):
    return {"phone": phone, "text": text, "provider_message_id": extra.pop("pid", VOTE_ID % phone),
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
    # A tap is a complete answer: the reply is armed on the near-instant band, not the 10–15s
    # text band (founder 2026-11-14: "i wasn't gonna send a follow up").
    from tests._sync import PENDING_TIMERS
    assert PENDING_TIMERS[u.phone].interval <= config.POLL_VOTE_BUFFER_S[1] <= 1
    driver.flush(u.phone)
    assert [m.body for m in _rows(db, u.id, "in")] == [text]
    assert seen and seen[0][0]["content"] == text, "the model sees the pick as the user's message"
    # and that topic is no longer open — the same ask can go out again
    assert agent_tools.dispatch_tool("send_poll", ASK, u.id).startswith("ok:")


def test_a_titleless_vote_takes_its_title_from_our_row(db, client, driver, anthropic_stub, poll_ok):
    """iMessage polls have no title (the API field never renders) and the wire's `title` is the OPTION's
    title, so the sidecar's text can't name the poll — the row with provider_sid == poll_id can."""
    import agent_tools
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    agent_tools.dispatch_tool("send_poll", ASK, u.id)                       # provider_sid POLL-GUID-1
    seen = []
    anthropic_stub.reply_with(_turns(seen))
    r = _post(client, _payload(u.phone, '[picked "5:30" on the poll ""]', kinds=["poll_option"],
                               poll={"poll_id": "POLL-GUID-1", "title": "", "option": "5:30",
                                     "selected": True, "options": ASK["options"]}))
    assert r.status_code == 200 and r.get_json()["poll_vote"] is True
    driver.flush(u.phone)
    assert [m.body for m in _rows(db, u.id, "in")] == ['[picked "5:30" on the poll "gym time today?"]']
    # …and the gate sees that poll as answered
    assert agent_tools.dispatch_tool("send_poll", ASK, u.id).startswith("ok:")


def test_a_real_length_vote_id_fits_the_row_and_the_idempotency_claim(db):
    """Live 2026-11-14 01:17 UTC: the first vote 500'd — provider_sid was VARCHAR(80), the vote id is
    121 chars — and processed_messages.message_sid (64) made the claim fail open. Both are 200 now."""
    from models import claim_message_sid, Message
    from sms import log_incoming
    u = make_user(db)
    sid = VOTE_ID % u.phone
    assert len(sid) > 100
    assert claim_message_sid(sid, u.id) is True
    assert claim_message_sid(sid, u.id) is False, "the second claim is the duplicate — the first one really landed"
    log_incoming(u.id, '[picked "x" on the poll "y"]', channel="imessage", provider_sid=sid)
    db.expire_all()
    assert db.query(Message).filter(Message.user_id == u.id, Message.provider_sid == sid).count() == 1


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
    (row,) = [r for r in _rows(db, u.id, "out") if r.message_type == "poll"]
    assert row.message_type == "poll"
    from datetime import datetime, timezone
    u2 = make_user(db, preferred_channel="imessage", onboarding_step=3)
    u2.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    r = client.post("/admin/poll/send", data={"user_id": u2.id, "title": "x?", "options": "a|b"})
    assert r.status_code == 400
    r = client.post("/admin/poll/send", data={"user_id": u.id, "title": "x?", "options": "only"})
    assert r.status_code == 400


# ── heartbeat: the proactive path gets the same tool (founder: "full fledged production") ──

def _hb_user(db, monkeypatch):
    from tests.tier1.test_heartbeat import _allow
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _allow(monkeypatch, u)
    return u


def _hb_ticks(user_id):
    from tests.tier1.test_heartbeat import _ticks
    return _ticks(user_id)


def test_heartbeat_offers_send_poll_only_to_allowlisted_users(db, monkeypatch, anthropic_stub):
    import heartbeat
    from tests._fake_anthropic import ToolUse
    me = _hb_user(db, monkeypatch)
    other = make_user(db, preferred_channel="imessage", onboarding_step=3)
    monkeypatch.setattr(config, "SEND_POLL_USER_IDS", frozenset({me.id}))
    seen = {}
    def handler(kw):
        seen.setdefault("tools", []).append({t.get("name") for t in (kw.get("tools") or [])})
        seen.setdefault("sys", []).append("".join(b["text"] for b in kw.get("system")))
        return ToolUse("stay_silent", {"reason": "quiet"})
    anthropic_stub.reply_with(handler)
    heartbeat.decide(me.id)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [other.phone])
    heartbeat.decide(other.id)
    assert "send_poll" in seen["tools"][0] and "ASK WITH A POLL" in seen["sys"][0]
    assert "send_poll" not in seen["tools"][1] and "ASK WITH A POLL" not in seen["sys"][1]


def test_heartbeat_send_poll_is_the_ticks_text(db, monkeypatch, anthropic_stub, poll_ok, imessage_sends, sms_capture):
    """send_poll on a tick: question bubble + poll go out from decide, NO heartbeat text on top,
    the tick is SPOKE with the poll's row body as its message (anti-repetition sees it)."""
    import heartbeat
    from tests._fake_anthropic import ToolUse
    u = _hb_user(db, monkeypatch)
    anthropic_stub.reply_with(lambda kw: ToolUse("send_poll", {"title": "gym time today?", "options": ["4", "5:30", "not today"], "topic": "gym time"}))
    heartbeat.heartbeat_tick(u.id)
    assert poll_ok == [(u.phone, "gym time today?", ["4", "5:30", "not today"])]
    assert imessage_sends == [(u.phone, "gym time today?")], "the question bubble and nothing else"
    assert not sms_capture
    tick = _hb_ticks(u.id)[-1]
    assert tick.spoke is True and tick.message == '[poll "gym time today?" (gym time): 4 / 5:30 / not today]'
    assert [r.message_type for r in _rows(db, u.id, "out")] == ["poll_question", "poll"], "no 'heartbeat' row on top"


def test_heartbeat_poll_refused_falls_through_to_text(db, monkeypatch, anthropic_stub, poll_ok, imessage_sends):
    """Same topic already open → the gate's line comes back as the tool result and the model
    sends a text instead; the tick is a normal spoke."""
    import agent_tools, heartbeat
    from tests._fake_anthropic import ToolUse
    u = _hb_user(db, monkeypatch)
    assert agent_tools.dispatch_tool("send_poll", {"title": "gym time today?", "options": ["4", "7"], "topic": "gym time"}, u.id).startswith("ok:")
    imessage_sends.clear()
    got = {}
    def handler(kw):
        msgs = kw.get("messages") or []
        last = msgs[-1]["content"] if msgs else ""
        if isinstance(last, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last):
            got["result"] = next(b["content"] for b in last if b.get("type") == "tool_result")
            return ToolUse("send_text", {"message": "u still hitting the gym today?"})
        return ToolUse("send_poll", {"title": "when u going today?", "options": ["4", "7"], "topic": "gym time"})
    anthropic_stub.reply_with(handler)
    heartbeat.heartbeat_tick(u.id)
    assert got["result"].startswith("error:") and "already out as a poll" in got["result"]
    assert len(poll_ok) == 1, "no second poll on the same topic"
    assert imessage_sends == [(u.phone, "u still hitting the gym today?")]
    tick = _hb_ticks(u.id)[-1]
    assert tick.spoke is True and tick.message == "u still hitting the gym today?"


# ── 2026-11-18 watch (1 poll in 4 days): defaults, briefing + poll in one tick, a durable attempt ledger ──

def test_polls_are_the_default_for_the_recurring_asks_in_both_paths():
    """The misses were all the same three asks stated as sentences ('gym after', 'lift between
    12:30 and 3:30 if u want', 'thurs or sat could work for a run'). Both paths now say: those are
    polls, not sentences."""
    import agent_tools, heartbeat
    desc = agent_tools.SEND_POLL_TOOL["description"]
    assert "a poll, not a sentence" in desc and "gym time" in desc and "run day" in desc and "which dish" in desc
    voice = open("prompts/voice.md").read()
    assert "a poll, not a sentence" in voice
    assert "a poll, not a sentence" in heartbeat._POLL_GUIDANCE
    assert "send_text AND send_poll" in heartbeat._POLL_GUIDANCE and "briefing" in heartbeat._POLL_GUIDANCE


def test_heartbeat_can_send_the_briefing_and_a_poll_in_one_tick(db, monkeypatch, anthropic_stub, poll_ok, imessage_sends, sms_capture):
    """The morning briefing was exactly where 'legs after c104?' belonged, and the tick had to
    choose. Now: send_text + send_poll in one response → the text first, then the question bubble,
    then the poll; ONE tick row, message = text --- poll body; nothing sent twice."""
    import heartbeat
    from tests._fake_anthropic import MultiText, ToolUse
    u = _hb_user(db, monkeypatch)
    anthropic_stub.reply_with(lambda kw: MultiText(
        ToolUse("send_text", {"message": "62° and clear. c104 at 3 then ur free"}, id="t1"),
        ToolUse("send_poll", {"title": "legs after c104?", "options": ["right after", "tonight", "not today"], "topic": "gym time"}, id="t2"),
        stop_reason="tool_use"))
    heartbeat.heartbeat_tick(u.id)
    assert imessage_sends == [(u.phone, "62° and clear. c104 at 3 then ur free"), (u.phone, "legs after c104?")], "text first, then the question bubble"
    assert poll_ok == [(u.phone, "legs after c104?", ["right after", "tonight", "not today"])]
    assert not sms_capture
    ticks = _hb_ticks(u.id)
    assert len(ticks) == 1 and ticks[-1].spoke is True
    assert ticks[-1].message == '62° and clear. c104 at 3 then ur free --- [poll "legs after c104?" (gym time): right after / tonight / not today]'
    assert [r.message_type for r in _rows(db, u.id, "out")] == ["heartbeat", "poll_question", "poll"]


def test_every_poll_attempt_is_recorded_with_its_gate_answer(db, poll_ok):
    """Refusals only went to the web log, which deploys rotate away — so 'did it try and get
    blocked, or never try?' had no answer. poll_attempts keeps every call: source, title, topic,
    ok, and the exact result line."""
    import agent_tools
    from models import get_session, PollAttempt
    u = make_user(db, preferred_channel="imessage", onboarding_step=3)
    assert agent_tools.dispatch_tool("send_poll", {**ASK, "topic": "gym time"}, u.id).startswith("ok:")
    blocked = agent_tools.dispatch_tool("send_poll", {**ASK, "topic": "gym time"}, u.id)
    assert blocked.startswith("error:")
    s = get_session()
    try:
        rows = s.query(PollAttempt).filter(PollAttempt.user_id == u.id).order_by(PollAttempt.id).all()
    finally:
        s.close()
    assert [(r.source, r.ok, r.topic) for r in rows] == [("coach", True, "gym time"), ("coach", False, "gym time")]
    assert rows[1].result.startswith("error:") and "already out as a poll" in rows[1].result
    assert rows[0].title == "gym time today?"


def test_reminder_times_and_yes_no_confirmations_are_poll_shaped_too():
    """Alex (u42, 2026-11-16): 'what time works' for study pings, 'lmk if u want them every
    week', 'want me to set a wake-up reminder?' — all asked in words. Those are polls."""
    import agent_tools, heartbeat
    desc = agent_tools.SEND_POLL_TOOL["description"]
    assert "reminder" in desc and "every week" in desc and "yes-or-no" in desc
    voice = open("prompts/voice.md").read()
    assert "what time works" in voice and "yes-or-no" in voice


def test_did_you_checks_are_polls_too():
    """2026-11-24 ledger read: 4 of the last 8 heartbeat asks were did-you checks ("u get it in",
    "u submit it yet", "u eat anything since chipotle", "push happen after the quiz") — the
    concrete yes/no the founder named on day one — and none was a poll, because the default list
    never named them. Shape: done / not yet / skipping it."""
    import agent_tools, heartbeat
    for text, where in ((agent_tools.SEND_POLL_TOOL["description"], "tool"),
                        (open("prompts/voice.md").read(), "voice.md"),
                        (heartbeat._POLL_GUIDANCE, "heartbeat")):
        assert "did-you" in text, where
        assert "done / not yet / skipping it" in text, where
