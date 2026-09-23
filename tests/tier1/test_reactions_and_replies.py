"""
iMessage tapback reactions + threaded replies (founder, 2026-09-11) — part 2, the
Flask side. The HOW (sidecar /react, reply_to on /send) is in the sidecar suite.

The rule that bites (founder): a reaction NEVER counts as an unanswered outbound.
Rule 1 exists to close loops; if a silence gate saw the 👍 as an outbound the user
didn't reply to, closure would become a strike. Excluded by message_type=reaction
in every place that counts silence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import pytest

from tests._fake_anthropic import ToolUse, MultiText
from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"


@pytest.fixture
def imessage_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "IMESSAGE_REACTIONS_ENABLED", True)
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", False)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)  # the tools live in the loop


@pytest.fixture
def sidecar(monkeypatch):
    """Fake sidecar: records /send, /react, /typing; answers ok."""
    import requests
    calls: list = []

    class _R:
        def __init__(self, payload, status=200):
            self._p, self.status_code, self.text = payload, status, str(payload)
        def json(self):
            return self._p

    def _post(url, json=None, headers=None, timeout=None):
        route = url.rsplit("/", 1)[-1]
        calls.append((route, json))
        if route == "react" and json.get("message_id") == "spc-msg-gone":
            return _R({"ok": False, "error": "message not found"}, 502)
        return _R({"ok": True, "provider_message_id": f"spc-{route}-{len(calls)}"})
    monkeypatch.setattr(requests, "post", _post)
    return calls


def _inbound(db, user, body, sid, minutes_ago=1):
    from models import get_session, Message
    s = get_session()
    try:
        m = Message(user_id=user.id, direction="in", body=body, message_type="freeform",
                    channel="imessage", provider_sid=sid, delivery_status="delivered",
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago))
        s.add(m); s.commit(); return m.id
    finally:
        s.close()


def _rows(db, user, direction="out"):
    from models import Message
    db.expire_all()
    return db.query(Message).filter(Message.user_id == user.id, Message.direction == direction).order_by(Message.id).all()


# ── the never-a-strike rule ──────────────────────────────────────────────────

def test_reaction_rows_are_invisible_to_every_silence_gate(db, imessage_on, sidecar):
    from engagement_tracker import increment_unanswered, has_unanswered_outbound, has_unanswered_proactive
    from sms import react_to_message
    from models import User
    user = make_user(db, preferred_channel="imessage", unanswered_count=0)
    mid = _inbound(db, user, "Right right", "spc-msg-ack")

    assert react_to_message(user.id, "spc-msg-ack", "like") is True
    rows = _rows(db, user)
    assert len(rows) == 1 and rows[0].message_type == "reaction" and rows[0].delivery_status == "sent"
    assert "👍" in rows[0].body

    # the 👍 is the LAST outbound. It must not be a strike, and it must not gate.
    increment_unanswered(user.id)
    db.expire_all()
    assert db.get(User, user.id).unanswered_count == 0, "closure became a strike"
    assert has_unanswered_outbound(user.id) is False
    assert has_unanswered_proactive(user.id, 240) is False


def test_failed_reaction_is_logged_not_a_strike_and_never_trips_the_breaker(db, imessage_on, sidecar, caplog):
    from sms import react_to_message
    from engagement_tracker import increment_unanswered
    from models import User
    user = make_user(db, preferred_channel="imessage")
    _inbound(db, user, "lol", "spc-msg-gone")
    with caplog.at_level(logging.WARNING):
        assert react_to_message(user.id, "spc-msg-gone", "laugh") is False
    rows = _rows(db, user)
    assert rows[-1].message_type == "reaction" and rows[-1].delivery_status == "failed"
    increment_unanswered(user.id)
    db.expire_all()
    u = db.get(User, user.id)
    assert u.unanswered_count == 0
    assert u.channel_failed_over is False, "a stale message id is not a channel failure"
    assert any("REACTION_FAILED" in r.getMessage() and "breaker untouched" in r.getMessage() for r in caplog.records)


def test_heartbeat_last_message_clock_ignores_reactions(db, imessage_on, sidecar, anthropic_stub, monkeypatch):
    """A tapback is not 'your last message' for the open-thread clock."""
    import config, heartbeat
    from sms import react_to_message, _log_message
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_WEB_SEARCH", False)
    user = make_user(db, preferred_channel="imessage")
    _log_message(user.id, "you gonna lift today?", "heartbeat", channel="imessage", provider_sid="spc-1", delivery_status="sent")
    _inbound(db, user, "yeah", "spc-msg-yeah")
    react_to_message(user.id, "spc-msg-yeah", "like")
    seen = {}
    def _h(kw):
        seen["system"] = kw.get("system")
        return ToolUse("stay_silent", {"reason": "n/a"})
    anthropic_stub.reply_with(_h)
    try:
        heartbeat.decide(user.id)
    except Exception:
        pass
    sysm = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in (seen.get("system") or []))
    assert "TIME SINCE YOUR LAST MESSAGE" in sysm
    # the reaction row is the most recent outbound; the clock must have skipped it —
    # i.e. the age reported is the heartbeat text's age (older), not ~0.0 from the tapback
    import re
    m = re.search(r"TIME SINCE YOUR LAST MESSAGE\n~([\d.]+) hours", sysm)
    assert m is not None


# ── the tools ────────────────────────────────────────────────────────────────

def test_tools_offered_only_to_imessage_users(db, imessage_on, sidecar, anthropic_stub):
    from agent_loop import run_agent_loop
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.setdefault("tools", [t["name"] for t in kw.get("tools", []) if "name" in t]) and "hey")
    im = make_user(db, preferred_channel="imessage")
    run_agent_loop(im, "hi", "freeform")
    assert {"react_to_message", "reply_in_thread"} <= set(seen["tools"])

    seen.clear()
    sms_user = make_user(db, preferred_channel="sms")
    run_agent_loop(sms_user, "hi", "freeform")
    assert not ({"react_to_message", "reply_in_thread"} & set(seen["tools"]))

    seen.clear()
    tripped = make_user(db, preferred_channel="imessage", channel_failed_over=True)
    run_agent_loop(tripped, "hi", "freeform")
    assert not ({"react_to_message", "reply_in_thread"} & set(seen["tools"])), "tripped breaker = no iMessage affordances"


def test_context_tags_their_imessages_with_refs(db, imessage_on):
    from agent_loop import build_loop_context
    from models import get_session
    user = make_user(db, preferred_channel="imessage")
    mid = _inbound(db, user, "we got malatang after", "spc-msg-m1")
    s = get_session()
    try:
        ctx = build_loop_context(user, s)
    finally:
        s.close()
    assert f"[m{mid}]: we got malatang after" in ctx


def test_reaction_only_turn_sends_no_text_and_clears_typing(db, imessage_on, sidecar, anthropic_stub, sms_capture, monkeypatch):
    """The model reacts and ends with no text → nothing is sent, no fallback fires,
    the row is the reaction, and the typing bubble is cleared."""
    import app, config
    from engagement_tracker import increment_unanswered
    from models import User
    monkeypatch.setattr(config, "TYPING_INDICATOR_ENABLED", True)
    import typing_indicator
    from tests import _sync
    monkeypatch.setattr(typing_indicator, "threading", _sync.make_threading_shim(Thread=_sync.SyncThread))
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    mid = _inbound(db, user, "Right right", "spc-msg-rr")

    anthropic_stub.push(ToolUse("react_to_message", {"message_ref": f"m{mid}", "emoji": "like"}),
                        MultiText(stop_reason="end_turn"))  # no text block at all
    app.process_buffered_message(user.id, "Right right", "freeform")

    routes = [r for r, _ in sidecar]
    assert routes.count("react") == 1 and "send" not in routes, f"a reaction-only turn sends no text: {routes}"
    assert "typing" in routes and [j for r, j in sidecar if r == "typing"][-1]["state"] == "stop"
    assert sms_capture == [], "no Twilio fallback on a reaction-only turn"
    out = _rows(db, user)
    assert [r.message_type for r in out] == ["reaction"]
    increment_unanswered(user.id)
    db.expire_all()
    assert db.get(User, user.id).unanswered_count == 0


def test_react_and_text_in_one_turn_and_one_reaction_max(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    m1 = _inbound(db, user, "frat bro said lets go so i went", "spc-msg-story", minutes_ago=2)
    m2 = _inbound(db, user, "even tho i had to study", "spc-msg-study", minutes_ago=1)
    anthropic_stub.push(ToolUse("react_to_message", {"message_ref": f"m{m1}", "emoji": "laugh"}),
                        ToolUse("react_to_message", {"message_ref": f"m{m2}", "emoji": "love"}),
                        "accountability buddy clutch. but real talk — quiz is at 4.")
    app.process_buffered_message(user.id, "frat bro said lets go so i went\neven tho i had to study", "freeform")
    reacts = [j for r, j in sidecar if r == "react"]
    assert len(reacts) == 1 and reacts[0]["emoji"] == "laugh" and reacts[0]["message_id"] == "spc-msg-story"
    # the second react was refused by the handler, and the text still went out
    tool_results = [c for c in anthropic_stub.calls if any(isinstance(m.get("content"), list) and
                    any(isinstance(b, dict) and b.get("type") == "tool_result" and "already reacted" in str(b.get("content")) for b in m["content"])
                    for m in c["messages"])]
    assert tool_results, "second reaction in one turn must be refused"
    sends = [j for r, j in sidecar if r == "send"]
    assert len(sends) == 1 and "quiz is at 4" in sends[0]["text"]


def test_reply_in_thread_threads_this_turns_text(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    m_days = _inbound(db, user, "rn prolly like 3-4 days a week", "spc-msg-days", minutes_ago=2)
    _inbound(db, user, "also malatang is 25 dollars a bowl", "spc-msg-food", minutes_ago=1)
    anthropic_stub.push(ToolUse("reply_in_thread", {"message_ref": f"m{m_days}"}),
                        "4 days is honestly plenty if you're busy.")
    app.process_buffered_message(user.id, "rn prolly like 3-4 days a week\nalso malatang is 25 dollars a bowl", "freeform")
    sends = [j for r, j in sidecar if r == "send"]
    assert len(sends) == 1 and sends[0]["reply_to"] == "spc-msg-days"
    assert sends[0]["text"].startswith("4 days is honestly plenty")
    # a plain turn afterwards carries no reply_to (per-turn state was consumed)
    sidecar.clear()
    anthropic_stub.reply_with(lambda kw: "go study")
    app.process_buffered_message(user.id, "ok", "freeform")
    sends = [j for r, j in sidecar if r == "send"]
    assert len(sends) == 1 and "reply_to" not in sends[0]


def test_bad_refs_and_emoji_are_refused_cleanly(db, imessage_on, sidecar):
    from agent_tools import handle_react_to_message, handle_reply_in_thread, begin_turn
    user = make_user(db, preferred_channel="imessage")
    sms_msg_id = None
    from models import get_session, Message
    s = get_session()
    try:
        m = Message(user_id=user.id, direction="in", body="via sms", message_type="freeform", channel="sms", provider_sid="SM1")
        s.add(m); s.commit(); sms_msg_id = m.id
    finally:
        s.close()
    begin_turn(user.id)
    assert handle_react_to_message(user.id, {"message_ref": "m999999", "emoji": "like"}).startswith("error: no iMessage")
    assert handle_react_to_message(user.id, {"message_ref": f"m{sms_msg_id}", "emoji": "like"}).startswith("error: no iMessage"), "an SMS row is not a tapback target"
    assert handle_react_to_message(user.id, {"message_ref": "m1", "emoji": "this is not an emoji"}).startswith("error: emoji")
    assert handle_reply_in_thread(user.id, {"message_ref": "m999999"}).startswith("error")
    assert sidecar == []


def test_voice_carries_the_rules():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    for phrase in ("React INSTEAD of texting", "END YOUR TURN WITH NO TEXT", "‼️ is the hype tapback",
                   "participation trophy", "never the action", "At most ONE reaction per user turn",
                   "No tapback is banned", "no warm-up period", "never for a coaching call-out",
                   "A reaction never counts against them"):
        assert phrase in v, phrase


# ── what the live eval caught ────────────────────────────────────────────────

def test_reaction_only_sentinel_and_meta_notes_are_swallowed(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """Live eval: after reacting, the model sent 'Turn ended with reaction, no text
    needed.' / 'No text needed.' as the TEXT. Those must never reach the user."""
    import app
    from agent_tools import is_reaction_only_text, REACTION_ONLY_SENTINEL
    assert is_reaction_only_text(REACTION_ONLY_SENTINEL, True)
    for note in ("No text needed.", "Turn ended with reaction, no text needed.", "(reaction only)", "nothing else to add", ""):
        assert is_reaction_only_text(note, True), note
    assert not is_reaction_only_text("No text needed.", False), "without a reaction it's just a (bad) text — not ours to swallow"
    assert not is_reaction_only_text("go study, quiz at 4", True)

    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    mid = _inbound(db, user, "Right right", "spc-msg-rr")
    anthropic_stub.push(ToolUse("react_to_message", {"message_ref": f"m{mid}", "emoji": "like"}),
                        "Turn ended with reaction, no text needed.")
    app.process_buffered_message(user.id, "Right right", "freeform")
    routes = [r for r, _ in sidecar]
    assert routes.count("react") == 1 and "send" not in routes, routes
    assert sms_capture == []


def test_lone_emoji_text_becomes_a_tapback_on_their_latest_message(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """Live eval: 'Hopefully I do good' → the coach TEXTED '❤️'. On iMessage that is a
    tapback that lost its way."""
    import app
    from agent_tools import is_single_emoji_text
    assert is_single_emoji_text("❤️") and is_single_emoji_text(" 😂 ") and is_single_emoji_text("👍")
    assert not is_single_emoji_text("❤️ you got this") and not is_single_emoji_text("ok")

    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "Hopefully I do good", "spc-msg-hope")
    anthropic_stub.reply_with(lambda kw: "❤️")
    app.process_buffered_message(user.id, "Hopefully I do good", "freeform")
    reacts = [j for r, j in sidecar if r == "react"]
    assert reacts == [{"phone": user.phone, "message_id": "spc-msg-hope", "emoji": "❤️"}]
    assert not [j for r, j in sidecar if r == "send"], "the ❤️ must not go out as a bubble"
    assert [r.message_type for r in _rows(db, user)] == ["reaction"]


def test_lone_emoji_text_stays_a_text_for_sms_users(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    import app
    user = make_user(db, preferred_channel="sms", onboarding_step=3)
    anthropic_stub.reply_with(lambda kw: "❤️")
    app.process_buffered_message(user.id, "Hopefully I do good", "freeform")
    assert sms_capture and sms_capture[0][1].strip() != "", "SMS has no tapbacks — the text goes out"
    assert not [j for r, j in sidecar if r == "react"]


def test_voice_forbids_announcing_new_targets():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "Their calorie and protein targets are set in code" in v
    assert "never change them by stating a new number" in v and "set_targets" in v


def test_tool_call_written_as_text_is_executed_never_sent(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """Live eval: on 'Alr alr / I'm on it bro' the model wrote 'react_to_message 👍' as
    its TEXT (a documented low-effort failure mode). That must become the tapback."""
    import app
    from agent_tools import leaked_tool_call
    assert leaked_tool_call("react_to_message 👍") == ("react_to_message", "👍")
    assert leaked_tool_call("react_to_message(m12, like)") == ("react_to_message", "like")
    assert leaked_tool_call('react_to_message: message_ref=m12 emoji=laugh') == ("react_to_message", "laugh")
    assert leaked_tool_call("reply_in_thread m12") == ("reply_in_thread", "m12")
    assert leaked_tool_call("go study. quiz at 4.") is None
    assert leaked_tool_call("i'd react_to_message but nah") is None

    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "I'm on it bro", "spc-msg-onit")
    anthropic_stub.reply_with(lambda kw: "react_to_message 👍")
    app.process_buffered_message(user.id, "Alr alr\nI'm on it bro", "freeform")
    reacts = [j for r, j in sidecar if r == "react"]
    assert reacts == [{"phone": user.phone, "message_id": "spc-msg-onit", "emoji": "👍"}]
    assert not [j for r, j in sidecar if r == "send"] and sms_capture == []


def test_questions_never_get_a_tapback_by_code(db, imessage_on, sidecar, anthropic_stub, sms_capture):
    """Live eval: 'Why does everyone think that c104 means data science?' got a 😂.
    Rule 3 is categorical, so it is enforced in code on every reaction path."""
    import app
    from agent_tools import handle_react_to_message, begin_turn, is_question_message
    assert is_question_message("why does everyone think c104 means data science?")
    assert not is_question_message("hit pull")
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    mid = _inbound(db, user, "Why does everyone think that c104 means data science?", "spc-msg-q")
    begin_turn(user.id)
    out = handle_react_to_message(user.id, {"message_ref": f"m{mid}", "emoji": "laugh"})
    assert out.startswith("error:") and "question" in out
    assert sidecar == []
    # the automatic conversions respect it too: a lone ❤️ text after a question is dropped
    # rather than tapbacked (and, with no reaction, would be a bad text — so it's sent as-is
    # only when it isn't emoji-only; here the loop falls through to sending the text)
    anthropic_stub.reply_with(lambda kw: "❤️")
    app.process_buffered_message(user.id, "Why does everyone think that c104 means data science?", "freeform")
    assert not [j for r, j in sidecar if r == "react"], "no tapback on a question, even via the emoji-text path"


# ── rule 1 on a STANDALONE ack: the webhook's ack suppression happens before the buffer ──

def _post_imessage(client, phone, text, msg_id):
    import json
    return client.post("/internal/inbound", data=json.dumps({
        "phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": "g", "service": "iMessage",
        "line_phone": "+16282649335", "timestamp": "2026-09-11T20:17:12Z", "attachments": []}),
        headers={"X-Internal-Secret": SECRET}, content_type="application/json")


def test_suppressed_closing_ack_gets_a_thumbs_up_on_imessage(db, imessage_on, sidecar, client, anthropic_stub, sms_capture):
    """Live 2026-09-11 20:17: the founder's bare 'Ok' hit 'Fix 1: suppressed closing ack'
    and got NOTHING — the suppression drops it before the buffer, so the tools never see
    it. Rule 1 says the only honest reply to a closing ack is a 👍. Deterministic, no model."""
    from sms import _log_message
    from models import User
    from engagement_tracker import increment_unanswered
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _log_message(user.id, "go study. i'll check in at noon.", "freeform", channel="imessage",
                 provider_sid="spc-prior", delivery_status="sent")          # recent, no question → suppress
    calls_before = len(anthropic_stub.calls)

    r = _post_imessage(client, user.phone, "Ok", "spc-msg-ok")
    assert r.status_code == 200
    reacts = [j for r_, j in sidecar if r_ == "react"]
    assert reacts == [{"phone": user.phone, "message_id": "spc-msg-ok", "emoji": "like"}]
    assert not [j for r_, j in sidecar if r_ == "send"] and sms_capture == []
    assert len(anthropic_stub.calls) == calls_before, "no model call for a bare ack"
    out = [m for m in _rows(db, user) if m.message_type == "reaction"]
    assert len(out) == 1 and "👍" in out[0].body
    increment_unanswered(user.id)
    db.expire_all()
    assert db.get(User, user.id).unanswered_count == 0


def test_suppressed_closing_ack_stays_silent_on_sms(db, imessage_on, sidecar, driver, sms_capture):
    from sms import _log_message
    user = make_user(db, preferred_channel="sms", onboarding_step=3)
    _log_message(user.id, "go study.", "freeform", channel="sms", provider_sid="SM1", delivery_status="sent")
    driver.send(user, "Ok")
    assert sidecar == [] and sms_capture == [], "Twilio has no tapbacks — a suppressed ack stays silent"


def test_ack_with_an_open_question_still_reaches_the_model(db, imessage_on, sidecar, client, anthropic_stub, sms_capture):
    """Suppression only fires when the coach's last message wasn't a question; with an
    open question the ack goes through the buffer to the model as before."""
    from models import get_session, Message
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    s = get_session()
    try:  # naive UTC like prod writes — an aware value shifts by the test cluster's local tz and reads hours old
        s.add(Message(user_id=user.id, direction="out", body="you gonna lift today?", message_type="freeform",
                      channel="imessage", provider_sid="spc-q", delivery_status="sent",
                      created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        s.commit()
    finally:
        s.close()
    anthropic_stub.reply_with(lambda kw: "say less, go get it")
    from tests._sync import PENDING_TIMERS
    _post_imessage(client, user.phone, "Ok", "spc-msg-ok2")
    t = PENDING_TIMERS.pop(user.phone, None)
    assert t is not None, "the ack was buffered for the model, not suppressed"
    t.fire()
    assert [j for r_, j in sidecar if r_ == "send"], "the model's text went out"


def test_ig_and_hesitation_go_to_the_model_not_the_ack_list():
    """Founder (2026-09-13): "ig" is not always a conversation ender, so it is NOT a
    deterministic 👍 — it reaches the model, which voice.md tells not to re-deliver
    its last message. Same for "idk" / "we'll see" / "hmm"."""
    from app import is_closing_acknowledgment
    for not_ack in ("Ig", "ig", "i guess", "I guess so", "sure ig", "idk", "we'll see", "hmm",
                    "ig but what if i choke", "i guess why though"):
        assert not is_closing_acknowledgment(not_ack), not_ack
    for ack in ("ok bet", "got it", "sounds good"):
        assert is_closing_acknowledgment(ack), ack


def test_voice_forbids_re_delivering_the_last_message():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "Never re-deliver your last message" in v
    assert "don't prescribe it back to them" in v


ALEX_NARRATION = ("react to this — it's a simple decline, just acknowledge.\n\n"
                  "Also it's 7:31, class ended at 7:30, they said remind to run after class. "
                  "Should I set that reminder? Earlier I said I'd ping at 7:30. "
                  "Let me set the standing Tue/Thu reminder.")


def test_narration_detector_matches_plans_not_coach_speech():
    from agent_tools import looks_like_narration
    assert looks_like_narration(ALEX_NARRATION)
    assert looks_like_narration("The user declined the water offer. I should react with a thumbs up.")
    assert looks_like_narration("call set_reminder for tue/thu 7:30pm then reply")
    # things the coach legitimately texts TO the user
    for ok in ("all good, won't bring it up again", "let me set that up for u rn",
               "should i set a reminder for that?", "called rsf, they said 11pm",
               "go run, i got u at 7:30", "noted, no water pings"):
        assert not looks_like_narration(ok), ok


def test_narration_is_nudged_once_then_the_real_reply_is_sent(db, imessage_on, sidecar, anthropic_stub, sms_capture, caplog):
    """Live 2026-09-23 (Alex, msg 4461): the model's plan went out as the text. Now: one
    code follow-up, the model answers properly, only that goes out."""
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "Nahh I drink a lot of water", "spc-msg-nah")
    anthropic_stub.push(ALEX_NARRATION, "all good, won't bring it up again")
    with caplog.at_level(logging.WARNING):
        app.process_buffered_message(user.id, "Nahh I drink a lot of water", "freeform")
    sends = [j for r, j in sidecar if r == "send"]
    assert len(sends) == 1 and sends[0]["text"] == "all good, won't bring it up again"
    # (the stub records the loop's messages list by reference, so count the log line)
    assert caplog.text.count("AGENT_LOOP_NARRATION_NUDGE") == 1
    assert any("planning notes" in str(m.get("content")) for c in anthropic_stub.calls for m in c["messages"])


def test_narration_twice_is_dropped_never_sent(db, imessage_on, sidecar, anthropic_stub, sms_capture, caplog):
    import app
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    _inbound(db, user, "Nahh I drink a lot of water", "spc-msg-nah2")
    anthropic_stub.push(ALEX_NARRATION, "The user declined. I should just acknowledge.")
    with caplog.at_level(logging.WARNING):
        app.process_buffered_message(user.id, "Nahh I drink a lot of water", "freeform")
    assert not [j for r, j in sidecar if r == "send"] and sms_capture == []
    assert "AGENT_LOOP_NARRATION_DROPPED" in caplog.text
